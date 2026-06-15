"""LLM backends for meaning extraction and (later) reasoning.

`LLMBackend` is the contract: a sync `generate(prompt) -> str` plus a `model: str`
identifier. The Phase-3 agent loop wraps `generate` in an executor. `FakeLLMBackend`
makes the pipeline testable with no Ollama/MLX.

Only `FakeLLMBackend` is unit-tested; `OllamaBackend`/`MLXBackend` touch the network
/ native runtime and are exercised manually (see docs/DESIGN.md verification).
"""

from __future__ import annotations

import json
import urllib.request
from abc import ABC, abstractmethod
from enum import Enum

from cortana.config import Config


class Backend(str, Enum):
    """The closed set of valid backend names — single source of truth."""

    FAKE = "fake"
    OLLAMA = "ollama"
    MLX = "mlx"


class LLMBackend(ABC):
    """Contract every backend satisfies: a model identifier and sync text generation."""

    model: str

    @abstractmethod
    def generate(self, prompt: str) -> str:
        """Return the model's completion for ``prompt``."""
        raise NotImplementedError


class FakeLLMBackend(LLMBackend):
    """Deterministic backend for hermetic tests. Returns a canned string and
    counts calls so tests can assert how often the LLM was invoked."""

    def __init__(self, response: str = "fake summary", model: str = "fake") -> None:
        self.response = response
        self.model = model
        self.calls = 0

    def generate(self, prompt: str) -> str:
        self.calls += 1
        return self.response


class OllamaBackend(LLMBackend):
    """Talks to a local Ollama server over HTTP. Uses only the stdlib."""

    def __init__(self, *, model: str, host: str = "http://127.0.0.1:11434",
                 timeout: float = 120.0) -> None:
        self.url = host.rstrip("/") + "/api/generate"
        self.model = model
        self.timeout = timeout

    def generate(self, prompt: str) -> str:
        payload = json.dumps({
            "model": self.model,
            "prompt": prompt,
            "stream": False,
            "options": {"temperature": 0.2, "num_predict": 128},
        }).encode("utf-8")
        req = urllib.request.Request(
            self.url, data=payload, headers={"Content-Type": "application/json"}
        )
        with urllib.request.urlopen(req, timeout=self.timeout) as resp:
            data = json.loads(resp.read().decode("utf-8"))
        return (data.get("response") or "").strip()


class MLXBackend(LLMBackend):
    """Native Apple-Silicon inference via mlx-lm. The loaded model stays resident in
    RAM as ``_model``; ``model`` remains the identifier string (the contract).
    Imported lazily so this module loads without mlx-lm installed."""

    def __init__(self, *, model: str) -> None:
        from mlx_lm import generate, load  # lazy

        self._generate = generate
        self.model = model                      # identifier string (contract)
        self._model, self.tokenizer = load(model)

    def generate(self, prompt: str) -> str:
        messages = [{"role": "user", "content": prompt}]
        text = self.tokenizer.apply_chat_template(
            messages, add_generation_prompt=True, tokenize=False
        )
        return self._generate(
            self._model, self.tokenizer, prompt=text, max_tokens=128, verbose=False
        ).strip()


def make_backend(name: str | Backend, cfg: Config | None = None) -> LLMBackend:
    """Construct a backend by name. ``cfg`` (a Config) supplies model/host when given.
    Raises ValueError for an unknown name."""
    backend = Backend(name)                      # raises ValueError on unknown
    model = getattr(cfg, "model", "qwen2.5:7b-instruct")
    host = getattr(cfg, "ollama_host", "http://127.0.0.1:11434")
    match backend:
        case Backend.FAKE:
            return FakeLLMBackend()
        case Backend.OLLAMA:
            return OllamaBackend(model=model, host=host)
        case Backend.MLX:
            return MLXBackend(model=model)
    raise AssertionError(f"unhandled backend: {backend!r}")  # unreachable
