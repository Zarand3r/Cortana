"""The desktop app's controller — the testable state machine behind the menu bar.
ONE unified on/off state: active ⇔ (tracking running AND chat window open). No
mixed states — closing the window stops tracking; the toggle controls everything.
The rumps/pywebview shell and the asyncio tracking thread are native/pragma."""

from cortana.desktop import DesktopController


def _controller():
    calls = {"start": 0, "stop": 0, "open": 0, "close": 0, "rec": 0}
    ctl = DesktopController(
        start_tracking=lambda: calls.__setitem__("start", calls["start"] + 1),
        stop_tracking=lambda: calls.__setitem__("stop", calls["stop"] + 1),
        open_chat=lambda: calls.__setitem__("open", calls["open"] + 1),
        close_chat=lambda: calls.__setitem__("close", calls["close"] + 1),
        show_recommendation=lambda: calls.__setitem__("rec", calls["rec"] + 1),
    )
    return ctl, calls


def test_starts_inactive():
    ctl, _ = _controller()
    assert ctl.active is False


def test_start_activates_both_tracking_and_chat():
    ctl, calls = _controller()
    ctl.start()
    assert ctl.active is True
    assert calls["start"] == 1 and calls["open"] == 1     # one state, both effects


def test_start_is_idempotent():
    ctl, calls = _controller()
    ctl.start()
    ctl.start()
    assert calls["start"] == 1 and calls["open"] == 1


def test_stop_deactivates_both():
    ctl, calls = _controller()
    ctl.start()
    ctl.stop()
    assert ctl.active is False
    assert calls["stop"] == 1 and calls["close"] == 1


def test_stop_is_idempotent_when_inactive():
    ctl, calls = _controller()
    ctl.stop()
    assert calls["stop"] == 0 and calls["close"] == 0


def test_toggle_flips_the_one_state():
    ctl, calls = _controller()
    ctl.toggle()
    assert ctl.active is True and calls["start"] == 1 and calls["open"] == 1
    ctl.toggle()
    assert ctl.active is False and calls["stop"] == 1 and calls["close"] == 1


def test_sync_stops_tracking_when_window_was_closed():
    # The user closed the chat window (red X): active must become False and
    # tracking must stop — no mixed state where tracking runs windowless.
    ctl, calls = _controller()
    ctl.start()
    changed = ctl.sync(window_open=False)
    assert changed is True
    assert ctl.active is False
    assert calls["stop"] == 1


def test_sync_noop_when_state_matches_window():
    ctl, calls = _controller()
    ctl.start()
    assert ctl.sync(window_open=True) is False      # active + window open: fine
    ctl.stop()
    assert ctl.sync(window_open=False) is False     # inactive + closed: fine
    assert calls["stop"] == 1                        # only the explicit stop


def test_label_reflects_the_one_state():
    ctl, _ = _controller()
    assert "Start" in ctl.label()
    ctl.start()
    assert "Stop" in ctl.label()


def test_sync_surfaces_a_dead_tracking_thread():
    # The perception loop crashed while the window is still open: sync must stop
    # everything and raise a visible crash badge — never keep showing "on".
    calls = {"start": 0, "stop": 0, "open": 0, "close": 0, "rec": 0}
    healthy = {"ok": True}
    ctl = DesktopController(
        start_tracking=lambda: calls.__setitem__("start", calls["start"] + 1),
        stop_tracking=lambda: calls.__setitem__("stop", calls["stop"] + 1),
        open_chat=lambda: calls.__setitem__("open", calls["open"] + 1),
        close_chat=lambda: calls.__setitem__("close", calls["close"] + 1),
        show_recommendation=lambda: None,
        tracking_healthy=lambda: healthy["ok"],
    )
    ctl.start()
    healthy["ok"] = False                       # the tracking thread dies
    changed = ctl.sync(window_open=True)         # window still open, but tracker dead
    assert changed is True
    assert ctl.active is False and ctl.failed is True
    assert calls["stop"] == 1
    assert "error" in ctl.label().lower()        # visible crash badge
    ctl.start()                                  # restarting clears the badge
    assert ctl.failed is False and "Stop" in ctl.label()


def test_sync_healthy_tracker_is_left_running():
    ctl, calls = _controller()                   # default tracking_healthy = always True
    ctl.start()
    assert ctl.sync(window_open=True) is False    # alive + window open: no change
    assert ctl.active is True and ctl.failed is False


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


def test_chat_window_close_terminates_and_is_idempotent():
    from cortana.desktop import ChatWindowManager

    class _KillableProc(_FakeProc):
        def __init__(self):
            super().__init__()
            self.terminated = 0

        def terminate(self):
            self.terminated += 1
            self.die()

    spawned = []
    mgr = ChatWindowManager(lambda: spawned.append(_KillableProc()) or spawned[-1])
    mgr.close()                                   # close before open: no-op
    mgr.open()
    mgr.close()                                   # closes the live window
    assert spawned[0].terminated == 1
    assert mgr.is_open() is False
    mgr.close()                                   # already closed: no second terminate
    assert spawned[0].terminated == 1


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


def test_recommendation_message_prefers_working_memory(tmp_path):
    # With working memory populated, the recommendation comes from current activity
    # (no DB query needed) — the point of short-term memory.
    from cortana.backends import FakeLLMBackend
    from cortana.desktop import recommendation_message
    from cortana.memory import Memory
    from cortana.perception import Observation
    from cortana.working_memory import WorkingMemory
    wm = WorkingMemory()
    wm.add(Observation(ts="2026-07-04T09:00:00+00:00", app_name="Xcode", bundle_id="c",
                       window_title="w", ocr_text="debugging", captured=True))
    mem = Memory(tmp_path / "empty.db")          # DB empty; WM must be used instead
    msg = recommendation_message(mem, FakeLLMBackend(response="Fix the crash."), wm)
    assert msg == "Fix the crash."
    mem.close()


def test_recommendation_message_shows_error_instead_of_silence(tmp_path):
    from cortana.desktop import recommendation_message
    from cortana.memory import Memory
    from cortana.perception import Observation
    from cortana.working_memory import WorkingMemory

    class Broken:
        model = "x"
        def generate(self, prompt):
            raise RuntimeError("model offline")
    wm = WorkingMemory()
    wm.add(Observation(ts="t", app_name="A", bundle_id="c", window_title="w",
                       ocr_text="x", captured=True))
    mem = Memory(tmp_path / "m.db")
    try:
        msg = recommendation_message(mem, Broken(), wm)
    finally:
        mem.close()
    assert "couldn't generate" in msg.lower()    # visible failure, not a dead button


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
