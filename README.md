# Cortana — Local macOS Context Tracker

A completely local, private context-tracking agent for Apple Silicon. It samples
your foreground app + window title, screenshots the display, OCRs it with Apple's
native **Vision** framework, summarizes the activity with a **local LLM** (Ollama
or `mlx-lm`), and writes everything to a local SQLite database
(`~/.local_mac_context.db`). Nothing leaves the machine.

> ⚠️ **This is a searchable log of everything on your screen.** It auto-skips
> password fields and known password managers, and **redacts high-confidence
> secrets** (private keys, API tokens, Luhn-valid card numbers, SSNs) before
> writing — but treat the `.db` as sensitive. At rest it relies on **FileVault**
> (full-disk encryption); per-database encryption (SQLCipher) is a later opt-in.
> It does **not** log keystrokes. Disable redaction with `--no-redact` (not advised).

---

## Running the agent (`cortana` package)

The agent is being rebuilt as the importable, tested `cortana/` package (Memory +
Perception + the asyncio agent loop — see [`docs/DESIGN.md`](docs/DESIGN.md)). The
hermetic test suite runs with no native deps (`./ci/run.sh`). To run it **live on a
Mac** you need the native bits and a local model:

```bash
# 1. native deps + a local model (one-time)
pip install pyobjc-core pyobjc-framework-Quartz pyobjc-framework-Vision pyobjc-framework-Cocoa
ollama pull qwen2.5:7b-instruct          # the default model
# grant Screen Recording permission to your terminal (System Settings → Privacy)

# 2. run the perceive→remember loop
python -m cortana run                     # uses config/cortana.toml + defaults
python -m cortana run --interval 15 --backend fake   # smoke test without a model

# 3. ask about your past activity (read-only; reasons over stored memory)
python -m cortana ask "what was I working on this morning?"
python -m cortana ask "when did I last open the budget?" --app Numbers --since 2026-06-01
```

### Chat (`cortana chat`) — a local ChatGPT, aware of your activity

A private, ChatGPT-style web UI backed entirely by your **on-device** model,
sharing the same Ollama/MLX backends. Zero extra dependencies: it's the stdlib
`http.server` serving one self-contained page that streams the reply
token-by-token over Server-Sent Events. Nothing leaves `127.0.0.1`.

When launched via the CLI it is **memory-backed**: each turn retrieves your
relevant recent screen activity and grounds the answer in it (ask *"what was I just
working on?"* and it knows). Pass no memory for a plain assistant.

```bash
ollama serve &                            # or: brew services start ollama
ollama pull qwen2.5:7b-instruct           # any chat model you like
python -m cortana chat                     # then open http://127.0.0.1:8808
python -m cortana chat --port 9000 --model qwen2.5:72b-instruct-q6_K
python -m cortana chat --backend mlx --model mlx-community/Qwen2.5-7B-Instruct-4bit
```

Config lives under `[chat]` in [`config/cortana.toml`](config/cortana.toml)
(`host`, `port`, `system_prompt`); flags `--port`/`--host`/`--backend`/`--model`
override it. Multi-turn history is held in the browser and re-sent each turn — no
conversation is written to disk.

### Desktop app (`cortana desktop`) — menu-bar shell

Runs Cortana as a macOS **menu-bar app** so one signed `.app` bundle owns the
Screen Recording / Accessibility permissions and hosts perception + chat +
inference together. Menu: ▶/⏸ tracking, Open Chat (native window), Quit.

```bash
pip install -r requirements-desktop.txt    # rumps + pywebview + pyobjc
python -m cortana desktop                   # run from source
python setup.py py2app                       # build dist/Cortana.app (see docs/DESKTOP.md)
```

Architecture, the event-loop decision, and signing/notarization:
[`docs/DESKTOP.md`](docs/DESKTOP.md).

### Run at login (launchd)

To run Cortana continuously as a background agent (starts at login, restarts on
crash, logs a metrics summary every 10 min and re-prunes daily):

```bash
./install.sh              # installs ~/Library/LaunchAgents/com.cortana.tracker.plist + loads it
tail -f cortana.log       # watch it
./install.sh uninstall    # stop + remove
```

Flags: `--config <toml>` · `--interval <s>` · `--backend ollama|mlx|fake` ·
`--model <tag>` · `--db <path>`. Configuration lives in
[`config/cortana.toml`](config/cortana.toml) (in-code defaults are authoritative;
the file overrides them).

> The legacy single-file `context_tracker.py` (documented below) still works; the
> `cortana` package is its principled rewrite and will replace it.

---

## 1. Install dependencies

Use a Python 3.11+ virtual environment.

```bash
cd ~/Cortana
python3.11 -m venv .venv
source .venv/bin/activate
pip install --upgrade pip

# --- Required native macOS bindings (PyObjC) ---
pip install \
  pyobjc-core \
  pyobjc-framework-Quartz \
  pyobjc-framework-Vision \
  pyobjc-framework-Cocoa

# --- Optional: only if you use --read-focused-text (Accessibility API) ---
pip install pyobjc-framework-ApplicationServices

# --- LLM backend ---
# Option A (default): Ollama — no Python deps, uses the stdlib over HTTP.
#   Just have the server + model ready (see step 3).
# Option B: native MLX (fastest on Apple Silicon):
pip install mlx-lm
```

> Each `pyobjc-framework-*` wheel only pulls in the one system framework it wraps,
> so the install stays lean — no Tesseract, no OpenCV, no heavyweight CV stack.
> (`pip install pyobjc` would install *every* framework binding; we only need 4.)

---

## 2. Grant macOS permissions (one-time)

macOS gates screen capture and OCR behind TCC. The **first run will silently
produce blank screenshots** until you approve the terminal/Python host:

1. **System Settings → Privacy & Security → Screen Recording** → enable your
   terminal app (Terminal / iTerm / VS Code — whatever launches the script).
2. Restart that terminal after toggling (the permission only takes effect for
   newly launched processes).
3. *(Only for `--read-focused-text`)* **Privacy & Security → Accessibility** →
   enable the same terminal app.

Without Screen Recording permission the tracker still runs, logs the app/window,
and records a `skip_reason` of `capture_blocked_or_no_permission` — it just won't
have OCR text.

---

## 3. Provide a local LLM

### Ollama (default backend)
```bash
brew services start ollama                 # or: ollama serve
ollama pull qwen2.5:72b-instruct-q6_K      # the model the script defaults to
```

### MLX (native, fastest)
```bash
# Model is auto-downloaded from Hugging Face on first use:
python context_tracker.py --backend mlx \
  --model mlx-community/Qwen2.5-72B-Instruct-8bit
```

---

## 4. Run

```bash
# Default: Ollama backend, capture every 30s
python context_tracker.py

# Faster cadence, MLX backend, verbose logs
python context_tracker.py --interval 15 \
  --backend mlx --model mlx-community/Qwen2.5-72B-Instruct-8bit -v

# Also capture the focused text field (needs Accessibility permission)
python context_tracker.py --read-focused-text
```

Stop with `Ctrl-C` — the tracker drains its queue and closes the DB cleanly.

### Useful flags
| Flag | Default | Meaning |
|------|---------|---------|
| `--interval` | `30` | seconds between captures |
| `--backend` | `ollama` | `ollama` or `mlx` |
| `--model` | `qwen2.5:72b-instruct-q6_K` | ollama tag or MLX HF repo |
| `--batch-size` | `4` | events per LLM summarization call |
| `--batch-window` | `60` | flush a partial batch after this many seconds |
| `--read-focused-text` | off | log focused field via Accessibility |
| `--db` | `~/.local_mac_context.db` | SQLite path |

---

## 5. Query your history

```bash
sqlite3 ~/.local_mac_context.db \
  "SELECT ts, app_name, summary FROM context
   WHERE summary != '' ORDER BY ts DESC LIMIT 20;"
```

Schema: `context(id, ts, app_name, bundle_id, window_title, ocr_text, summary, captured, skip_reason)`.

---

## Notes & limitations
- **Capture API:** uses `CGWindowListCreateImage` (Quartz) for simplicity. It is
  deprecated on macOS 14+ in favor of **ScreenCaptureKit**; it still works with
  Screen Recording permission. For a fully future-proof build, swap
  `capture_main_display()` to an `SCStream`/`SCScreenshotManager` capture.
- **Window titles** are only populated when Screen Recording permission is
  granted; otherwise `window_title` may be empty.
- **Concurrency:** capture+OCR, the LLM, and SQLite each run on their own
  single-worker thread pool, fed by an `asyncio.Queue`, so a slow model never
  stalls the capture cadence (it applies backpressure / drops oldest instead).
