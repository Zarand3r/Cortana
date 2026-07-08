"""Regression tests for the principal-engineer hardening pass: failure resilience,
memory robustness, redaction coverage, and config validation."""

import pytest

from cortana.backends import FakeLLMBackend, Message, Role
from cortana.chatapp import sse_frames
from cortana.config import Config
from cortana.memory import Memory
from cortana.perception import Observation, Semantic
from cortana.redaction import redact_observation


# --- recall robustness (memory) --------------------------------------------

def _seed(tmp_path):
    mem = Memory(tmp_path / "m.db")
    o = Observation(ts="2026-07-04T09:00:00+00:00", app_name="Numbers", bundle_id="c",
                    window_title="Q2", ocr_text="quarterly budget", captured=True)
    mem.remember([o], Semantic(summary="s", model="fake",
                               window_start_ts=o.ts, window_end_ts=o.ts))
    return mem


@pytest.mark.parametrize("q", ['budget"', "foo (bar", 'a AND b OR', '"', "*", "c++"])
def test_recall_never_crashes_on_raw_fts_syntax(tmp_path, q):
    mem = _seed(tmp_path)
    mem.recall(query=q)          # must not raise sqlite3.OperationalError
    mem.close()


def test_recall_clamps_nonpositive_limit(tmp_path):
    mem = _seed(tmp_path)
    # LIMIT -1 in SQLite means "unbounded"; recall must clamp so it can't dump the DB.
    assert len(mem.recall(limit=-1)) <= 1
    assert len(mem.recall(limit=0)) <= 1
    mem.close()


# --- redaction covers window_title (real bypass) ---------------------------

def test_redact_observation_scrubs_window_title():
    obs = Observation(ts="t", app_name="Safari", bundle_id="c",
                      window_title="Reset your password ghp_" + "a" * 36,
                      ocr_text="", captured=True)
    out = redact_observation(obs)
    assert "ghp_" not in out.window_title
    assert "REDACTED" in out.window_title


# --- chat SSE surfaces a mid-stream failure --------------------------------

def test_sse_frames_emits_error_frame_on_failure():
    def tokens():
        yield "hello"
        raise RuntimeError("backend exploded")
    frames = list(sse_frames(tokens()))
    assert any(b"hello" in f for f in frames)
    assert any(b"error" in f and b"exploded" in f for f in frames)
    assert frames[-1] == b"data: [DONE]\n\n"       # stream still terminates cleanly


# --- config fails loudly on bad values, never silently ---------------------

def test_config_rejects_string_bool_instead_of_silently_truthy():
    # `redact = "false"` must NOT be silently read as True.
    with pytest.raises(ValueError) as e:
        Config.from_dict({"privacy": {"redact": "false"}})
    assert "redact" in str(e.value)


def test_config_rejects_scalar_where_list_expected():
    with pytest.raises(ValueError) as e:
        Config.from_dict({"perception": {"ocr_languages": "en-US"}})
    assert "ocr_languages" in str(e.value)        # not shattered into ('e','n',...)


def test_config_error_names_section_and_key():
    with pytest.raises(ValueError) as e:
        Config.from_dict({"perception": {"interval": "thirty"}})
    assert "perception" in str(e.value) and "interval" in str(e.value)


def test_config_accepts_valid_native_types():
    cfg = Config.from_dict({"privacy": {"redact": False},
                            "perception": {"ocr_languages": ["en-US", "fr-FR"]}})
    assert cfg.redact is False
    assert cfg.ocr_languages == ("en-US", "fr-FR")
