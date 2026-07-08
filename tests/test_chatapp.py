"""The local chat web app: request parsing, message assembly, SSE framing, and
routing are pure functions and fully tested here. The socket plumbing
(ChatHandler / serve) is native I/O and marked pragma: no cover."""

import json

import pytest

from cortana.backends import FakeLLMBackend, Message, Role
from cortana.chatapp import (
    ChatHandler,
    build_messages,
    load_index,
    make_handler,
    parse_chat_request,
    route,
    sse_frames,
)


# --- parse_chat_request ----------------------------------------------------

def test_parse_valid_request_returns_messages():
    body = json.dumps({"messages": [
        {"role": "user", "content": "hi"},
        {"role": "assistant", "content": "hello"},
    ]}).encode()
    msgs = parse_chat_request(body)
    assert msgs == [Message(Role.USER, "hi"), Message(Role.ASSISTANT, "hello")]


@pytest.mark.parametrize("body", [
    b"not json",
    json.dumps([1, 2]).encode(),                       # not an object
    json.dumps({"messages": []}).encode(),             # empty list
    json.dumps({"messages": "hi"}).encode(),           # not a list
    json.dumps({"messages": [{"role": "user"}]}).encode(),          # no content
    json.dumps({"messages": [{"content": "hi"}]}).encode(),         # no role
    json.dumps({"messages": [{"role": "boss", "content": "hi"}]}).encode(),  # bad role
    json.dumps({"messages": [{"role": "user", "content": 5}]}).encode(),     # content not str
])
def test_parse_rejects_malformed(body):
    with pytest.raises(ValueError):
        parse_chat_request(body)


# --- build_messages --------------------------------------------------------

def test_build_prepends_system_prompt():
    out = build_messages([Message(Role.USER, "hi")], "be nice")
    assert out[0] == Message(Role.SYSTEM, "be nice")
    assert out[1] == Message(Role.USER, "hi")


def test_build_does_not_double_up_when_client_sent_system():
    history = [Message(Role.SYSTEM, "client rules"), Message(Role.USER, "hi")]
    assert build_messages(history, "server default") == history


# --- sse_frames ------------------------------------------------------------

def test_sse_frames_encodes_tokens_and_terminates_with_done():
    frames = list(sse_frames(["a", "b"]))
    assert frames[0] == b'data: {"token": "a"}\n\n'
    assert frames[1] == b'data: {"token": "b"}\n\n'
    assert frames[-1] == b"data: [DONE]\n\n"


def test_sse_frames_empty_stream_still_terminates():
    assert list(sse_frames([])) == [b"data: [DONE]\n\n"]


# --- route -----------------------------------------------------------------

def _backend():
    return FakeLLMBackend(response="hi there")


def test_route_get_root_serves_html():
    resp = route("GET", "/", b"", _backend(),
                 system_prompt="s", index_html="<html>UI</html>")
    assert resp.status == 200
    assert resp.content_type.startswith("text/html")
    assert b"".join(resp.body) == b"<html>UI</html>"


def test_route_post_chat_streams_sse_from_backend():
    body = json.dumps({"messages": [{"role": "user", "content": "hi"}]}).encode()
    be = _backend()
    resp = route("POST", "/api/chat", body, be,
                 system_prompt="be nice", index_html="")
    assert resp.status == 200
    assert resp.content_type == "text/event-stream"
    payload = b"".join(resp.body)
    assert payload.endswith(b"data: [DONE]\n\n")
    reassembled = "".join(
        json.loads(line[len(b"data: "):])["token"]
        for line in payload.split(b"\n\n")
        if line.startswith(b"data: {")
    )
    assert reassembled == "hi there"
    assert be.calls == 1                                # the model was actually invoked


def test_route_post_chat_bad_body_is_400_json():
    resp = route("POST", "/api/chat", b"garbage", _backend(),
                 system_prompt="s", index_html="")
    assert resp.status == 400
    assert resp.content_type == "application/json"
    assert "error" in json.loads(b"".join(resp.body))


def test_route_unknown_path_is_404():
    resp = route("GET", "/nope", b"", _backend(), system_prompt="s", index_html="")
    assert resp.status == 404


# --- load_index ------------------------------------------------------------

def test_make_handler_binds_config_onto_the_handler_type():
    be = _backend()
    handler = make_handler(be, system_prompt="be nice", index_html="<html>")
    assert issubclass(handler, ChatHandler)
    assert handler.backend is be
    assert handler.system_prompt == "be nice"
    assert handler.index_html == "<html>"


def test_load_index_returns_the_shipped_html():
    html = load_index()
    assert "<!doctype html>" in html.lower()
    assert "/api/chat" in html                          # the UI actually calls the endpoint
