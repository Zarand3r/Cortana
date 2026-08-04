"""Server-side conversation persistence: the chat survives closing/reopening the
window (it lives in the server until the app quits), not just in the browser page."""

import json

from cortana.backends import FakeLLMBackend, Message, Role
from cortana.chatapp import Conversation, route


class _Backend(FakeLLMBackend):
    def chat(self, messages):
        return ["hi ", "there"]


def _post(body_msgs, conversation):
    body = json.dumps({"messages": body_msgs}).encode()
    resp = route("POST", "/api/chat", body, _Backend(),
                 system_prompt="sp", index_html="", conversation=conversation)
    list(resp.body)                     # consume the stream (drives recording)
    return resp


# --- Conversation store ----------------------------------------------------

def test_conversation_add_and_snapshot():
    c = Conversation()
    c.add(Message(Role.USER, "a"))
    c.add(Message(Role.ASSISTANT, "b"))
    assert [(m.role, m.content) for m in c.snapshot()] == [
        (Role.USER, "a"), (Role.ASSISTANT, "b")]


def test_conversation_replace():
    c = Conversation()
    c.replace([Message(Role.USER, "x")])
    assert [m.content for m in c.snapshot()] == ["x"]


# --- route persists across "reopens" ---------------------------------------

def test_post_records_user_and_assistant_turns():
    c = Conversation()
    _post([{"role": "user", "content": "hello"}], c)
    snap = [(m.role, m.content) for m in c.snapshot()]
    assert snap == [(Role.USER, "hello"), (Role.ASSISTANT, "hi there")]


def test_history_endpoint_returns_stored_conversation():
    c = Conversation()
    _post([{"role": "user", "content": "hello"}], c)
    # A freshly reopened window fetches /api/history:
    resp = route("GET", "/api/history", b"", _Backend(),
                 system_prompt="sp", index_html="", conversation=c)
    data = json.loads(b"".join(resp.body))
    assert data["messages"] == [
        {"role": "user", "content": "hello"},
        {"role": "assistant", "content": "hi there"},
    ]


def test_history_empty_without_conversation():
    resp = route("GET", "/api/history", b"", _Backend(),
                 system_prompt="sp", index_html="", conversation=None)
    assert json.loads(b"".join(resp.body)) == {"messages": []}


def test_conversation_accumulates_across_turns():
    c = Conversation()
    _post([{"role": "user", "content": "first"}], c)
    # second turn: browser re-sends full history + new user turn
    _post([{"role": "user", "content": "first"},
           {"role": "assistant", "content": "hi there"},
           {"role": "user", "content": "second"}], c)
    roles = [m.role for m in c.snapshot()]
    assert roles == [Role.USER, Role.ASSISTANT, Role.USER, Role.ASSISTANT]
