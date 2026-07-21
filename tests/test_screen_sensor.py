"""The live ScreenSensor composes the native capture/OCR calls into an Observation.
The native calls are faked here so the *composition* logic — including window-title
population, which was a latent bug (the live sensor shipped an empty title) — is
covered hermetically without PyObjC."""

from cortana import perception


def _fake_natives(monkeypatch, *, app="Safari", bundle="com.apple.Safari", pid=42,
                  title="Cortana — Wikipedia", text="some page text"):
    monkeypatch.setattr(perception, "frontmost_app", lambda: (app, bundle, pid))
    monkeypatch.setattr(perception, "frontmost_window_title", lambda p: title)
    monkeypatch.setattr(perception, "capture_screen", lambda: object())      # non-None image
    monkeypatch.setattr(perception, "image_hash", lambda img: "hash-xyz")
    monkeypatch.setattr(perception, "ocr_image", lambda img, langs: text)


def test_screen_sensor_populates_window_title(monkeypatch):
    _fake_natives(monkeypatch, title="Inbox (3) — Mail")
    sensor = perception.ScreenSensor(("en-US",), dedup=False)
    obs = sensor("2026-07-19T09:00:00+00:00")
    assert obs.captured is True
    assert obs.window_title == "Inbox (3) — Mail"      # was "" before the fix
    assert obs.ocr_text == "some page text"


def test_screen_sensor_window_title_blank_when_unavailable(monkeypatch):
    _fake_natives(monkeypatch, title="")
    sensor = perception.ScreenSensor(("en-US",), dedup=False)
    obs = sensor("t")
    assert obs.window_title == ""                        # no title -> empty, not a crash


def test_volatile_title_tokens_do_not_flip_change_detection():
    # Unread counters / progress % / spinner frames in a TITLE change every second
    # while content is static — they must not defeat compaction (a row + LLM share
    # per second on an idle screen). OCR *content* keeps its counts (a real change).
    from cortana.perception import content_hash
    a = content_hash("Slack", "(3) Slack — #general", "static body")
    b = content_hash("Slack", "(4) Slack — #general", "static body")
    assert a == b                                      # count flip: NOT a change
    c = content_hash("Term", "⠧ build — 42%", "static body")
    d = content_hash("Term", "⠇ build — 43%", "static body")
    assert c == d                                      # spinner/percent: NOT a change
    e = content_hash("Slack", "(3) Slack — #general", "someone posted a message")
    assert e != a                                      # real content change still fires


def test_screen_sensor_window_title_flows_into_change_detection(monkeypatch):
    # window_title feeds content_hash, so two identical screens with *different*
    # titles must be seen as different content (a real regression the empty-title
    # bug masked). Here we just assert the title reaches the Observation used to hash.
    _fake_natives(monkeypatch, title="doc-A.md", text="same body")
    sensor = perception.ScreenSensor(("en-US",), dedup=False)
    a = sensor("t1")
    _fake_natives(monkeypatch, title="doc-B.md", text="same body")
    b = sensor("t2")
    assert a.window_title != b.window_title
