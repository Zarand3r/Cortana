# Cortana Desktop App (menu-bar) — design & build

> Status: **runs from source; `.app` packaging is scaffolded but unverified in CI**
> (no GUI / py2app in the test environment). The testable core (`DesktopController`)
> is fully covered; the rumps/pywebview/asyncio shell is native and marked
> `# pragma: no cover`. Governed by `STYLE.md`.

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
   ├─ rumps menu:  ▶/⏸ Start/Stop Tracking · Open Chat… · Quit
   ├─ chat server thread   (chatapp.serve, memory-backed)  ──▶ 127.0.0.1:8808
   ├─ _TrackingService     (AgentLoop on a bg asyncio thread; start/stop = cancel)
   └─ "Open Chat…"         ──▶ subprocess: `cortana chat-window --url …` (pywebview)
                                     WKWebView window ─▶ 127.0.0.1:8808
```

- **`DesktopController`** (tested): the start/stop/toggle state machine the menu wires
  to; delegates real work to injected callables.
- **`_TrackingService`** (native): runs `AgentLoop` on a private event loop in a daemon
  thread; `stop()` cancels the run task — `AgentLoop.run`'s `finally` drains + closes.
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
python3 -m venv .venv && ./.venv/bin/pip install -r requirements-desktop.txt
# have a model ready: `ollama pull qwen2.5:7b-instruct`  (or pip install mlx-lm)
./.venv/bin/python -m cortana desktop
```

Grant **Screen Recording** (and **Accessibility** if using `--read-focused-text`) to
the launching app in System Settings → Privacy & Security, then restart it.

## Build the `.app` (scaffold — verify on a real Mac)

```bash
pip install -r requirements-desktop.txt
python setup.py py2app          # -> dist/Cortana.app
```

**Signing/notarization (required for TCC grants to persist across rebuilds):**
```bash
codesign --deep --force --options runtime \
  --sign "Developer ID Application: <you>" dist/Cortana.app
xcrun notarytool submit dist/Cortana.app --keychain-profile <profile> --wait
xcrun stapler staple dist/Cortana.app
```
Unsigned bundles still work, but the Screen Recording grant can reset on each rebuild.

### Known gotchas to verify
- **Resource paths in a bundle.** `chatapp.INDEX_PATH` and `Config.DEFAULT_CONFIG_PATH`
  resolve relative to the source tree; inside a py2app bundle the layout differs. The
  `setup.py` ships them as `resources`, but the lookup may need a bundle-aware path
  (`sys.frozen` / `NSBundle.resourcePath`). Verify the chat UI loads from the built app.
- **Screen Recording key.** We capture via `CGWindowListCreateImage` (no usage-string
  key needed). If we migrate to ScreenCaptureKit (Phase 7), add
  `NSScreenCaptureUsageDescription` to the plist.
- **Subprocess Python in a bundle.** `open_chat_window` spawns `sys.executable -m
  cortana chat-window`; in a frozen app `sys.executable` is the bundle binary — confirm
  the `chat-window` entry dispatches there, or embed the window differently.

## Out of scope (now)
- Auto-launch at login (the existing `dist/com.cortana.tracker.plist` + `install.sh`
  cover the headless daemon; a menu-bar LaunchAgent is a small follow-up).
- The guidance advisor's menu surface (built when the advisor lands).
- Windows/Linux packaging (Cortana is macOS-only by design).
