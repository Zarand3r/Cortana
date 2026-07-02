# Cortana Chat — Local ChatGPT (Design & Reference)

> A private, ChatGPT-style web app backed entirely by an **on-device** LLM. Ships
> as the `cortana chat` subcommand. Zero third-party dependencies — the Python
> stdlib `http.server` serves one self-contained page that streams the model's
> reply token-by-token over Server-Sent Events. Nothing leaves `127.0.0.1`.

This document is the detailed reference for the chat surface: what it is, how it
is wired, the exact request/response protocol, a symbol-by-symbol API reference,
configuration, testing strategy, security posture, and the extension seams
(notably: RAG over Cortana's own screen-memory). For the perception daemon see
[`DESIGN.md`](DESIGN.md); for repo conventions see [`../STYLE.md`](../STYLE.md).

---

## 1. Overview

### 1.1 What it is

`cortana chat` starts a tiny local web server on `127.0.0.1:8808` (configurable)
and serves a single-page chat UI. You type a message in the browser; the page
POSTs the full conversation to the server; the server relays it to a local model
(Ollama or MLX) and streams the reply back token-by-token, rendering it live in a
chat bubble.

```
┌──────────────┐  POST /api/chat        ┌───────────────┐  chat(messages)   ┌──────────┐
│   Browser    │ ─────────────────────▶ │ cortana chat  │ ────────────────▶ │  Ollama  │
│ (index.html) │   {messages:[...]}     │  http.server  │   stream=True     │   / MLX  │
│              │ ◀───────────────────── │  (route/SSE)  │ ◀──────────────── │  (local) │
└──────────────┘   text/event-stream    └───────────────┘   tokens          └──────────┘
        ▲   data: {"token": "..."}  ×N
        └── data: [DONE]
```

### 1.2 Why it is shaped this way

Cortana's perception daemon is deliberately headless — its design says *"No GUI…
no server. Ever."* ([`DESIGN.md` §Non-Goals](DESIGN.md)). The chat app is a
**separate surface**, not part of that daemon, so that rule is preserved. It was
built to honor the repo's standing constraints:

| Constraint (from `STYLE.md` / `DESIGN.md`) | How chat honors it |
|---|---|
| Stdlib-first, resist framework creep | `http.server` + `urllib` only — **no** Flask/FastAPI/websockets |
| Few-file, self-contained | One Python module + one HTML file |
| 100% on-device, egress only to `127.0.0.1` | Server binds localhost; model is local; UI has no external assets |
| Closed contracts via ABC + enums | `chat()` added to the `LLMBackend` ABC; `Role` is a `str, Enum` |
| Config in TOML under `config/` | `[chat]` section in `config/cortana.toml` |
| TDD with a 95% coverage ratchet | Pure logic fully unit-tested; socket/network I/O `# pragma: no cover` |

### 1.3 What it is *not* (v1 scope)

- **No server-side conversation store.** History lives in the browser tab and is
  re-sent on every turn. Closing the tab discards it. (Rationale in §7.)
- **No authentication / multi-user.** It binds `127.0.0.1`; the trust boundary is
  "processes on this Mac."
- **No RAG over Cortana's memory yet.** It is a clean assistant. The hook to add
  screen-memory recall is documented in §9.1.
- **No token/亿-level context management, no tool-calling, no file uploads.** It
  is a faithful minimal ChatGPT, not a superset.

---

## 2. Quick start

```bash
# 1) a local model (one-time)
ollama serve &                        # or: brew services start ollama
ollama pull qwen2.5:7b-instruct       # any chat-tuned model works

# 2) start the chat server
python -m cortana chat                 # then open http://127.0.0.1:8808

# variations
python -m cortana chat --port 9000
python -m cortana chat --model qwen2.5:72b-instruct-q6_K
python -m cortana chat --backend mlx --model mlx-community/Qwen2.5-7B-Instruct-4bit
python -m cortana chat --backend fake  # no model needed — echoes a canned reply (smoke test)
```

Stop with `Ctrl-C`; the server drains and closes its socket cleanly.

---

## 3. Architecture

### 3.1 Component map

```
cortana/
├── backends.py          # LLMBackend ABC + Role/Message + chat() on every backend
├── chatapp.py           # pure routing/parsing/SSE + thin HTTP handler + serve()
├── cli.py               # `chat` subcommand → cmd_chat() → chatapp.serve()
├── config.py            # [chat] host/port/system_prompt
└── webui/
    └── index.html       # self-contained single-page UI (inline CSS + JS)
config/cortana.toml      # shipped [chat] template (mirrors code defaults)
```

**Dependency direction** (one-directional, per `STYLE.md §1`):

```
cli → chatapp → backends → config
                    ▲
              webui/index.html  (data file, read at serve time)
```

`chatapp` imports only from `backends` (for `LLMBackend`, `Message`, `Role`) and
the stdlib. No cycles.

### 3.2 The pure core / thin shell split

The module is intentionally split so that everything worth testing is a pure
function, and only genuinely un-hermetic code (sockets, the network) is excluded
from coverage:

| Layer | Symbols | Tested? |
|---|---|---|
| **Pure core** | `parse_chat_request`, `build_messages`, `sse_frames`, `route`, `load_index`, `make_handler` | ✅ unit-tested |
| **Native shell** | `ChatHandler` (socket read/write), `serve` (binds a port) | `# pragma: no cover` |
| **Network backend** | `OllamaBackend.chat`, `MLXBackend.chat` | `# pragma: no cover` |

`route()` is the whole server contract expressed as a pure function
`(method, path, body, backend) → Response`. `ChatHandler` is a ~25-line adapter
that reads the socket, calls `route()`, and pumps the resulting body iterator to
the wire. This is why the suite can prove the server's behavior without ever
opening a socket.

---

## 4. Request / response protocol

The wire protocol is deliberately tiny: two endpoints.

### 4.1 `GET /` (and `/index.html`) → the UI

Returns `200 text/html; charset=utf-8` with the contents of
`cortana/webui/index.html`. The page is read fresh on each request
(`load_index()`), so editing the HTML shows up on reload without restarting the
server.

### 4.2 `POST /api/chat` → streamed reply

**Request body** (`application/json`):

```json
{
  "messages": [
    {"role": "user", "content": "What is the capital of France?"},
    {"role": "assistant", "content": "Paris."},
    {"role": "user", "content": "And its population?"}
  ]
}
```

- `messages` must be a **non-empty list** of objects, each with a string `role`
  and a string `content`.
- `role` must be one of `system` | `user` | `assistant` (validated against the
  `Role` enum — anything else is a 400).
- The client sends the **entire** conversation each turn (the server is
  stateless). See §7.

**Server-side assembly.** Before calling the model, the server prepends its
configured system prompt via `build_messages()` — *unless* the client already
sent a leading `system` turn, in which case the client's wins. So the model
always sees exactly one system message, and a power user can override it from the
client.

**Success response** — `200 text/event-stream`, `Cache-Control: no-cache`, a
sequence of Server-Sent Events:

```
data: {"token": "Paris"}

data: {"token": " has"}

data: {"token": " about"}

data: {"token": " 2.1"}

data: {"token": " million"}

data: [DONE]

```

- Each token event is `data: ` + a JSON object `{"token": "<piece>"}` + a blank
  line (`\n\n`). Tokens are raw model chunks; concatenating every `token` yields
  the full reply verbatim (whitespace included — no re-joining needed on the
  client).
- The stream is terminated by the sentinel `data: [DONE]\n\n`. This always fires,
  even for an empty reply, so the client can deterministically close the reader.

**Error response** — malformed body returns `400 application/json`:

```json
{"error": "each message needs a 'role' and 'content'"}
```

The error is emitted as a normal (non-SSE) JSON body so a `fetch` caller can
`resp.json()` it. The browser UI surfaces it inline as `⚠ <message>`.

**Unknown routes** return `404 text/plain`.

### 4.3 Why SSE (not WebSockets)?

The data flow is strictly one-directional during a turn (server → browser), which
is exactly what SSE is for. SSE rides on a plain HTTP chunked response, so it
needs **no extra protocol, no dependency, and no handshake** — the stdlib
`BaseHTTPRequestHandler` can produce it by writing framed bytes and flushing.
WebSockets would add a framing library and a bidirectional channel we don't use.

---

## 5. API reference — `cortana/chatapp.py`

### `INDEX_PATH: Path`
Absolute path to `cortana/webui/index.html`, resolved relative to the module so
it works regardless of the process CWD.

### `load_index() -> str`
Reads and returns the UI's HTML (UTF-8). Read on every `GET /` so edits are live.
Tested by asserting the returned HTML contains `<!doctype html>` and the
`/api/chat` endpoint string.

### `parse_chat_request(body: bytes) -> list[Message]`
Parses and **validates** a `POST /api/chat` body into `Message` objects. Raises
`ValueError` (which `route` converts to a 400) on any of: non-JSON body, non-object
top level, missing/empty/`non-list` `messages`, a message missing `role`/`content`,
an unknown role, or a non-string `content`. Roles are coerced through `Role(...)`
so validation is centralized in the enum.

### `build_messages(history: Iterable[Message], system_prompt: str) -> list[Message]`
Returns the message list to send to the model: the configured `system_prompt`
prepended as a `Role.SYSTEM` turn, **unless** `history` already starts with a
system turn (then `history` is returned unchanged). Idempotent w.r.t. a
client-supplied system prompt.

### `sse_frames(tokens: Iterable[str]) -> Iterator[bytes]`
Lazily frames an iterable of reply tokens as SSE byte-events, then yields the
terminating `data: [DONE]\n\n`. Because it is a generator over the backend's token
generator, **no full reply is ever buffered** — bytes flow to the socket as the
model produces them.

### `Response` (dataclass)
`Response(status: int, content_type: str, body: Iterable[bytes])`. A fully
resolved HTTP response. `body` is an iterable of already-encoded chunks, possibly
a live stream (for SSE) or a single-element list (for HTML/JSON/404).

### `route(method, path, body, backend, *, system_prompt, index_html) -> Response`
The pure server contract. Dispatch table:

| method | path | → |
|---|---|---|
| `GET` | `/`, `/index.html` | `200` HTML (`index_html`) |
| `POST` | `/api/chat` | `200` SSE stream, or `400` JSON on `ValueError` |
| *else* | * | `404` |

It takes `index_html` as a parameter (rather than calling `load_index()` itself)
so tests can drive it with a stub and so the file is read once per request in the
handler, not per route call.

### `ChatHandler(BaseHTTPRequestHandler)`  · `# pragma: no cover`
The socket adapter. `_dispatch` reads `Content-Length` bytes, calls `route()`,
writes status + headers (adding `Cache-Control: no-cache` for event streams), then
iterates `resp.body` writing+flushing each chunk. `BrokenPipeError` (user closed
the tab mid-stream) is swallowed. `do_GET`/`do_POST` delegate to `_dispatch`.
`log_message` is silenced. Config (`backend`, `system_prompt`, `index_html`) rides
on the **class**, bound by `make_handler`.

### `make_handler(backend, *, system_prompt, index_html) -> type[ChatHandler]`
Returns a `ChatHandler` subclass with `backend`/`system_prompt`/`index_html` set
as class attributes. This is the idiom for passing state into the stdlib server,
which instantiates the handler class fresh per request. Unit-tested (no socket) by
asserting the returned type carries the bound attributes.

### `serve(backend, *, host="127.0.0.1", port=8808, system_prompt) -> None` · `# pragma: no cover`
Builds a bound handler (reading `load_index()` once at startup), constructs a
`ThreadingHTTPServer`, and `serve_forever()` until `KeyboardInterrupt`, then
`server_close()`. `ThreadingHTTPServer` gives each request its own thread so one
long stream doesn't block a second tab.

---

## 6. Backend contract — `cortana/backends.py`

Chat extends the existing closed `LLMBackend` contract rather than inventing a
parallel one.

### `Role(str, Enum)`
`SYSTEM = "system"`, `USER = "user"`, `ASSISTANT = "assistant"`. The values are
also the wire strings, so `Role(wire_value)` both validates and coerces at the
boundary (unknown → `ValueError`).

### `Message` (frozen dataclass)
`Message(role: Role, content: str)` with `to_wire() -> {"role", "content"}` for
the model APIs.

### `LLMBackend.chat(messages: Iterable[Message]) -> Iterator[str]` *(abstractmethod)*
Added to the ABC, so **every** backend must implement streaming chat (uniform
contract, per `STYLE.md §2`). Returns an iterator of reply tokens.

| Backend | `chat` implementation | Tested |
|---|---|---|
| `FakeLLMBackend` | Yields the canned `response` split into whitespace tokens; increments `calls`. | ✅ |
| `OllamaBackend` | POSTs to `{host}/api/chat` with `stream: true`; yields each line's `message.content`; stops on `done`. Stdlib `urllib` only. | `# pragma: no cover` (network) |
| `MLXBackend` | Applies the tokenizer chat template, then yields `response.text` from `mlx_lm.stream_generate` (lazy import). | `# pragma: no cover` (native) |

`generate()` (single-shot, used by the perception pipeline) is unchanged; `chat()`
is purely additive. The Ollama chat path mirrors the already-proven streaming in
the top-level `ask.py`.

---

## 7. State & conversation model

The server is **stateless across turns**. Each `POST /api/chat` carries the full
conversation; the server assembles `system + history`, streams a reply, and
forgets everything. The browser (`index.html`) owns the `history` array: it pushes
the user turn before sending and pushes the completed assistant turn when the
stream ends.

**Consequences (all intentional for v1):**

- **Privacy:** no conversation is written to disk anywhere. Close the tab → gone.
- **Simplicity:** no session store, no DB migration, no cleanup job.
- **Cost:** the whole history re-tokenizes each turn (fine for local, single-user;
  the model's own context window is the only bound).
- **No cross-device / no resume.** A refresh starts a new conversation.

If persistence is wanted later, §9.2 sketches the change.

---

## 8. Configuration

The `[chat]` section of [`config/cortana.toml`](../config/cortana.toml) (in-code
defaults in `cortana/config.py` are authoritative; the file overrides them):

```toml
[chat]
host = "127.0.0.1"              # bind address (localhost only by default)
port = 8808                     # open http://127.0.0.1:8808 after `cortana chat`
system_prompt = "You are a helpful assistant running fully locally on the user's Mac. Be concise and accurate."
```

| Config field | Default | CLI override | Meaning |
|---|---|---|---|
| `chat_host` | `127.0.0.1` | `--host` | Bind address. **Keep localhost** unless you understand the exposure (§10). |
| `chat_port` | `8808` | `--port` | Web-UI port. |
| `chat_system_prompt` | *(see above)* | — | Server-side system prompt (client may override with its own `system` turn). |
| `backend` | `ollama` | `--backend` | `ollama` \| `mlx` \| `fake`. Shared with the daemon. |
| `model` | `qwen2.5:7b-instruct` | `--model` | Ollama tag or MLX HF repo. Shared with the daemon. |
| `ollama_host` | `http://127.0.0.1:11434` | — | Where the Ollama server lives. |

The shipped TOML is a verbatim template of the code defaults; a test asserts
`Config.load() == Config()` so the two never drift (`STYLE.md §4`).

**CLI wiring:** `cli.py` adds a `chat` subparser (`--port`, `--host`, plus the
common `--config/--backend/--model/--db`). `_config_from_args` maps `--port/--host`
onto `chat_port/chat_host`; `cmd_chat` builds the backend and calls
`chatapp.serve(...)`. `cmd_chat` and `serve` are `# pragma: no cover` (they bind a
real socket); the config wiring is unit-tested in `tests/test_cli.py`.

---

## 9. Extension points

### 9.1 RAG over Cortana's screen-memory (the natural next step)

Cortana already remembers what's been on your screen and can recall it
(`Memory.recall(query, since, until, app)` → rows, feeding
`reasoning.reason(...)`). Wiring that into chat turns the assistant into *"what was
I working on this morning?"* over your own activity. The seam is `build_messages`:

```python
# sketch — inject retrieved screen-memory as extra context before the model call
def build_messages(history, system_prompt, memory=None):
    msgs = _with_system(history, system_prompt)
    if memory is not None:
        last_user = next(m for m in reversed(msgs) if m.role is Role.USER)
        hits = memory.recall(last_user.content, limit=8)          # FTS over OCR
        if hits:
            context = render_citations(hits)                       # ts + app + text
            msgs.insert(1, Message(Role.SYSTEM, f"Relevant memory:\n{context}"))
    return msgs
```

`route`/`serve` would thread a `Memory` through (opened read-only,
`check_same_thread=False` like `cli.open_memory`). Keep it read-only — no writes on
the chat path — and preserve the existing redaction guarantees. This is the single
most valuable extension and was left out of v1 only to keep the first slice clean.

### 9.2 Persisting conversations
Add a `conversations` table (or JSON files under `~/.cortana/chats/`) and two
routes: `GET /api/chats` (list) and `POST /api/chats/{id}` (append). The browser
would load history on open instead of starting empty. Note the privacy trade-off in
§7 — persistence means chats survive on disk and inherit the same "treat as
sensitive / FileVault at rest" posture as the memory DB.

### 9.3 Generation parameters
`temperature`, `num_predict`/`max_tokens`, `top_p` are currently backend defaults.
To expose them, add fields to `[chat]`, thread them into `OllamaBackend.chat`
(`options`) and `MLXBackend.chat` (`sampler`/`max_tokens`), and optionally surface a
settings panel in the UI.

### 9.4 Model picker in the UI
The server knows `cfg.model`; add a `GET /api/models` (shell out to
`ollama list` or read a config list) and a `<select>` in the header that sends a
`model` field alongside `messages`.

---

## 10. Security & privacy posture

- **Localhost only.** Default bind is `127.0.0.1`; the server is unreachable from
  the network. `--host 0.0.0.0` exposes it to your LAN **with no auth** — only do
  this on a trusted network and behind a firewall, and consider it a debugging
  affordance, not a supported deployment.
- **No egress.** The only outbound connection is to the local model
  (`ollama_host`, itself `127.0.0.1`) or the in-process MLX model. The UI embeds
  all CSS/JS inline and loads no remote assets.
- **No persistence.** Conversations are never written to disk (§7). There is
  nothing to redact-at-rest for chat in v1.
- **Untrusted input is the request body.** It is parsed defensively
  (`parse_chat_request` validates shape, types, and role enum) and never `eval`'d
  or interpolated into a shell/SQL. The model prompt is assembled from typed
  `Message` objects.
- **Same-origin, tiny surface.** Two endpoints, no cookies, no auth tokens, no
  file paths accepted from the client. `Content-Length` bounds the read.

---

## 11. Testing

Hermetic, no Ollama/MLX/network/socket, run via `./ci/run.sh` (95% branch-coverage
gate). Chat-specific tests:

**`tests/test_chat_backend.py`** — the backend seam:
- `Role` is the single source of valid roles; unknown role raises.
- `Message.to_wire()` round-trips.
- `FakeLLMBackend.chat` streams multiple tokens that reassemble to the response,
  and counts the call.

**`tests/test_chatapp.py`** — the pure web core:
- `parse_chat_request` accepts a valid body; a parametrized battery of malformed
  bodies each raise `ValueError`.
- `build_messages` prepends the system prompt, and does *not* double it when the
  client already sent one.
- `sse_frames` encodes token events and always terminates with `[DONE]` (incl. the
  empty-stream case).
- `route` covers all four outcomes: HTML on `GET /`, an SSE stream that reassembles
  to the backend's reply (and proves the backend was invoked once), `400` JSON on a
  bad body, `404` on an unknown path.
- `make_handler` binds config onto the handler type; `load_index` returns the
  shipped HTML.

**`tests/test_cli.py`** — `build_config(["chat", ...])` defaults and
`--port/--host/--backend` overrides.

**What is deliberately *not* unit-tested** (marked `# pragma: no cover` with a
reason): `ChatHandler`'s socket read/write, `serve`'s port bind, and the
Ollama/MLX network `chat` paths. These were validated **manually** with an
end-to-end smoke test:

```bash
python -m cortana chat --backend fake --port 8899 &
curl -s http://127.0.0.1:8899/ | grep -o '<title>[^<]*</title>'      # UI served
curl -s -N -X POST http://127.0.0.1:8899/api/chat \
  -H 'Content-Type: application/json' \
  -d '{"messages":[{"role":"user","content":"hello"}]}'               # SSE stream
# → data: {"token": "fake"} / data: {"token": " summary"} / data: [DONE]
```

---

## 12. The web UI — `cortana/webui/index.html`

A single self-contained page (inline CSS + vanilla JS, no build step, no external
requests):

- **Layout:** header (local-only status), scrollable message log, sticky composer
  (auto-growing `<textarea>`; Enter sends, Shift+Enter newlines).
- **Streaming render:** `fetch('/api/chat')` → `resp.body.getReader()` →
  incremental `TextDecoder`. It buffers bytes, splits on the SSE frame delimiter
  `\n\n`, parses each `data:` line, appends each `token` as a text node before a
  blinking cursor element, and stops on `[DONE]`.
- **History:** a JS `history` array of `{role, content}` sent on every turn; the
  user turn is pushed before send, the assistant turn when the stream completes.
- **Errors:** a non-200 response is read as JSON and shown inline as `⚠ <error>`;
  the send button is disabled while a stream is in flight.

Because the server reads the file per request, iterating on the UI is edit-and-
reload — no restart.

---

## 13. Design decisions (log)

| Decision | Choice | Why |
|---|---|---|
| Form factor | Local **web app** (browser) | Most ChatGPT-like; stdlib `http.server` keeps it dependency-free. A terminal REPL was the runner-up. |
| Transport | **SSE** over chunked HTTP | One-directional streaming; no dependency, no handshake, trivial to emit from the stdlib. |
| Server framework | **stdlib `http.server`** | `STYLE.md` "resist framework creep"; two routes don't justify Flask/FastAPI. |
| Contract for streaming | `chat()` on the **existing `LLMBackend` ABC** | Uniform closed contract (`STYLE.md §2`); reuses Ollama/MLX wiring. |
| Conversation state | **Client-held, stateless server** | Zero persistence surface, maximal privacy, minimal code. |
| System prompt | **Server default, client-overridable** | Sensible default out of the box; power users can steer per-conversation. |
| Memory RAG | **Deferred** to a documented seam (§9.1) | Keep the first slice a clean assistant; wiring is a small, well-located change. |
| Coverage of socket/network | **`# pragma: no cover` + manual smoke test** | Matches `STYLE.md §5`: coverage measures testable logic; native/IO is excluded with a reason. |

---

## 14. Troubleshooting

| Symptom | Likely cause | Fix |
|---|---|---|
| Browser shows `⚠ request failed` / connection error immediately | Ollama not running or model not pulled | `ollama serve` (or `brew services start ollama`); `ollama pull <model>` |
| Reply never starts, no error | Model is loading into RAM on first call (large model) | Wait; try a smaller model (`qwen2.5:7b-instruct`); check `ollama ps` |
| `Address already in use` on start | Port 8808 taken (another `cortana chat`?) | `--port 9000`, or kill the other process |
| Page loads but is blank/unstyled | — (shouldn't happen; assets are inline) | Hard-reload; confirm `cortana/webui/index.html` is intact |
| Want to test without any model | Use the fake backend | `python -m cortana chat --backend fake` |
| MLX: `mlx-lm not installed` | MLX backend selected without the package | `pip install mlx-lm`, or use `--backend ollama` |

---

## 15. Related docs

- [`DESIGN.md`](DESIGN.md) — the Cortana perception agent (Memory, Perception, the
  agent loop). Explains why chat is a *separate* surface.
- [`AGENT_LOOP.md`](AGENT_LOOP.md) — the daemon's concurrency substrate.
- [`../STYLE.md`](../STYLE.md) — the conventions this feature was built to.
- [`../README.md`](../README.md) — user-facing quick start (the "Chat" section).
