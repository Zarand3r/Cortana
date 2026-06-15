"""Step 1: the data shapes the rest of the system depends on."""

from cortana.perception import Observation, Semantic


def test_observation_fields_and_defaults():
    obs = Observation(
        ts="2026-06-14T10:00:00+00:00",
        app_name="Safari",
        bundle_id="com.apple.Safari",
        window_title="Inbox",
        ocr_text="hello world",
        captured=True,
    )
    assert obs.app_name == "Safari"
    assert obs.captured is True
    assert obs.skip_reason == ""
    assert obs.focused_text == ""
    assert obs.content_hash == ""


def test_semantic_fields():
    sem = Semantic(
        summary="Reading email in Safari.",
        model="fake",
        window_start_ts="2026-06-14T10:00:00+00:00",
        window_end_ts="2026-06-14T10:01:00+00:00",
    )
    assert sem.summary.startswith("Reading email")
    assert sem.model == "fake"
    assert sem.window_start_ts < sem.window_end_ts
