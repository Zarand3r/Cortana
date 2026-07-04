"""A local, private ChatGPT-style web app backed by an on-device LLM.

This is a *separate* surface from the Cortana perception daemon: a thin,
zero-dependency web server (stdlib ``http.server`` only) that serves a
single-page chat UI and streams the local model's reply token-by-token over
Server-Sent Events. It reuses the package's ``LLMBackend`` contract, so the same
Ollama / MLX models drive both perception and chat. Nothing leaves ``127.0.0.1``.

The routing, request parsing and SSE framing are pure functions (fully tested);
the socket plumbing (``ChatHandler`` / ``serve``) is a thin, native-I/O shell
marked ``# pragma: no cover``.
"""

from __future__ import annotations

import json
from collections.abc import Iterable, Iterator
from dataclasses import dataclass
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from importlib.resources import files

from cortana.backends import LLMBackend, Message, Role
from cortana.memory import Memory
from cortana.reasoning import question_to_fts

_CONTEXT_PER_MEMORY_CHARS = 400   # cap each memory in the context block


def load_index() -> str:
    """Return the chat UI's HTML. Loaded via importlib.resources so it resolves in
    a source checkout AND inside a packaged .app bundle (no fragile __file__ paths)."""
    return (files("cortana") / "webui" / "index.html").read_text(encoding="utf-8")


# --------------------------------------------------------------------------- #
# Memory context — make the chat aware of what the user has been doing.        #
# The latest user turn drives retrieval over Cortana's own screen-memory; the  #
# results are injected into the system prompt so answers are grounded + cited. #
# --------------------------------------------------------------------------- #
def latest_user_text(history: Iterable[Message]) -> str:
    """The most recent user turn's content (drives retrieval), or '' if none."""
    for msg in reversed(list(history)):
        if msg.role is Role.USER:
            return msg.content
    return ""


def retrieve_context(memory: Memory, query: str, *, limit: int = 8) -> list[dict]:
    """Recall memories relevant to ``query`` (full-text on its content words;
    falls back to most-recent when the query has no signal — useful for
    'what should I do next?')."""
    return memory.recall(query=question_to_fts(query), limit=limit)


_CONTEXT_HEADER = "--- CONTEXT: THE USER'S RECENT SCREEN ACTIVITY ---"


def format_context_block(memories: list[dict]) -> str:
    """Render retrieved memories into a system-prompt context block. When memory is
    empty, return an explicit 'nothing recorded' note (never '') so the model reports
    that instead of claiming it cannot see the screen."""
    if not memories:
        return (f"{_CONTEXT_HEADER}\n(No screen activity has been recorded yet — "
                "tracking may be off or just started.)")
    lines = [f"{_CONTEXT_HEADER}  (cite the time + app if you rely on it)"]
    for m in memories:
        body = (m.get("summary") or m.get("ocr_text") or "").strip().replace("\n", " ")
        lines.append(f"[{m['ts']}] app={m['app_name']!r}: {body[:_CONTEXT_PER_MEMORY_CHARS]}")
    return "\n".join(lines)


def parse_chat_request(body: bytes) -> list[Message]:
    """Parse a POST body of ``{"messages": [{"role", "content"}, ...]}`` into
    validated ``Message`` objects. Raises ``ValueError`` on anything malformed so
    the caller can turn it into a 400."""
    try:
        data = json.loads(body)
    except json.JSONDecodeError as exc:
        raise ValueError(f"body is not valid JSON: {exc}") from exc
    if not isinstance(data, dict):
        raise ValueError("body must be a JSON object")
    raw = data.get("messages")
    if not isinstance(raw, list) or not raw:
        raise ValueError("'messages' must be a non-empty list")
    messages: list[Message] = []
    for item in raw:
        if not isinstance(item, dict) or "role" not in item or "content" not in item:
            raise ValueError("each message needs a 'role' and 'content'")
        role = Role(item["role"])                    # ValueError on unknown role
        content = item["content"]
        if not isinstance(content, str):
            raise ValueError("message 'content' must be a string")
        messages.append(Message(role, content))
    return messages


def build_messages(history: Iterable[Message], system_prompt: str,
                   context_block: str = "") -> list[Message]:
    """Assemble the conversation with a system prompt, folding in an optional
    memory ``context_block``. Respects a client-supplied system turn (augmenting
    it with context rather than duplicating)."""
    history = list(history)
    if history and history[0].role is Role.SYSTEM:
        if context_block:
            head = Message(Role.SYSTEM, f"{history[0].content}\n\n{context_block}")
            return [head, *history[1:]]
        return history
    system = f"{system_prompt}\n\n{context_block}" if context_block else system_prompt
    return [Message(Role.SYSTEM, system), *history]


def sse_frames(tokens: Iterable[str]) -> Iterator[bytes]:
    """Frame a stream of reply tokens as Server-Sent Events, terminated by a
    sentinel ``[DONE]`` event the browser uses to close the stream. If the token
    source fails mid-stream (e.g. the backend dies), emit an ``error`` frame so the
    client sees a visible failure instead of a silently truncated reply."""
    try:
        for token in tokens:
            yield b"data: " + json.dumps({"token": token}).encode("utf-8") + b"\n\n"
    except Exception as exc:  # noqa: BLE001 - surface any backend/stream failure to the UI
        yield b"data: " + json.dumps({"error": str(exc)}).encode("utf-8") + b"\n\n"
    yield b"data: [DONE]\n\n"


@dataclass
class Response:
    """A fully-resolved HTTP response: status, content-type, and an iterable of
    body chunks (already encoded, possibly a live stream)."""

    status: int
    content_type: str
    body: Iterable[bytes]


def route(method: str, path: str, body: bytes, backend: LLMBackend,
          *, system_prompt: str, index_html: str,
          memory: Memory | None = None, context_limit: int = 8) -> Response:
    """Map a request to a Response. Pure: no sockets, no globals — the whole
    server contract lives here so it can be tested directly. When ``memory`` is
    given, the reply is grounded in the user's recent screen activity."""
    if method == "GET" and path in ("/", "/index.html"):
        return Response(200, "text/html; charset=utf-8", [index_html.encode("utf-8")])
    if method == "POST" and path == "/api/chat":
        try:
            history = parse_chat_request(body)
        except ValueError as exc:
            payload = json.dumps({"error": str(exc)}).encode("utf-8")
            return Response(400, "application/json", [payload])
        context_block = ""
        if memory is not None:
            memories = retrieve_context(memory, latest_user_text(history),
                                        limit=context_limit)
            context_block = format_context_block(memories)
        messages = build_messages(history, system_prompt, context_block)
        return Response(200, "text/event-stream", sse_frames(backend.chat(messages)))
    return Response(404, "text/plain; charset=utf-8", [b"not found"])


class ChatHandler(BaseHTTPRequestHandler):  # pragma: no cover - native socket I/O
    """Thin adapter: reads the request, delegates to ``route``, streams the body.
    Bound to a backend/config via ``make_handler``."""

    backend: LLMBackend
    system_prompt: str
    index_html: str
    memory: Memory | None = None

    def _dispatch(self, method: str) -> None:
        length = int(self.headers.get("Content-Length") or 0)
        body = self.rfile.read(length) if length else b""
        resp = route(method, self.path, body, self.backend,
                     system_prompt=self.system_prompt, index_html=self.index_html,
                     memory=self.memory)
        self.send_response(resp.status)
        self.send_header("Content-Type", resp.content_type)
        if resp.content_type == "text/event-stream":
            self.send_header("Cache-Control", "no-cache")
        self.end_headers()
        try:
            for chunk in resp.body:
                self.wfile.write(chunk)
                self.wfile.flush()
        except BrokenPipeError:
            pass                                     # client closed the tab mid-stream

    def do_GET(self) -> None:
        self._dispatch("GET")

    def do_POST(self) -> None:
        self._dispatch("POST")

    def log_message(self, *args) -> None:
        pass                                         # keep the console quiet


def make_handler(backend: LLMBackend, *, system_prompt: str, index_html: str,
                 memory: Memory | None = None) -> type[ChatHandler]:
    """Build a ChatHandler subclass bound to this backend/config (the stdlib
    server instantiates the class per request, so config rides on the type)."""

    class _Bound(ChatHandler):  # pragma: no cover - native socket I/O
        pass

    _Bound.backend = backend
    _Bound.system_prompt = system_prompt
    _Bound.index_html = index_html
    _Bound.memory = memory
    return _Bound


def serve(backend: LLMBackend, *, host: str = "127.0.0.1", port: int = 8808,
          system_prompt: str,
          memory: Memory | None = None) -> None:  # pragma: no cover - binds a real socket
    """Run the chat web server until interrupted. When ``memory`` is given, replies
    are grounded in the user's recent screen activity."""
    handler = make_handler(backend, system_prompt=system_prompt,
                           index_html=load_index(), memory=memory)
    server = ThreadingHTTPServer((host, port), handler)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
