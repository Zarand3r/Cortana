"""Semantic retrieval — local text embeddings + fusion for hybrid search.

Long-term recall pairs keyword search (SQLite FTS5, exact terms) with **semantic**
search (embedding cosine, meaning/paraphrase) and fuses them with Reciprocal Rank
Fusion — the production RAG baseline (see docs/MEMORY.md §frontier). Everything runs
on-device: a local embedding model (Ollama) or the `FakeEmbedder` for tests.

`cosine`, `reciprocal_rank_fusion`, and `FakeEmbedder` are pure and tested; the real
`OllamaEmbedder` hits the local server and is `# pragma: no cover`.
"""

from __future__ import annotations

import hashlib
import json
import math
import re
import urllib.error
import urllib.request
from abc import ABC, abstractmethod


def cosine(a: list[float], b: list[float]) -> float:
    """Cosine similarity in [-1, 1]; 0.0 for a zero vector (no NaN)."""
    dot = sum(x * y for x, y in zip(a, b))
    na = math.sqrt(sum(x * x for x in a))
    nb = math.sqrt(sum(y * y for y in b))
    if na == 0.0 or nb == 0.0:
        return 0.0
    return dot / (na * nb)


def reciprocal_rank_fusion(rankings: list[list], k: int = 60) -> list:
    """Fuse several ranked id-lists into one. RRF score(id) = Σ 1/(k + rank); items
    ranked high in multiple lists win. Order-stable, robust to score-scale mismatch
    between keyword and vector retrievers."""
    scores: dict = {}
    order: list = []
    for ranking in rankings:
        for rank, item in enumerate(ranking):
            if item not in scores:
                scores[item] = 0.0
                order.append(item)
            scores[item] += 1.0 / (k + rank)
    return sorted(order, key=lambda it: scores[it], reverse=True)


class Embedder(ABC):
    """Contract: turn text into a fixed-length vector."""

    @abstractmethod
    def embed(self, text: str) -> list[float]:
        raise NotImplementedError


class FakeEmbedder(Embedder):
    """Deterministic hashed-bag-of-words embedding for tests: shared words → similar
    vectors, so cosine is meaningful without a model. Not for production quality."""

    def __init__(self, dim: int = 128) -> None:
        self.dim = dim

    def embed(self, text: str) -> list[float]:
        vec = [0.0] * self.dim
        for tok in re.findall(r"[a-z0-9]+", text.lower()):
            h = int(hashlib.md5(tok.encode("utf-8")).hexdigest(), 16)
            vec[h % self.dim] += 1.0
        return vec


class OllamaEmbedder(Embedder):  # pragma: no cover - hits the local Ollama server
    """Local embeddings via Ollama's /api/embeddings (e.g. nomic-embed-text)."""

    def __init__(self, *, model: str = "nomic-embed-text",
                 host: str = "http://127.0.0.1:11434", timeout: float = 30.0) -> None:
        self.url = host.rstrip("/") + "/api/embeddings"
        self.model = model
        self.timeout = timeout

    def embed(self, text: str) -> list[float]:
        payload = json.dumps({"model": self.model, "prompt": text}).encode("utf-8")
        req = urllib.request.Request(
            self.url, data=payload, headers={"Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                data = json.loads(resp.read().decode("utf-8"))
        except (urllib.error.URLError, TimeoutError, json.JSONDecodeError) as exc:
            raise RuntimeError(f"Ollama embeddings failed ({self.model}): {exc}") from exc
        return data.get("embedding") or []


def make_embedder(name: str, cfg=None) -> Embedder:
    """Construct an embedder by name ('fake' | 'ollama')."""
    if name == "fake":
        return FakeEmbedder()
    if name == "ollama":
        model = getattr(cfg, "embed_model", "nomic-embed-text")
        host = getattr(cfg, "ollama_host", "http://127.0.0.1:11434")
        return OllamaEmbedder(model=model, host=host)
    raise ValueError(f"unknown embedder: {name!r}")
