"""Runtime metrics for the agent loop — counters + LLM tail latency.

Pure data; the loop increments fields and logs ``render()`` periodically and at
shutdown so an operator can see throughput, idle-skip ratio, backpressure drops,
and how slow the model is running.
"""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class Metrics:
    captures: int = 0        # observations perceived (sensor returned something)
    changed: int = 0         # routed as new content -> enqueued
    unchanged: int = 0       # idle heartbeats (no LLM)
    summarized: int = 0      # events written with a summary
    dropped: int = 0         # backpressure evictions (persisted, never silent)
    llm_calls: int = 0
    _latencies: list[float] = field(default_factory=list)

    def record_llm(self, seconds: float) -> None:
        self.llm_calls += 1
        self._latencies.append(seconds)

    def p95(self) -> float:
        """95th-percentile LLM latency (0.0 with no samples)."""
        if not self._latencies:
            return 0.0
        ordered = sorted(self._latencies)
        idx = min(len(ordered) - 1, round(0.95 * (len(ordered) - 1)))
        return ordered[idx]

    def render(self) -> str:
        return (f"captures={self.captures} changed={self.changed} "
                f"unchanged={self.unchanged} summarized={self.summarized} "
                f"dropped={self.dropped} llm_calls={self.llm_calls} "
                f"llm_p95={self.p95():.2f}s")
