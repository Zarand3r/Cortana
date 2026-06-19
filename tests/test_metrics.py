"""Phase 6: runtime metrics — counters + LLM latency percentile + a log line."""

from cortana.metrics import Metrics


def test_counters_start_at_zero():
    m = Metrics()
    assert m.captures == m.changed == m.unchanged == m.summarized == m.dropped == 0
    assert m.llm_calls == 0


def test_record_llm_tracks_calls_and_latency():
    m = Metrics()
    for s in (0.1, 0.2, 0.3):
        m.record_llm(s)
    assert m.llm_calls == 3
    assert m.p95() >= 0.3 - 1e-9      # p95 of a small set lands near the top


def test_p95_is_zero_with_no_samples():
    assert Metrics().p95() == 0.0


def test_p95_picks_the_95th_percentile():
    m = Metrics()
    for i in range(1, 101):           # latencies 0.01 .. 1.00
        m.record_llm(i / 100)
    assert abs(m.p95() - 0.95) < 0.02


def test_render_includes_all_fields():
    m = Metrics()
    m.captures = 10
    m.changed = 6
    m.unchanged = 4
    m.summarized = 6
    m.dropped = 1
    m.record_llm(0.5)
    line = m.render()
    for key in ("captures=10", "changed=6", "unchanged=4", "summarized=6",
                "dropped=1", "llm_calls=1", "llm_p95="):
        assert key in line
