"""Working (short-term) memory: a bounded, thread-safe, in-memory rolling buffer of
the most recent observations — the live 'what am I doing now' view, distinct from
the durable SQLite long-term store."""

import threading

from cortana.perception import Observation
from cortana.working_memory import WorkingMemory


def _obs(ocr, *, ts, app="Notes"):
    return Observation(ts=ts, app_name=app, bundle_id="c", window_title="w",
                       ocr_text=ocr, captured=True)


def test_add_and_recent_are_chronological():
    wm = WorkingMemory(maxlen=10)
    for i in range(3):
        wm.add(_obs(f"screen {i}", ts=f"2026-07-04T10:0{i}:00+00:00"))
    recent = wm.recent()
    assert [o.ocr_text for o in recent] == ["screen 0", "screen 1", "screen 2"]
    assert len(wm) == 3


def test_bounded_evicts_oldest():
    wm = WorkingMemory(maxlen=2)
    for i in range(5):
        wm.add(_obs(f"s{i}", ts=f"2026-07-04T10:0{i}:00+00:00"))
    recent = wm.recent()
    assert [o.ocr_text for o in recent] == ["s3", "s4"]      # only the last 2 kept
    assert len(wm) == 2


def test_recent_limit_returns_last_n():
    wm = WorkingMemory(maxlen=10)
    for i in range(5):
        wm.add(_obs(f"s{i}", ts=f"2026-07-04T10:0{i}:00+00:00"))
    assert [o.ocr_text for o in wm.recent(limit=2)] == ["s3", "s4"]


def test_recent_limit_zero_returns_none_not_everything():
    wm = WorkingMemory(maxlen=10)
    for i in range(3):
        wm.add(_obs(f"s{i}", ts=f"2026-07-04T10:0{i}:00+00:00"))
    assert wm.recent(limit=0) == []          # NOT items[-0:] == whole list
    assert len(wm.recent(limit=None)) == 3


def test_recent_returns_a_copy_not_the_live_buffer():
    wm = WorkingMemory(maxlen=10)
    wm.add(_obs("a", ts="2026-07-04T10:00:00+00:00"))
    snap = wm.recent()
    wm.add(_obs("b", ts="2026-07-04T10:01:00+00:00"))
    assert [o.ocr_text for o in snap] == ["a"]              # snapshot is stable


def test_thread_safe_concurrent_adds():
    wm = WorkingMemory(maxlen=1000)

    def worker(base):
        for i in range(100):
            wm.add(_obs(f"{base}-{i}", ts=f"2026-07-04T10:00:{i:02d}+00:00"))

    threads = [threading.Thread(target=worker, args=(b,)) for b in range(5)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert len(wm) == 500       # no lost/corrupted appends under concurrency
