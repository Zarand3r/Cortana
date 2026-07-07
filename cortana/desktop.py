"""Cortana as a macOS desktop app — the shell that hosts the whole agent.

Motivation: macOS TCC permissions (Screen Recording, Accessibility) attach to a
signed ``.app`` bundle, not to a script. Packaging Cortana as a menu-bar app means
one bundle owns those grants and runs perception + the chat server + inference
under a single permission boundary. Design & build/sign notes: docs/DESKTOP.md.

Structure (STYLE.md — testable core, thin native shell):
  * ``DesktopController`` — the pure start/stop/toggle state machine the menu wires
    to. Fully unit-tested.
  * ``_TrackingService`` — runs the AgentLoop on a background asyncio thread,
    start/stop on demand. Native (threads + PyObjC sensor) -> pragma: no cover.
  * ``run_app`` — builds the rumps menu-bar app + serves chat; opens the chat window
    (pywebview) in a subprocess. Native -> pragma: no cover.
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
    """The menu bar's brain: owns whether perception is running and delegates the
    real work to injected callables (so the state machine is testable without
    threads, PyObjC, or a GUI)."""

    def __init__(self, *, start_tracking: Callable[[], None],
                 stop_tracking: Callable[[], None],
                 open_chat: Callable[[], None],
                 show_recommendation: Callable[[], None],
                 tracking: bool = False) -> None:
        self._start_tracking = start_tracking
        self._stop_tracking = stop_tracking
        self._open_chat = open_chat
        self._show_recommendation = show_recommendation
        self.tracking = tracking

    def start(self) -> None:
        """Begin perception if not already running (idempotent)."""
        if not self.tracking:
            self._start_tracking()
            self.tracking = True

    def stop(self) -> None:
        """Stop perception if running (idempotent)."""
        if self.tracking:
            self._stop_tracking()
            self.tracking = False

    def toggle(self) -> None:
        self.stop() if self.tracking else self.start()

    def open_chat(self) -> None:
        self._open_chat()

    def recommend(self) -> None:
        """Surface a proactive recommendation from recent activity."""
        self._show_recommendation()

    def tracking_label(self) -> str:
        """Menu title reflecting current state."""
        return "⏸  Stop Tracking" if self.tracking else "▶  Start Tracking"


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
        # Schedule cancellation on the loop thread; queued before the loop starts, it
        # runs once _run has created the task — so a fast Start→Stop still cancels.
        if self._loop:
            self._loop.call_soon_threadsafe(self._cancel)
        if self._thread:
            self._thread.join(timeout=self._cfg.batch_window + 30)
        if self._working is not None:
            self._working.clear()     # stale session data must not be served as "now"


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

    backend = make_backend(cfg.backend, cfg)
    read_memory = open_memory(cfg, check_same_thread=False)   # shared read-only recall
    working = WorkingMemory(maxlen=cfg.working_memory_max)    # short-term, shared in-process
    _serve_chat_background(cfg, backend, read_memory, working)  # chat sees the live view
    service = _TrackingService(cfg, working_memory=working)   # the tracker fills it
    url = f"http://{cfg.chat_host}:{cfg.chat_port}"
    chat_window = ChatWindowManager(lambda: _spawn_chat_window(url))

    def _show_recommendation() -> None:
        # The LLM call is multi-second: run it on a worker thread so the menu bar
        # doesn't beachball, and show the alert from the follow-up.
        def work():
            msg = recommendation_message(read_memory, backend, working)
            rumps.alert(title="Cortana — Recommendation", message=msg)
        threading.Thread(target=work, name="cortana-recommend", daemon=True).start()

    controller = DesktopController(
        start_tracking=service.start,
        stop_tracking=service.stop,
        open_chat=chat_window.open,          # single reused window, not one-per-click
        show_recommendation=_show_recommendation,
    )

    class CortanaApp(rumps.App):
        def __init__(self):
            super().__init__("Cortana", quit_button=None)   # own quit for a clean drain
            self.track_item = rumps.MenuItem(controller.tracking_label(),
                                             callback=self._toggle)
            self.menu = [
                self.track_item,
                rumps.MenuItem("Open Chat…", callback=self._chat),
                rumps.MenuItem("Get Recommendation", callback=self._recommend),
                None,
                rumps.MenuItem("Quit Cortana", callback=self._quit),
            ]

        def _toggle(self, _):
            controller.toggle()
            self.track_item.title = controller.tracking_label()

        def _chat(self, _):
            controller.open_chat()

        def _recommend(self, _):
            controller.recommend()

        def _quit(self, _):
            # Clean shutdown: stop tracking (drains the queue, closes the writer),
            # close the read connection, then exit — no observations lost mid-batch.
            controller.stop()
            read_memory.close()
            rumps.quit_application()

    CortanaApp().run()
    return 0
