"""LLM backends for meaning extraction and (later) reasoning.

Sync `generate(prompt) -> str` is the tool-layer seam; the Phase-3 agent loop wraps
it in an executor. `FakeLLMBackend` makes the pipeline testable with no Ollama/MLX.

Only `FakeLLMBackend` is unit-tested; `OllamaBackend`/`MLXBackend` touch the network
/ native runtime and are exercised manually (see docs/DESIGN.md verification).
"""

from __future__ import annotations

import json
import urllib.request
from typing import Optional


class FakeLLMBackend:
    """Deterministic backend for hermetic tests. Returns a canned string and
    counts calls so tests can assert how often the LLM was invoked."""

    def __init__(self, response: str = "fake summary", model: str = "fake") -> None:
        self.response = response
        self.model = model
        self.calls = 0

    def generate(self, prompt: str) -> str:
        self.calls += 1
        return self.response


class OllamaBackend:
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


class MLXBackend:
    """Native Apple-Silicon inference via mlx-lm. Model stays resident in RAM.
    Imported lazily so this module loads without mlx-lm installed."""

    def __init__(self, *, model: str) -> None:
        from mlx_lm import generate, load  # lazy

        self._generate = generate
        self.model_name = model
        self.model, self.tokenizer = load(model)

    @property
    def model_id(self) -> str:
        return self.model_name

    def generate(self, prompt: str) -> str:
        messages = [{"role": "user", "content": prompt}]
        text = self.tokenizer.apply_chat_template(
            messages, add_generation_prompt=True, tokenize=False
        )
        return self._generate(
            self.model, self.tokenizer, prompt=text, max_tokens=128, verbose=False
        ).strip()


def make_backend(name: str, cfg: Optional[object] = None):
    """Construct a backend by name. ``cfg`` (a Config) supplies model/host when given."""
    model = getattr(cfg, "model", "qwen2.5:7b-instruct")
    host = getattr(cfg, "ollama_host", "http://127.0.0.1:11434")
    if name == "fake":
        return FakeLLMBackend()
    if name == "ollama":
        return OllamaBackend(model=model, host=host)
    if name == "mlx":
        return MLXBackend(model=model)
    raise ValueError(f"unknown backend: {name!r}")
