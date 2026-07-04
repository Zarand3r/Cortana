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

from cortana.advisor import recommend


def recommendation_message(memory, backend) -> str:
    """Compute the text to display for 'Get Recommendation' (testable; no GUI).
    Friendly, explicit message when there's no activity yet — so the button always
    says *something* rather than appearing to do nothing."""
    rec = recommend(memory, backend)
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

    def __init__(self, cfg) -> None:
        self._cfg = cfg
        self._thread = None
        self._loop = None
        self._task = None
        self._memory = None

    def start(self) -> None:
        import threading
        self._thread = threading.Thread(target=self._run, name="cortana-track",
                                        daemon=True)
        self._thread.start()

    def _run(self) -> None:
        import asyncio

        from cortana.cli import make_loop
        self._loop = asyncio.new_event_loop()
        asyncio.set_event_loop(self._loop)
        agent, self._memory = make_loop(self._cfg)
        self._task = self._loop.create_task(agent.run(install_signal_handlers=False))
        try:
            self._loop.run_until_complete(self._task)
        except asyncio.CancelledError:
            pass
        finally:
            self._memory.close()
            self._loop.close()

    def stop(self) -> None:
        if self._loop and self._task and not self._task.done():
            self._loop.call_soon_threadsafe(self._task.cancel)
        if self._thread:
            self._thread.join(timeout=self._cfg.batch_window + 30)


def _serve_chat_background(cfg, backend, memory) -> None:  # pragma: no cover - native socket + thread
    """Run the memory-backed chat server in a daemon thread so the menu-bar app
    stays responsive."""
    import threading

    from cortana.chatapp import serve
    threading.Thread(
        target=lambda: serve(backend, host=cfg.chat_host, port=cfg.chat_port,
                             system_prompt=cfg.chat_system_prompt, memory=memory),
        name="cortana-chat", daemon=True,
    ).start()


def open_chat_window(url: str) -> None:  # pragma: no cover - native webview subprocess
    """Open the chat UI in a native window. Launched as a subprocess so its pywebview
    run loop doesn't collide with the menu bar's rumps run loop (both want the main
    thread — see docs/DESKTOP.md §event-loops)."""
    import subprocess
    import sys
    subprocess.Popen([sys.executable, "-m", "cortana", "chat-window", "--url", url])


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

    backend = make_backend(cfg.backend, cfg)
    read_memory = open_memory(cfg, check_same_thread=False)   # shared read-only recall
    _serve_chat_background(cfg, backend, read_memory)
    service = _TrackingService(cfg)
    url = f"http://{cfg.chat_host}:{cfg.chat_port}"

    def _show_recommendation() -> None:
        # rumps.alert is a modal dialog that works when run from source; notifications
        # silently no-op unless the app is a bundled/signed .app.
        rumps.alert(title="Cortana — Recommendation",
                    message=recommendation_message(read_memory, backend))

    controller = DesktopController(
        start_tracking=service.start,
        stop_tracking=service.stop,
        open_chat=lambda: open_chat_window(url),
        show_recommendation=_show_recommendation,
    )

    class CortanaApp(rumps.App):
        def __init__(self):
            super().__init__("Cortana", quit_button="Quit Cortana")
            self.track_item = rumps.MenuItem(controller.tracking_label(),
                                             callback=self._toggle)
            self.menu = [
                self.track_item,
                rumps.MenuItem("Open Chat…", callback=self._chat),
                rumps.MenuItem("Get Recommendation", callback=self._recommend),
            ]

        def _toggle(self, _):
            controller.toggle()
            self.track_item.title = controller.tracking_label()

        def _chat(self, _):
            controller.open_chat()

        def _recommend(self, _):
            controller.recommend()

    CortanaApp().run()
    return 0
