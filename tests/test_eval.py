"""The retrieval eval harness runs and meets a baseline — so a retrieval regression
(bad recall) fails CI. Hermetic: FakeEmbedder, temp DBs."""

from benchmarks.retrieval_eval import DATASET, evaluate, hit_at_k


def test_hit_at_k_helper():
    rows = [{"app_name": "Mail"}, {"app_name": "Numbers"}]
    assert hit_at_k(rows, "Numbers", 3) is True
    assert hit_at_k(rows, "Numbers", 1) is False        # only top-1 considered
    assert hit_at_k(rows, "Safari", 3) is False


def test_keyword_retrieval_meets_baseline():
    result = evaluate()                                  # keyword-only
    assert result["cases"] == len(DATASET)
    assert result["hit_at_k"] >= 0.75                    # regression gate


def test_hybrid_retrieval_meets_baseline():
    from cortana.embeddings import FakeEmbedder
    result = evaluate(embedder=FakeEmbedder())
    assert result["hit_at_k"] >= 0.75                    # hybrid at least as good
