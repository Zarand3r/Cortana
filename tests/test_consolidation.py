"""Reflection / consolidation: summarize recent episodes into a durable higher-level
'reflection' (semantic memory), the way generative agents synthesize insights."""

from cortana.backends import FakeLLMBackend
from cortana.consolidation import Reflection, build_digest_prompt, consolidate
from cortana.memory import Memory
from cortana.perception import Observation, Semantic


def _seed(tmp_path):
    mem = Memory(tmp_path / "m.db")
    for i, (app, ocr) in enumerate([("Visual Studio Code", "editing agent.py"),
                                    ("Google Chrome", "reading asyncio docs")]):
        ts = f"2026-07-05T09:0{i}:00+00:00"
        mem.remember([Observation(ts=ts, app_name=app, bundle_id="c", window_title="w",
                                  ocr_text=ocr, captured=True)],
                     Semantic(summary=f"{app} work", model="fake",
                              window_start_ts=ts, window_end_ts=ts))
    return mem


def test_build_digest_prompt_includes_activity():
    p = build_digest_prompt([{"ts": "T", "app_name": "Xcode",
                              "summary": "debugging a crash", "ocr_text": ""}])
    assert "Xcode" in p and "debugging a crash" in p


def test_consolidate_stores_a_reflection(tmp_path):
    mem = _seed(tmp_path)
    be = FakeLLMBackend(response="Worked on the Cortana agent loop and read asyncio docs.")
    ref = consolidate(mem, be)
    assert isinstance(ref, Reflection)
    assert ref.text.startswith("Worked on")
    assert ref.basis_count == 2
    assert ref.period_start <= ref.period_end
    stored = mem.recent_reflections()
    assert stored and stored[0]["text"] == ref.text
    mem.close()


def test_consolidate_empty_memory_stores_nothing(tmp_path):
    mem = Memory(tmp_path / "empty.db")
    ref = consolidate(mem, FakeLLMBackend(response="ignored"))
    assert ref.basis_count == 0
    assert mem.recent_reflections() == []
    mem.close()


def test_digest_cli(tmp_path, capsys):
    from cortana.cli import main
    _seed(tmp_path).close()                            # builds tmp_path/m.db
    rc = main(["digest", "--backend", "fake", "--db", str(tmp_path / "m.db")])
    assert rc == 0
    assert "episodes" in capsys.readouterr().out       # prints the reflection + span

