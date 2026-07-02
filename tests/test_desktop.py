"""The desktop app's controller — the testable state machine behind the menu bar.
The rumps/pywebview shell and the asyncio tracking thread are native and marked
pragma: no cover; this pins the start/stop/toggle logic the menu wires to."""

from cortana.desktop import DesktopController


def _controller():
    calls = {"start": 0, "stop": 0, "chat": 0}
    ctl = DesktopController(
        start_tracking=lambda: calls.__setitem__("start", calls["start"] + 1),
        stop_tracking=lambda: calls.__setitem__("stop", calls["stop"] + 1),
        open_chat=lambda: calls.__setitem__("chat", calls["chat"] + 1),
    )
    return ctl, calls


def test_starts_not_tracking():
    ctl, _ = _controller()
    assert ctl.tracking is False


def test_start_sets_tracking_and_invokes_callable():
    ctl, calls = _controller()
    ctl.start()
    assert ctl.tracking is True
    assert calls["start"] == 1


def test_start_is_idempotent():
    ctl, calls = _controller()
    ctl.start()
    ctl.start()
    assert calls["start"] == 1          # already running -> no second start


def test_stop_clears_tracking_and_invokes_callable():
    ctl, calls = _controller()
    ctl.start()
    ctl.stop()
    assert ctl.tracking is False
    assert calls["stop"] == 1


def test_stop_is_idempotent_when_not_tracking():
    ctl, calls = _controller()
    ctl.stop()
    assert calls["stop"] == 0


def test_toggle_flips_state():
    ctl, calls = _controller()
    ctl.toggle()
    assert ctl.tracking is True and calls["start"] == 1
    ctl.toggle()
    assert ctl.tracking is False and calls["stop"] == 1


def test_open_chat_invokes_callable():
    ctl, calls = _controller()
    ctl.open_chat()
    assert calls["chat"] == 1


def test_tracking_label_reflects_state():
    ctl, _ = _controller()
    assert "Start" in ctl.tracking_label()
    ctl.start()
    assert "Stop" in ctl.tracking_label()


# --- CLI surface (parser only; the native launch is pragma: no cover) --------

def test_cli_accepts_desktop_subcommand():
    from cortana.cli import build_config
    cfg = build_config(["desktop", "--port", "9000"])
    assert cfg.chat_port == 9000


def test_cli_accepts_chat_window_subcommand():
    from cortana.cli import _parser
    args = _parser().parse_args(["chat-window", "--url", "http://127.0.0.1:8808"])
    assert args.command == "chat-window"
    assert args.url == "http://127.0.0.1:8808"
