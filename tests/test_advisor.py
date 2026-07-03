"""The guidance advisor: proactive recommendations grounded in recent activity."""

from cortana.advisor import Recommendation, build_recommendation_prompt, recommend
from cortana.backends import FakeLLMBackend
from cortana.memory import Memory
from cortana.perception import Observation, Semantic


def _seed(tmp_path):
    mem = Memory(tmp_path / "m.db")
    for i, (app, ocr, summ) in enumerate([
        ("Visual Studio Code", "def run(): ...", "Editing the agent loop."),
        ("Google Chrome", "asyncio docs", "Reading asyncio documentation."),
    ]):
        ts = f"2026-07-03T09:0{i}:00+00:00"
        o = Observation(ts=ts, app_name=app, bundle_id="c", window_title="w",
                        ocr_text=ocr, captured=True)
        mem.remember([o], Semantic(summary=summ, model="fake",
                                   window_start_ts=ts, window_end_ts=ts))
    return mem


def test_build_prompt_includes_activity():
    prompt = build_recommendation_prompt([
        {"ts": "T", "app_name": "Xcode", "summary": "debugging a crash", "ocr_text": ""},
    ])
    assert "Xcode" in prompt and "debugging a crash" in prompt


def test_build_prompt_handles_no_activity():
    assert "no recent activity" in build_recommendation_prompt([]).lower()


def test_recommend_returns_grounded_recommendation(tmp_path):
    mem = _seed(tmp_path)
    be = FakeLLMBackend(response="Consider writing tests for the agent loop.")
    rec = recommend(mem, be, limit=12)
    assert isinstance(rec, Recommendation)
    assert rec.text == "Consider writing tests for the agent loop."
    assert len(rec.basis) == 2                 # drew on the recent activity
    assert be.calls == 1
    mem.close()


def test_recommend_on_empty_memory(tmp_path):
    mem = Memory(tmp_path / "empty.db")
    be = FakeLLMBackend(response="Not enough activity yet.")
    rec = recommend(mem, be)
    assert rec.text == "Not enough activity yet."
    assert rec.basis == []
    mem.close()


def test_recommend_cli(tmp_path, capsys):
    from cortana.cli import main
    db = tmp_path / "m.db"
    _seed_at(db)
    rc = main(["recommend", "--backend", "fake", "--db", str(db)])
    assert rc == 0
    out = capsys.readouterr().out
    assert "fake summary" in out
    assert "based on:" in out          # citations printed


def _seed_at(db):
    mem = Memory(db)
    o = Observation(ts="2026-07-03T09:00:00+00:00", app_name="Xcode", bundle_id="c",
                    window_title="w", ocr_text="debugging", captured=True)
    mem.remember([o], Semantic(summary="Debugging a crash.", model="fake",
                               window_start_ts=o.ts, window_end_ts=o.ts))
    mem.close()
