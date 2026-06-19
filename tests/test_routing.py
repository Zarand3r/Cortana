"""Step 7: the pure change-detection routing decision used by the producer."""

from cortana.agent import Disposition, plan_disposition
from cortana.perception import Observation


def _obs(ocr, *, app="Notes", title="Doc", ts="2026-06-15T10:00:00+00:00"):
    return Observation(ts=ts, app_name=app, bundle_id="com.app", window_title=title,
                       ocr_text=ocr, captured=True)


def test_first_observation_is_changed_and_gets_a_hash():
    obs = _obs("hello world")
    assert plan_disposition(None, obs) is Disposition.CHANGED
    assert obs.content_hash != ""          # decision assigns the hash


def test_identical_content_is_unchanged():
    o1 = _obs("same screen")
    plan_disposition(None, o1)
    o2 = _obs("same screen")
    assert plan_disposition(o1.content_hash, o2) is Disposition.UNCHANGED


def test_clock_only_change_is_unchanged():
    o1 = _obs("notes status 12:34")
    plan_disposition(None, o1)
    o2 = _obs("notes status 12:35")
    assert plan_disposition(o1.content_hash, o2) is Disposition.UNCHANGED


def test_real_content_change_is_changed():
    o1 = _obs("alpha")
    plan_disposition(None, o1)
    o2 = _obs("beta")
    assert plan_disposition(o1.content_hash, o2) is Disposition.CHANGED
    assert o2.content_hash != o1.content_hash
