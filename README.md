# Cortana — the AI assistant that actually knows what you're doing

Every AI assistant you've used starts every conversation blind. You paste context,
re-explain your project, describe what's on your screen. **Cortana never asks —
it already knows.** It lives in your Mac's menu bar, sees what you see, and builds
a private, searchable memory of your working life:

- *"What was I working on this morning before the meeting?"*
- *"Where did I see that error about the missing dylib?"*
- *"What should I do next?"* → a concrete recommendation, grounded in what you've
  actually been doing — not generic advice.

**And nothing — not one byte — ever leaves your machine.** That's not a policy
promise, it's architecture: the OCR is Apple Vision (on-device), the LLM is local
(bundled MLX or Ollama), memory is a SQLite file on your disk, and the only open
socket is the localhost chat server — verified with `lsof`, enforced in code, and
covered by the test suite. No cloud, no account, no telemetry. The tradeoff every
cloud assistant forces — *context vs privacy* — is the entire point of this app:
you get **total context and total privacy**, because the assistant moves to your
data instead of your data moving to it.

How it works, as one menu-bar app:

- **Perceives** — captures the screen every second, OCRs it on-device, and distills
  activity with a local LLM. Idle screens cost nothing (change-detection gate);
  password managers are never captured; secrets are redacted before storage.
- **Remembers** — tiered memory: a live "right now" buffer, searchable episodic +
  semantic long-term store (keyword + vector hybrid recall), and daily consolidated
  reflections — bounded by retention caps you control.
- **Chats** — a ChatGPT-style window where answers cite the actual timestamps and
  apps they came from. Ask "what am I doing right now" and it answers from the
  last few seconds.
- **Recommends** — one click for a next-action suggestion grounded in your recent
  activity.

> ⚠️ **Cortana's memory is a searchable log of what's been on your screen.** It skips
> password fields + known password managers and **redacts high-confidence secrets**
> (private keys, API tokens, Luhn-valid cards, SSNs) before writing. It never logs
> keystrokes. At rest it relies on **FileVault**; SQLCipher is a later opt-in. Treat
> the `.db` as sensitive.

---

## Download the app

Grab **`Cortana.dmg`** from the [latest release](../../releases/latest), drag
**Cortana.app** to Applications, and launch it — it lives in the **menu bar**
(top-right). First launch on Apple Silicon:

1. If macOS warns about an unidentified developer: **right-click → Open** (once).
   *(Releases aren't notarized yet — a Developer ID build removes this step.)*
2. Grant **Screen Recording** when prompted, then relaunch Cortana.
3. If the local model isn't cached yet, the status line shows the one-time
   ~4 GB download; after that Cortana is **100% offline**.

Then: menu-bar icon → **Start Cortana** → the chat window opens and tracking runs.

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
