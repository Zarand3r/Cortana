"""§A Golden-path integration — the Phase 1+2 spine.

perceive (fake observations) -> change-detect -> extract_meaning (fake) ->
remember -> recall-with-citation. Exercises P1, P2, P5, P6 in one path. Runs after
every change as the regression spine.
"""

from cortana.backends import FakeLLMBackend
from cortana.memory import Memory
from cortana.perception import (

    Observation,
    changed,
    content_hash,
    extract_meaning,
)

from conftest import counts



def _obs(ocr, *, ts, app, title="w"):
    return Observation(ts=ts, app_name=app, bundle_id="com." + app, window_title=title,
                       ocr_text=ocr, captured=True,
                       content_hash=content_hash(app, title, ocr))


def test_golden_path_perceive_remember_recall(tmp_path):
    # GIVEN a fixed perception stream: one budget screen, 4 identical duplicates,
    # then one distinct email screen.
    frames = [_obs("writing the Q2 budget", ts="2026-06-14T10:00:00+00:00",
                   app="Numbers")]
    frames += [_obs("writing the Q2 budget", ts=f"2026-06-14T10:0{i}:00+00:00",
                    app="Numbers") for i in range(1, 5)]      # 4 duplicates
    frames.append(_obs("reading personal email", ts="2026-06-14T10:30:00+00:00",
                       app="Mail"))

    backend = FakeLLMBackend(response="Working on finances and email.")
    mem = Memory(tmp_path / "agent.db")

    # WHEN the agent loop runs: change-detect gates meaning extraction + memory.
    prev_hash = None
    batches_remembered = 0
    for frame in frames:
        if changed(prev_hash, frame.content_hash):
            sem = extract_meaning([frame], backend, max_chars=6000)
            mem.remember([frame], sem)
            batches_remembered += 1
        prev_hash = frame.content_hash

    # THEN idle duplicates did not drive the LLM (P6): 6 frames -> 2 extractions.
    assert backend.calls == 2
    assert batches_remembered == 2

    # AND recall finds the budget memory with a citation (P5).
    hits = mem.recall(query="budget")
    assert len(hits) == 1
    assert hits[0]["app_name"] == "Numbers"            # where
    assert hits[0]["ts"] == "2026-06-14T10:00:00+00:00"  # when

    # AND the data model is sound: one summary per remembered batch (P1),
    # FTS mirrors context exactly (P2).
    c = counts(mem)
    assert c["summaries"] == batches_remembered
    assert c["context"] == batches_remembered
    assert c["context_fts"] == c["context"]
    mem.close()
