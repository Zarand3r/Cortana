# Cortana Desktop App (menu-bar) — design & build

> Status: **shipped and verified on device** (v0.1.0: build pipeline, first-run
> flow, live tracking, chat, and network posture all exercised on real hardware —
> `docs/PRODUCTION.md` has the checklist). Notarization still awaits a Developer
> ID cert. The testable core (`DesktopController`) is fully covered; the
> rumps/pywebview/asyncio shell is native and marked `# pragma: no cover`.
> Governed by `STYLE.md`.

## Why a desktop app

macOS **TCC permissions attach to a signed `.app` bundle, not to a script.** Run
from a terminal, Screen Recording/Accessibility are granted to *whatever terminal
launched Python* — fragile and confusing. As a bundle, Cortana:

- **Owns its own Screen Recording + Accessibility grants** (persist across restarts,
  survive launch-at-login).
- Runs **perception + the chat server + LLM inference in one process** it controls —
  the "privileges to run the server and inference too."

The desktop app is the **shell for the whole agent**, not just the chat page:
perception (writer) + chat (reader) + the future guidance advisor (reader) under one
permission boundary.

## Architecture

```
  Cortana.app  (menu-bar, LSUIElement — no Dock icon)
   ├─ rumps menu:  ▶/⏸ Start/Stop Cortana · Get Recommendation · Quit
   │      ONE state: Start = tracking ON + chat window OPEN; Stop (or the user
   │      closing the window — watched by a 2s rumps.Timer) = both OFF. Never mixed.
   ├─ chat server thread   (chatapp.serve, memory + live working-memory backed)
   ├─ _TrackingService     (AgentLoop on a bg asyncio thread; start/stop = cancel)
   ├─ "Get Recommendation" ──▶ worker thread computes recommendation_message(...),
   │      then AppHelper.callAfter → rumps.alert on the MAIN thread (NSAlert
   │      crashes off-main; alert not notification — notifications no-op unbundled)
   └─ chat window          ──▶ ChatWindowManager: one reused pywebview subprocess
                                     (`cortana chat-window --url …`) ─▶ 127.0.0.1:8808
                                     conversation persists server-side until quit
```

- **`DesktopController`** (tested): the start/stop/toggle state machine the menu wires
  to; delegates real work to injected callables.
- **`_TrackingService`** (native): runs `AgentLoop` on a private event loop in a daemon
  thread. `stop()` is **non-blocking** — it schedules cancellation and clears working
  memory, then returns; the daemon thread drains the queue + closes the writer in
  `AgentLoop.run`'s `finally`. A restart waits for the previous writer to finish
  *inside the new background thread* (`_prev.join()` in `_run`), so two SQLite writers
  never coexist and the menu thread never blocks. Quit calls `join()` on a **worker**
  thread to guarantee the drain, then `AppHelper.callAfter(rumps.quit_application)` —
  otherwise the join (up to `batch_window`+seconds if an LLM call is mid-flight) would
  beachball the menu bar.
- **chat server**: `chatapp.serve(..., memory=...)` in a daemon thread, so chat works
  whether or not tracking is on. Reads the same WAL SQLite the tracker writes.

### Event-loop decision (why the chat window is a subprocess)
Both `rumps` (menu bar) and `pywebview` (`webview.start()`) want to own the process
**main thread's** run loop — they can't share it. So the menu-bar app owns the rumps
loop, and **"Open Chat" launches the pywebview window as a separate process**
(`cortana chat-window`) pointed at the shared local server. Clean separation, and it
fits the "everything talks to 127.0.0.1" design. (Alternative considered: one browser
tab via `webbrowser.open` — simpler but not a native window.)

## Run from source (now)

```bash
./setup.sh          # or manually: python3 -m venv .venv && ./.venv/bin/pip install '.[desktop]'
# have a model ready: `ollama pull qwen2.5:7b-instruct`  (or pip install mlx-lm)
./.venv/bin/python -m cortana desktop
```

Grant **Screen Recording** to the launching app in System Settings → Privacy &
Security, then restart it.

## Build the `.app`

**Use the release script — do not run py2app by hand.** A raw
`python bundle/build_app.py py2app` produces a bundle **missing the mlx native
dylibs and the dist-info metadata** (post-build steps the script performs), and
`codesign --deep` does not sign Mach-O files under `Resources/` (notarization would
reject them) — the script signs every nested dylib inside-out instead.

```bash
export SIGN_ID="Developer ID Application: You (TEAMID)"   # omit -> unsigned local build
export NOTARY_PROFILE="cortana-notary"                     # omit -> signed, un-notarized
./scripts/build_release.sh                                 # -> dist/Cortana.dmg
```

Full pipeline + real-Mac verification checklist: [`PRODUCTION.md`](PRODUCTION.md).
Unsigned bundles still work locally, but the Screen Recording grant can reset on
each rebuild.

### Formerly-open gotchas (all resolved & verified on device)
- **Resource paths in a bundle** — chat UI loads via `importlib.resources`; config
  resolves through bundle-aware candidates (`config._default_config_candidates`).
- **Screen Recording** — requested via `CGRequestScreenCaptureAccess` (ctypes;
  pyobjc doesn't bind it — REVIEW.md #45). No plist usage-string needed for
  `CGWindowListCreateImage`.
- **Subprocess in a bundle** — `sys.executable` is py2app's plain interpreter, so
  the spawn exports the parent's `sys.path` via PYTHONPATH (REVIEW.md #35).

## Out of scope (now)
- Auto-launch at login (a LaunchAgent for the menu-bar app is a small follow-up;
  the old headless-daemon path was removed — the menu-bar app is the one product).
- Windows/Linux packaging (Cortana is macOS-only by design).
