"""Semantic retrieval primitives: embeddings, cosine similarity, and Reciprocal
Rank Fusion (for hybrid keyword+vector search). Pure logic — the real Ollama
embedder is native/pragma."""

import math

from cortana.embeddings import FakeEmbedder, cosine, make_embedder, reciprocal_rank_fusion


# --- cosine ----------------------------------------------------------------

def test_cosine_identical_is_one():
    assert math.isclose(cosine([1.0, 2.0, 3.0], [1.0, 2.0, 3.0]), 1.0, rel_tol=1e-9)


def test_cosine_orthogonal_is_zero():
    assert math.isclose(cosine([1.0, 0.0], [0.0, 1.0]), 0.0, abs_tol=1e-9)


def test_cosine_zero_vector_is_zero():
    assert cosine([0.0, 0.0], [1.0, 1.0]) == 0.0        # no NaN


# --- Reciprocal Rank Fusion ------------------------------------------------

def test_rrf_rewards_items_ranked_high_in_both_lists():
    keyword = [1, 2, 3]        # id 1 top by keyword
    vector = [3, 4, 1]         # id 3 top by vector; id 1 appears in both
    fused = reciprocal_rank_fusion([keyword, vector])
    assert set(fused) == {1, 2, 3, 4}
    assert fused[0] in (1, 3)   # items present+high in both lists rank first


def test_rrf_handles_empty_lists():
    assert reciprocal_rank_fusion([[], []]) == []
    assert reciprocal_rank_fusion([[5, 6]]) == [5, 6]


# --- FakeEmbedder ----------------------------------------------------------

def test_fake_embedder_is_deterministic():
    e = FakeEmbedder()
    assert e.embed("quarterly budget") == e.embed("quarterly budget")


def test_fake_embedder_similar_text_closer_than_dissimilar():
    e = FakeEmbedder()
    q = e.embed("quarterly budget report")
    near = e.embed("the budget report for this quarter")
    far = e.embed("python asyncio event loop")
    assert cosine(q, near) > cosine(q, far)


def test_make_embedder_fake():
    assert isinstance(make_embedder("fake"), FakeEmbedder)
