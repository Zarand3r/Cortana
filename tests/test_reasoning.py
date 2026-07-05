"""Phase 4: recall & reasoning. Deterministic via a fake backend — we assert the
*retrieved set* (citations) and that retrieved context reaches the prompt, never the
(nondeterministic) prose."""

from cortana.backends import FakeLLMBackend
from cortana.memory import Memory
from cortana.perception import Observation, Semantic
from cortana.reasoning import Answer, build_answer_prompt, question_to_fts, reason


def _obs(ocr, *, app, ts):
    return Observation(ts=ts, app_name=app, bundle_id="com." + app, window_title="w",
                       ocr_text=ocr, captured=True)


def _sem(summary, ts):
    return Semantic(summary=summary, model="fake", window_start_ts=ts, window_end_ts=ts)


class CapturingBackend(FakeLLMBackend):
    def generate(self, prompt):
        self.prompt = prompt
        return super().generate(prompt)


def _seed(tmp_path):
    mem = Memory(tmp_path / "m.db", check_same_thread=False)
    mem.remember([_obs("quarterly budget figures", app="Numbers",
                       ts="2026-06-17T09:00:00+00:00")], _sem("Reviewing the Q2 budget.",
                                                              "2026-06-17T09:00:00+00:00"))
    mem.remember([_obs("personal email inbox", app="Mail",
                       ts="2026-06-17T12:00:00+00:00")], _sem("Reading personal email.",
                                                             "2026-06-17T12:00:00+00:00"))
    return mem


# --- question -> FTS terms -------------------------------------------------- #

def test_question_to_fts_keeps_content_words_drops_stopwords():
    q = question_to_fts("What was I doing with the budget spreadsheet?")
    terms = q.split(" OR ")
    assert "budget" in terms and "spreadsheet" in terms
    assert "what" not in terms and "the" not in terms


def test_question_to_fts_none_when_only_stopwords():
    assert question_to_fts("what was i doing?") is None


def test_present_tense_question_returns_recent_not_relevance(tmp_path):
    # "what am I doing" is all stop-words -> even with an embedder, use recency, not a
    # relevance ranking that could surface a stale-but-similar memory.
    from cortana.embeddings import FakeEmbedder
    mem = _seed(tmp_path)   # budget @09:00, email @12:00 (email is most recent)
    ans = reason("what am I doing", mem, CapturingBackend(), embedder=FakeEmbedder())
    assert ans.citations and ans.citations[0]["app_name"] == "Mail"   # most recent
    mem.close()


# --- reason() --------------------------------------------------------------- #

def test_reason_retrieves_only_relevant_memories_as_citations(tmp_path):
    mem = _seed(tmp_path)
    be = FakeLLMBackend(response="You reviewed the Q2 budget in Numbers.")
    ans = reason("what budget work did i do?", mem, be)
    assert isinstance(ans, Answer)
    assert ans.text == "You reviewed the Q2 budget in Numbers."
    assert be.calls == 1
    apps = {c["app_name"] for c in ans.citations}
    assert "Numbers" in apps and "Mail" not in apps      # only budget retrieved
    mem.close()


def test_reason_feeds_retrieved_summary_into_prompt(tmp_path):
    mem = _seed(tmp_path)
    be = CapturingBackend(response="ok")
    reason("budget", mem, be)
    assert "Reviewing the Q2 budget." in be.prompt        # semantic memory reaches the LLM
    assert "Numbers" in be.prompt                          # with its citation
    mem.close()


def test_reason_with_no_matches_has_empty_citations(tmp_path):
    mem = _seed(tmp_path)
    be = FakeLLMBackend(response="I have no record of that.")
    ans = reason("did i use photoshop?", mem, be)
    assert ans.citations == []
    assert be.calls == 1
    mem.close()


def test_reason_respects_time_and_app_filters(tmp_path):
    mem = _seed(tmp_path)
    be = FakeLLMBackend()
    ans = reason("email", mem, be, app="Mail")
    assert {c["app_name"] for c in ans.citations} == {"Mail"}
    mem.close()


def test_build_answer_prompt_handles_no_memories():
    prompt = build_answer_prompt("anything?", [])
    assert "anything?" in prompt
    assert "no" in prompt.lower()                          # instructs the "no record" path
