"""The desktop app's controller — the testable state machine behind the menu bar.
The rumps/pywebview shell and the asyncio tracking thread are native and marked
pragma: no cover; this pins the start/stop/toggle logic the menu wires to."""

from cortana.desktop import DesktopController


def _controller():
    calls = {"start": 0, "stop": 0, "chat": 0, "rec": 0}
    ctl = DesktopController(
        start_tracking=lambda: calls.__setitem__("start", calls["start"] + 1),
        stop_tracking=lambda: calls.__setitem__("stop", calls["stop"] + 1),
        open_chat=lambda: calls.__setitem__("chat", calls["chat"] + 1),
        show_recommendation=lambda: calls.__setitem__("rec", calls["rec"] + 1),
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


def test_recommend_invokes_callable():
    ctl, calls = _controller()
    ctl.recommend()
    assert calls["rec"] == 1


# --- ChatWindowManager: one window, reused (not one per click) ----------------

class _FakeProc:
    def __init__(self):
        self._alive = True

    def poll(self):
        return None if self._alive else 0      # None = still running

    def die(self):
        self._alive = False


def test_chat_window_reuses_single_instance():
    from cortana.desktop import ChatWindowManager
    spawned = []
    mgr = ChatWindowManager(lambda: spawned.append(_FakeProc()) or spawned[-1])

    assert mgr.open() is True                   # first click opens a window
    assert len(spawned) == 1
    assert mgr.open() is False                   # second click reuses it — no new window
    assert mgr.open() is False
    assert len(spawned) == 1                     # still just one window


def test_chat_window_respawns_after_close():
    from cortana.desktop import ChatWindowManager
    spawned = []
    mgr = ChatWindowManager(lambda: spawned.append(_FakeProc()) or spawned[-1])
    mgr.open()
    assert mgr.is_open() is True
    spawned[0].die()                             # user closed the window
    assert mgr.is_open() is False
    assert mgr.open() is True                     # reopening spawns a fresh one
    assert len(spawned) == 2


# --- recommendation_message (what "Get Recommendation" actually displays) -----

def test_recommendation_message_when_memory_has_activity(tmp_path):
    from cortana.backends import FakeLLMBackend
    from cortana.desktop import recommendation_message
    from cortana.memory import Memory
    from cortana.perception import Observation, Semantic
    mem = Memory(tmp_path / "m.db")
    o = Observation(ts="2026-07-04T09:00:00+00:00", app_name="Xcode", bundle_id="c",
                    window_title="w", ocr_text="debugging a crash", captured=True)
    mem.remember([o], Semantic(summary="Debugging a crash.", model="fake",
                               window_start_ts=o.ts, window_end_ts=o.ts))
    msg = recommendation_message(mem, FakeLLMBackend(response="Write a regression test."))
    assert msg == "Write a regression test."
    mem.close()


def test_recommendation_message_when_memory_empty(tmp_path):
    from cortana.backends import FakeLLMBackend
    from cortana.desktop import recommendation_message
    from cortana.memory import Memory
    mem = Memory(tmp_path / "empty.db")
    msg = recommendation_message(mem, FakeLLMBackend(response="ignored"))
    assert "start tracking" in msg.lower()      # explicit, never blank
    mem.close()


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
