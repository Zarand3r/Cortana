"""Chat is context-aware: the web app retrieves relevant screen-memory and injects
it into the conversation so answers are grounded in what the user has been doing."""

import json

from cortana.backends import FakeLLMBackend, Message, Role
from cortana.chatapp import (
    build_messages,
    format_context_block,
    latest_user_text,
    retrieve_context,
    route,
)
from cortana.memory import Memory
from cortana.perception import Observation, Semantic


class _Recorder(FakeLLMBackend):
    """A fake backend that records the messages it was asked to chat over."""

    last_messages: list = []

    def chat(self, messages):
        self.last_messages = list(messages)
        return ["ok"]


def _seed(tmp_path):
    mem = Memory(tmp_path / "m.db")
    obs = Observation(ts="2026-07-01T09:00:00+00:00", app_name="Numbers",
                      bundle_id="com.apple.Numbers", window_title="Q2",
                      ocr_text="quarterly budget spreadsheet", captured=True)
    mem.remember([obs], Semantic(summary="Reviewing the Q2 budget in Numbers.",
                                 model="fake", window_start_ts=obs.ts,
                                 window_end_ts=obs.ts))
    return mem


# --- helpers ---------------------------------------------------------------

def test_latest_user_text_returns_last_user_turn():
    history = [Message(Role.USER, "first"), Message(Role.ASSISTANT, "a"),
               Message(Role.USER, "second")]
    assert latest_user_text(history) == "second"


def test_latest_user_text_empty_when_no_user():
    assert latest_user_text([Message(Role.ASSISTANT, "a")]) == ""


def test_format_context_block_empty_for_no_memories():
    assert format_context_block([]) == ""


def test_format_context_block_lists_time_app_and_body():
    block = format_context_block([
        {"ts": "2026-07-01T09:00:00+00:00", "app_name": "Numbers",
         "summary": "Reviewing the Q2 budget", "ocr_text": ""},
    ])
    assert "Numbers" in block
    assert "budget" in block
    assert "2026-07-01T09:00:00+00:00" in block


def test_retrieve_context_finds_seeded_memory(tmp_path):
    mem = _seed(tmp_path)
    rows = retrieve_context(mem, "what was I doing with the budget", limit=8)
    assert any("budget" in (r.get("summary", "") + r.get("ocr_text", "")).lower()
               for r in rows)
    mem.close()


# --- build_messages context injection --------------------------------------

def test_build_messages_injects_context_into_system_prompt():
    out = build_messages([Message(Role.USER, "hi")], "be nice", "CTX-BLOCK")
    assert out[0].role is Role.SYSTEM
    assert "be nice" in out[0].content and "CTX-BLOCK" in out[0].content


def test_build_messages_appends_context_to_client_system_turn():
    history = [Message(Role.SYSTEM, "client rules"), Message(Role.USER, "hi")]
    out = build_messages(history, "server default", "CTX")
    assert out[0].role is Role.SYSTEM
    assert "client rules" in out[0].content and "CTX" in out[0].content
    assert out[1] == Message(Role.USER, "hi")


def test_build_messages_no_context_unchanged():
    # default context_block="" preserves the original behavior
    out = build_messages([Message(Role.USER, "hi")], "sp")
    assert out == [Message(Role.SYSTEM, "sp"), Message(Role.USER, "hi")]


# --- route wiring ----------------------------------------------------------

def test_route_injects_memory_context(tmp_path):
    mem = _seed(tmp_path)
    rec = _Recorder()
    body = json.dumps({"messages": [
        {"role": "user", "content": "what was I doing with the budget"}]}).encode()
    resp = route("POST", "/api/chat", body, rec,
                 system_prompt="sp", index_html="", memory=mem)
    assert resp.status == 200
    sys_msgs = [m for m in rec.last_messages if m.role is Role.SYSTEM]
    assert sys_msgs and "budget" in sys_msgs[0].content.lower()
    mem.close()


def test_route_without_memory_uses_plain_system_prompt():
    rec = _Recorder()
    body = json.dumps({"messages": [{"role": "user", "content": "hi"}]}).encode()
    resp = route("POST", "/api/chat", body, rec,
                 system_prompt="sp", index_html="", memory=None)
    assert resp.status == 200
    assert rec.last_messages[0].content == "sp"
