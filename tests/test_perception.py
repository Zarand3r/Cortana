"""Step 2: perception pure logic — change detection, prompt, meaning extraction."""

from cortana.backends import FakeLLMBackend
from cortana.perception import (
    Observation,
    build_meaning_prompt,
    changed,
    content_hash,
    extract_meaning,
    normalize,
)


def _obs(ocr, *, app="Notes", title="Untitled", ts="2026-06-14T10:00:00+00:00"):
    return Observation(ts=ts, app_name=app, bundle_id="com.app", window_title=title,
                       ocr_text=ocr, captured=True)


# --- normalize / hash / changed (P6) --------------------------------------- #

def test_normalize_strips_clock():
    a = normalize("Notes", "Doc", "Meeting notes\nStatus bar 12:34")
    b = normalize("Notes", "Doc", "Meeting notes\nStatus bar 12:35")
    assert a == b


def test_normalize_strips_date():
    a = normalize("Cal", "June", "Today is 2026-06-14 budget review")
    b = normalize("Cal", "June", "Today is 2026-06-15 budget review")
    assert a == b


def test_content_hash_stable_and_sensitive():
    h1 = content_hash("Notes", "Doc", "alpha beta")
    h2 = content_hash("Notes", "Doc", "alpha beta")
    h3 = content_hash("Notes", "Doc", "alpha gamma")
    assert h1 == h2
    assert h1 != h3


def test_changed():
    h = content_hash("A", "B", "C")
    assert changed(None, h) is True
    assert changed(h, h) is False
    assert changed("other", h) is True


# --- prompt building -------------------------------------------------------- #

def test_build_meaning_prompt_contains_metadata():
    batch = [_obs("some on-screen text", app="Safari", title="Inbox")]
    prompt = build_meaning_prompt(batch, max_chars=6000)
    assert "Safari" in prompt
    assert "Inbox" in prompt
    assert "some on-screen text" in prompt


def test_build_meaning_prompt_truncates_ocr():
    long_text = "x" * 5000
    batch = [_obs(long_text)]
    prompt = build_meaning_prompt(batch, max_chars=100)
    assert "x" * 5000 not in prompt
    assert "truncated" in prompt.lower()


# --- extract_meaning (tool uses backend) ------------------------------------ #

def test_extract_meaning_uses_backend():
    be = FakeLLMBackend(response="Writing meeting notes.", model="fake-7b")
    batch = [
        _obs("notes part 1", ts="2026-06-14T10:00:00+00:00"),
        _obs("notes part 2", ts="2026-06-14T10:00:30+00:00"),
    ]
    sem = extract_meaning(batch, be, max_chars=6000)
    assert sem.summary == "Writing meeting notes."
    assert sem.model == "fake-7b"
    assert sem.window_start_ts == "2026-06-14T10:00:00+00:00"
    assert sem.window_end_ts == "2026-06-14T10:00:30+00:00"
    assert be.calls == 1


# --- the efficiency property: idle screens don't drive the LLM (P6) --------- #

def test_idle_loop_skips_extraction():
    """10 frames, only 2 distinct screens -> extract_meaning runs twice, not 10x."""
    frames = (
        [_obs("budget spreadsheet")] * 5
        + [_obs("email reading")] * 5
    )
    be = FakeLLMBackend(response="x")
    prev = None
    for frame in frames:
        h = content_hash(frame.app_name, frame.window_title, frame.ocr_text)
        if changed(prev, h):
            extract_meaning([frame], be, max_chars=6000)
        prev = h
    assert be.calls == 2  # 80% fewer than 10 -> exceeds the ≥70% target
