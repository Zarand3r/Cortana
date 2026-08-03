# Cortana — a local, screen-aware chat agent

Cortana is **one app**: a completely local, private macOS agent that watches what's
on your screen, remembers it, **gives you recommendations based on your activity**,
and lets you **chat with an assistant that knows your context** — all on-device.
Nothing leaves your machine.

It runs as a **menu-bar desktop app**:

- **Perceives** — every N seconds it captures the screen, OCRs it (Apple **Vision**),
  and summarizes the activity with a **local LLM** (Ollama or MLX).
- **Remembers** — stores a searchable, bounded episodic + semantic memory in SQLite.
- **Recommends** — suggests a helpful next action grounded in your recent activity.
- **Chats** — a ChatGPT-style window whose answers are grounded in your screen memory.

> ⚠️ **Cortana's memory is a searchable log of what's been on your screen.** It skips
> password fields + known password managers and **redacts high-confidence secrets**
> (private keys, API tokens, Luhn-valid cards, SSNs) before writing. It never logs
> keystrokes. At rest it relies on **FileVault**; SQLCipher is a later opt-in. Treat
> the `.db` as sensitive.

---

## Try it in 30 seconds (no deps, no model)

The whole pipeline runs against a synthetic screen + a fake model, so you can see
perceive → remember → recall/chat without installing anything or granting permissions:

```bash
python -m cortana run  --backend fake --demo --ticks 20 --db /tmp/cortana.db   # fill memory
python -m cortana ask  "what was I working on" --backend fake --db /tmp/cortana.db
python -m cortana recommend --backend fake --db /tmp/cortana.db                 # a suggestion
python -m cortana chat --backend fake --db /tmp/cortana.db                      # open http://127.0.0.1:8808
```

## Install & run it for real (menu-bar app)

One command sets up a virtualenv, installs the app, and pulls a model:

```bash
./setup.sh                 # macOS: desktop app + Ollama model
./setup.sh --core-only     # CLI only, no native deps (any OS)
./setup.sh --mlx           # also install the MLX backend
```

Then:

```bash
source .venv/bin/activate
cortana desktop            # the menu-bar app (or just: cortana)
```

First launch: grant **Screen Recording** in **System Settings → Privacy & Security**,
then relaunch — macOS ties the permission to the launching app. (Manual install
instead of `setup.sh`: `pip install '.[desktop]'` + `ollama pull qwen2.5:7b-instruct`.)

## Give it to someone else (export / distribute)

Cortana is a normal Python package, so there are three ways to hand it off:

```bash
# 1. Share the folder/repo — they run one command:
./setup.sh

# 2. Build a wheel and send that file:
./.venv/bin/pip install build && ./.venv/bin/python -m build   # -> dist/cortana-0.1.0-*.whl
#    they:  pip install 'cortana-0.1.0-py3-none-any.whl[desktop]'

# 3. Build the double-clickable menu-bar .app (the single-artifact product):
./scripts/build_release.sh        # -> dist/Cortana.app (unsigned local build)
#    with SIGN_ID + NOTARY_PROFILE set -> dist/Cortana.dmg, notarized for anyone
```

Do **not** run py2app by hand — the release script performs required post-build
steps (mlx dylibs, metadata, a headless boot self-check) and signs nested code
correctly. Details + checklist: [`docs/PRODUCTION.md`](docs/PRODUCTION.md).

## The pieces, individually

The menu-bar app composes these; each is also a CLI facet (handy for testing):

| Command | What it does |
|---|---|
| `cortana run` | the perceive→remember loop (`--demo` for a synthetic sensor, `--ticks N` to bound it) |
| `cortana ask "<q>"` | recall + reason over memory, with citations (read-only) |
| `cortana recommend` | one grounded suggestion from recent activity |
| `cortana chat` | the memory-backed chat web UI |
| `cortana desktop` | the unified menu-bar app (default when no command is given) |

Configuration lives in [`config/cortana.toml`](config/cortana.toml) (in-code defaults
are authoritative; the TOML overrides them). Common flags: `--config`, `--backend
ollama|mlx|fake`, `--model`, `--db`, `--interval`, `--no-redact`.

## Design & development

- Architecture and phase roadmap: [`docs/DESIGN.md`](docs/DESIGN.md)
- Agent loop internals: [`docs/AGENT_LOOP.md`](docs/AGENT_LOOP.md) ·
  chat: [`docs/CHAT.md`](docs/CHAT.md) · desktop: [`docs/DESKTOP.md`](docs/DESKTOP.md)
- Conventions: [`STYLE.md`](STYLE.md) · agent instructions: [`CLAUDE.md`](CLAUDE.md)

The logic is test-driven and hermetic — no PyObjC/Ollama/network needed to run the
suite. Native capture/OCR, real model backends, and the GUI shell are `# pragma: no
cover` (verified on a real Mac). Set up and run the gate:

```bash
python3 -m venv .venv && ./.venv/bin/pip install -r requirements-dev.txt
./ci/run.sh             # full suite + branch coverage (gate: 95%)
```
