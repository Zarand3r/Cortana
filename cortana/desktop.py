"""Cortana as a macOS desktop app — the shell that hosts the whole agent.

Motivation: macOS TCC permissions (Screen Recording, Accessibility) attach to a
signed ``.app`` bundle, not to a script. Packaging Cortana as a menu-bar app means
one bundle owns those grants and runs perception + the chat server + inference
under a single permission boundary. Design & build/sign notes: docs/DESKTOP.md.

ONE on/off state: "Start Cortana" begins tracking AND opens the chat window;
"Stop" (or closing the window) stops both — never a mixed state.

Structure (STYLE.md — testable core, thin native shell):
  * ``DesktopController`` — the pure unified state machine (active ⇔ tracking +
    window; ``sync`` reconciles when the user closes the window). Unit-tested.
  * ``_TrackingService`` — runs the AgentLoop on a background asyncio thread,
    start/stop on demand. Native (threads + PyObjC sensor) -> pragma: no cover.
  * ``run_app`` — builds the rumps menu-bar app + serves chat; opens the chat window
    (pywebview) in a subprocess; a 2s rumps.Timer watches for window close. UI calls
    (rumps.alert) always run on the main thread via AppHelper.callAfter — AppKit
    crashes off-main. Native -> pragma: no cover.
"""

from __future__ import annotations

from typing import Callable

from cortana.advisor import recommend, recommend_from_observations


def recommendation_message(memory, backend, working_memory=None) -> str:
    """Compute the text to display for 'Get Recommendation' (testable; no GUI).
    Prefers short-term working memory (current activity) when it has any, else the
    long-term store. Always returns a message — never fails silently."""
    try:
        if working_memory is not None and len(working_memory):
            rec = recommend_from_observations(working_memory.recent(limit=12), backend)
        else:
            rec = recommend(memory, backend)
    except Exception as exc:  # noqa: BLE001 - model down/slow: show it, never fail silently
        return f"Couldn't generate a recommendation (is the model running?): {exc}"
    if not rec.basis:
        return ("Nothing to recommend yet — start tracking so I can learn what "
                "you're working on.")
    return rec.text or "No suggestion right now."


class DesktopController:
    """The menu bar's brain — ONE unified on/off state: ``active`` means tracking is
    running AND the chat window is open. Starting does both; stopping does both;
    closing the window (detected via ``sync``) stops tracking. No mixed states.
    Real work is delegated to injected callables so this is testable without
    threads, PyObjC, or a GUI."""

    def __init__(self, *, start_tracking: Callable[[], None],
                 stop_tracking: Callable[[], None],
                 open_chat: Callable[[], None],
                 close_chat: Callable[[], None],
                 show_recommendation: Callable[[], None]) -> None:
        self._start_tracking = start_tracking
        self._stop_tracking = stop_tracking
        self._open_chat = open_chat
        self._close_chat = close_chat
        self._show_recommendation = show_recommendation
        self.active = False

    def start(self) -> None:
        """Activate Cortana: begin perception AND open the chat window (idempotent)."""
        if not self.active:
            self._start_tracking()
            self._open_chat()
            self.active = True

    def stop(self) -> None:
        """Deactivate Cortana: stop perception AND close the chat window (idempotent)."""
        if self.active:
            self._stop_tracking()
            self._close_chat()
            self.active = False

    def toggle(self) -> None:
        self.stop() if self.active else self.start()

    def sync(self, *, window_open: bool) -> bool:
        """Reconcile with reality: if we're active but the user closed the chat
        window, stop everything (window closed ⇒ Cortana off). Returns True when
        the state changed (caller refreshes the menu)."""
        if self.active and not window_open:
            self._stop_tracking()
            self._close_chat()      # idempotent — the window is already gone
            self.active = False
            return True
        return False

    def recommend(self) -> None:
        """Surface a proactive recommendation from recent activity."""
        self._show_recommendation()

    def label(self) -> str:
        """Menu title reflecting the one state."""
        return "⏸  Stop Cortana" if self.active else "▶  Start Cortana"


class _TrackingService:  # pragma: no cover - threads + asyncio + native sensor
    """Runs the perception AgentLoop on a private asyncio event loop in a daemon
    thread; ``stop`` cancels it (AgentLoop.run drains + closes in its finally)."""

    def __init__(self, cfg, working_memory=None) -> None:
        self._cfg = cfg
        self._working = working_memory
        self._thread = None
        self._loop = None
        self._task = None
        self._memory = None
        self._prev = None      # a stopping thread that must fully close before a restart

    def start(self) -> None:
        import asyncio
        import threading
        if self._thread and self._thread.is_alive():
            return                                  # already running — don't spawn a 2nd writer
        # Create the loop synchronously BEFORE the thread starts, so a stop() that
        # arrives before _run has run can still schedule cancellation onto it
        # (fixes the start/stop race that could leave a zombie loop + 2nd SQLite writer).
        self._loop = asyncio.new_event_loop()
        self._thread = threading.Thread(target=self._run, name="cortana-track",
                                        daemon=True)
        self._thread.start()

    def _run(self) -> None:
        import asyncio

        from cortana.cli import make_loop
        # If a previous session is still draining/closing its writer, wait for it HERE
        # (on this background thread, never the menu thread) so we never open a second
        # SQLite writer — restart is safe without blocking the UI.
        if self._prev is not None:
            self._prev.join()
            self._prev = None
        asyncio.set_event_loop(self._loop)
        agent, self._memory = make_loop(self._cfg, working_memory=self._working)
        self._task = self._loop.create_task(agent.run(install_signal_handlers=False))
        try:
            self._loop.run_until_complete(self._task)
        except asyncio.CancelledError:
            pass
        finally:
            self._memory.close()
            self._loop.close()

    def _cancel(self) -> None:
        if self._task and not self._task.done():
            self._task.cancel()

    def stop(self) -> None:
        # Non-blocking: signal cancellation and drop stale session data, then return
        # immediately. The daemon thread drains the queue + closes the writer in
        # AgentLoop.run's finally; a later start() waits for it (inside _run). Callers
        # that must guarantee the drain finished (quit) call join() — off the menu
        # thread, so stopping never beachballs the menu bar (the join could take
        # several seconds if an LLM call is mid-flight).
        if self._loop:
            self._loop.call_soon_threadsafe(self._cancel)
        # Hand the draining thread to _prev so a restart waits for it, not the caller.
        if self._thread is not None:
            self._prev, self._thread, self._loop = self._thread, None, None
        if self._working is not None:
            self._working.clear()     # stale session data must not be served as "now"

    def join(self, timeout=None) -> None:
        """Block until the tracking thread (current or draining) has finished — used
        only on quit, always off the menu thread."""
        for t in (self._prev, self._thread):
            if t is not None:
                t.join(timeout)


def _serve_chat_background(cfg, backend, memory,
                           working_memory=None) -> None:  # pragma: no cover - native socket + thread
    """Run the memory-backed chat server in a daemon thread so the menu-bar app
    stays responsive. ``working_memory`` gives chat the live 'right now' view."""
    import threading

    from cortana.chatapp import serve
    threading.Thread(
        target=lambda: serve(backend, host=cfg.chat_host, port=cfg.chat_port,
                             system_prompt=cfg.chat_system_prompt, memory=memory,
                             working_memory=working_memory),
        name="cortana-chat", daemon=True,
    ).start()


class ChatWindowManager:
    """Ensures a *single* chat window. `open()` spawns one only when none is already
    running, so repeated 'Open Chat' clicks reuse the existing window (and its
    in-progress conversation) instead of piling up new, empty windows. ``spawn`` is
    injected (returns a process-like object with ``poll()``) so the logic is testable
    without a real subprocess."""

    def __init__(self, spawn) -> None:
        self._spawn = spawn
        self._proc = None

    def is_open(self) -> bool:
        return self._proc is not None and self._proc.poll() is None

    def open(self) -> bool:
        """Open the window if not already open. Returns True if a new window was
        spawned, False if an existing one was reused."""
        if self.is_open():
            return False
        self._proc = self._spawn()
        return True

    def close(self) -> None:
        """Close the window if it's open (idempotent — safe on an already-closed or
        never-opened window)."""
        if self.is_open():
            self._proc.terminate()
        self._proc = None


def _spawn_chat_window(url: str):  # pragma: no cover - native webview subprocess
    """Spawn the chat UI as a subprocess so its pywebview run loop doesn't collide
    with the menu bar's rumps run loop (both want the main thread — docs/DESKTOP.md)."""
    import subprocess
    import sys
    return subprocess.Popen([sys.executable, "-m", "cortana", "chat-window", "--url", url])


def run_chat_window(url: str) -> None:  # pragma: no cover - native webview
    """The subprocess entrypoint: a single pywebview window pointed at the local
    chat server."""
    import webview
    webview.create_window("Cortana", url, width=420, height=640)
    webview.start()


def run_app(cfg) -> int:  # pragma: no cover - native menu-bar app (rumps)
    """Launch the one Cortana app: background perception, a memory-backed chat
    window, and proactive recommendations — one bundle, one permission boundary."""
    import rumps

    from cortana.backends import make_backend
    from cortana.cli import open_memory

    from cortana.working_memory import WorkingMemory

    import threading

    from PyObjCTools import AppHelper

    backend = make_backend(cfg.backend, cfg)
    read_memory = open_memory(cfg, check_same_thread=False)   # shared read-only recall
    working = WorkingMemory(maxlen=cfg.working_memory_max)    # short-term, shared in-process
    _serve_chat_background(cfg, backend, read_memory, working)  # chat sees the live view
    service = _TrackingService(cfg, working_memory=working)   # the tracker fills it
    url = f"http://{cfg.chat_host}:{cfg.chat_port}"
    chat_window = ChatWindowManager(lambda: _spawn_chat_window(url))

    def _show_recommendation() -> None:
        # The LLM call is multi-second: run it on a worker thread so the menu bar
        # doesn't beachball — but the ALERT must be shown from the MAIN thread
        # (AppKit/NSAlert crashes off-main; this was the "Get Recommendation"
        # exception). AppHelper.callAfter marshals it back onto the Cocoa run loop.
        def work():
            msg = recommendation_message(read_memory, backend, working)
            AppHelper.callAfter(
                lambda: rumps.alert(title="Cortana — Recommendation", message=msg))
        threading.Thread(target=work, name="cortana-recommend", daemon=True).start()

    controller = DesktopController(
        start_tracking=service.start,
        stop_tracking=service.stop,
        open_chat=chat_window.open,          # single reused window
        close_chat=chat_window.close,
        show_recommendation=_show_recommendation,
    )

    class CortanaApp(rumps.App):
        def __init__(self):
            super().__init__("Cortana", quit_button=None)   # own quit for a clean drain
            self.toggle_item = rumps.MenuItem(controller.label(), callback=self._toggle)
            self.menu = [
                self.toggle_item,
                rumps.MenuItem("Get Recommendation", callback=self._recommend),
                None,
                rumps.MenuItem("Quit Cortana", callback=self._quit),
            ]
            # Watch for the user closing the chat window: window closed ⇒ Cortana
            # off (tracking stops too — never a mixed state).
            self._watcher = rumps.Timer(self._sync, 2)
            self._watcher.start()

        def _toggle(self, _):
            controller.toggle()
            self.toggle_item.title = controller.label()

        def _sync(self, _):
            if controller.sync(window_open=chat_window.is_open()):
                self.toggle_item.title = controller.label()

        def _recommend(self, _):
            controller.recommend()

        def _quit(self, _):
            # Clean shutdown: stop tracking + close the window, WAIT for the queue to
            # drain and the writer to close, then close the read connection and exit.
            # The drain (service.join) can take seconds, so run it on a worker thread
            # and marshal the actual quit back onto the main/Cocoa thread — otherwise
            # the menu bar beachballs while draining.
            def shutdown():
                controller.stop()                       # non-blocking cancel + close window
                service.join(timeout=cfg.batch_window + 30)   # wait for the drain here
                read_memory.close()                     # lock-guarded; safe vs live recall
                AppHelper.callAfter(rumps.quit_application)
            threading.Thread(target=shutdown, name="cortana-quit", daemon=True).start()

    CortanaApp().run()
    return 0
