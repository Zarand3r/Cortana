# Cortana Chat — the memory-backed local chat surface

A ChatGPT-style web UI served from the menu-bar app on `127.0.0.1:8808`, answered
by the local model (MLX/Ollama) and **grounded in screen memory**. Pure stdlib
(`http.server` + SSE) — no framework, no cloud, nothing leaves the machine.

## How a question is answered

```
browser (webui/index.html)
  └─ POST /api/chat {messages}          one server-side Conversation per app run
       └─ route() builds the model call:
            system prompt
            + LIVE block      — working memory: what's on screen RIGHT NOW
            + MEMORY block    — recall() over SQLite (hybrid FTS5 + embeddings,
                                recency fallback for present-tense questions)
            + conversation history
       └─ backend.chat() streams tokens → SSE frames → the UI renders live
```

- **LIVE beats the DB for "now":** the working-memory block answers "what am I
  doing right now" from the last few seconds; the DB lags by batch + LLM latency.
- **Citations:** retrieved rows carry `[timestamp] app` so answers can point at
  the screen they came from.

## State model

The **server owns one `Conversation`** for the app's lifetime: closing/reopening
the chat window resumes the same conversation; quitting Cortana discards it.
Nothing conversational is written to disk — memory (SQLite) stores what was on
your *screen*, never your chat turns.

## Surfaces

| Route | What |
|---|---|
| `GET /` | the single-file UI (`cortana/webui/index.html`, shipped via `importlib.resources`) |
| `POST /api/chat` | JSON `{messages: [{role, content}]}` → SSE stream (`data: {"token": …}` … `data: [DONE]`) |

Entry points: the desktop app serves it automatically; standalone `cortana chat`
(same server, no tracker). Config: `[chat] host/port/system_prompt` in
`cortana.toml` — bound to `127.0.0.1` only.

## Design decisions

| Choice | Why |
|---|---|
| stdlib `http.server` + SSE | two routes don't justify a framework; SSE is one-directional streaming with zero deps (`STYLE.md`) |
| `chat()` on the `LLMBackend` ABC | one closed contract; Ollama/MLX/fake all stream the same way |
| server-held Conversation | the window is a subprocess that may be closed/reopened; persistence until quit is the product behavior |
| memory injected in `route()` | grounding is a server concern; the UI stays a dumb renderer |

## Testing

The pure core (`Conversation`, `parse_chat_request`, `route`, `sse_frames`,
`format_*_block`) is fully covered hermetically (`tests/test_chat_*`,
`FakeLLMBackend`); the socket layer is `# pragma: no cover` per `STYLE.md`.

Related: [`DESKTOP.md`](DESKTOP.md) (the shell) · [`MEMORY.md`](MEMORY.md)
(retrieval) · [`DESIGN.md`](DESIGN.md) (architecture).
