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


class DesktopController:
    """The menu bar's brain: owns whether perception is running and delegates the
    real work to injected callables (so the state machine is testable without
    threads, PyObjC, or a GUI)."""

    def __init__(self, *, start_tracking: Callable[[], None],
                 stop_tracking: Callable[[], None],
                 open_chat: Callable[[], None], tracking: bool = False) -> None:
        self._start_tracking = start_tracking
        self._stop_tracking = stop_tracking
        self._open_chat = open_chat
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


def _serve_chat_background(cfg) -> None:  # pragma: no cover - native socket + thread
    """Run the memory-backed chat server in a daemon thread so the menu-bar app
    stays responsive."""
    import threading

    from cortana.backends import make_backend
    from cortana.chatapp import serve
    from cortana.cli import open_memory

    backend = make_backend(cfg.backend, cfg)
    memory = open_memory(cfg, check_same_thread=False)   # read-only recall
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
    """Launch the Cortana menu-bar app: serve chat, wire the menu to a
    DesktopController, run the rumps event loop."""
    import rumps

    _serve_chat_background(cfg)
    service = _TrackingService(cfg)
    url = f"http://{cfg.chat_host}:{cfg.chat_port}"
    controller = DesktopController(
        start_tracking=service.start,
        stop_tracking=service.stop,
        open_chat=lambda: open_chat_window(url),
    )

    class CortanaApp(rumps.App):
        def __init__(self):
            super().__init__("Cortana", quit_button="Quit Cortana")
            self.track_item = rumps.MenuItem(controller.tracking_label(),
                                             callback=self._toggle)
            self.menu = [self.track_item, rumps.MenuItem("Open Chat…", callback=self._chat)]

        def _toggle(self, _):
            controller.toggle()
            self.track_item.title = controller.tracking_label()

        def _chat(self, _):
            controller.open_chat()

    CortanaApp().run()
    return 0
