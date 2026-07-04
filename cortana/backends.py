"""LLM backends for meaning extraction and (later) reasoning.

`LLMBackend` is the contract: a sync `generate(prompt) -> str` plus a `model: str`
identifier. The Phase-3 agent loop wraps `generate` in an executor. `FakeLLMBackend`
makes the pipeline testable with no Ollama/MLX.

Only `FakeLLMBackend` is unit-tested; `OllamaBackend`/`MLXBackend` touch the network
/ native runtime and are exercised manually (see docs/DESIGN.md verification).
"""

from __future__ import annotations

import json
import urllib.error
import urllib.request
from abc import ABC, abstractmethod
from collections.abc import Iterable, Iterator
from dataclasses import dataclass
from enum import Enum

from cortana.config import Config


class BackendError(RuntimeError):
    """An LLM backend call failed (server down, bad response, timeout). Raised with
    context so callers degrade visibly instead of propagating a raw URLError."""


class Backend(str, Enum):
    """The closed set of valid backend names — single source of truth."""

    FAKE = "fake"
    OLLAMA = "ollama"
    MLX = "mlx"


class Role(str, Enum):
    """The closed set of chat message roles (also the wire values)."""

    SYSTEM = "system"
    USER = "user"
    ASSISTANT = "assistant"


@dataclass(frozen=True)
class Message:
    """One turn in a chat conversation. ``role`` is validated against ``Role``."""

    role: Role
    content: str

    def to_wire(self) -> dict[str, str]:
        """Render as the ``{"role", "content"}`` dict the model APIs expect."""
        return {"role": self.role.value, "content": self.content}


class LLMBackend(ABC):
    """Contract every backend satisfies: a model identifier, single-shot text
    generation (used by the perception pipeline), and streaming multi-turn chat
    (used by the local chat app)."""

    model: str

    @abstractmethod
    def generate(self, prompt: str) -> str:
        """Return the model's completion for ``prompt``."""
        raise NotImplementedError

    @abstractmethod
    def chat(self, messages: Iterable[Message]) -> Iterator[str]:
        """Stream the assistant's reply to a multi-turn conversation, token by token."""
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

    def chat(self, messages: Iterable[Message]) -> Iterator[str]:
        """Yield the canned response as whitespace-delimited tokens (so streaming
        behaviour is exercised) and count the call."""
        self.calls += 1
        words = self.response.split()
        for i, word in enumerate(words):
            yield word if i == 0 else " " + word


class OllamaBackend(LLMBackend):
    """Talks to a local Ollama server over HTTP. Uses only the stdlib."""

    def __init__(self, *, model: str, host: str = "http://127.0.0.1:11434",
                 timeout: float = 120.0) -> None:
        self.host = host.rstrip("/")
        self.url = self.host + "/api/generate"
        self.chat_url = self.host + "/api/chat"
        self.model = model
        self.timeout = timeout

    def generate(self, prompt: str) -> str:  # pragma: no cover - hits the network
        payload = json.dumps({
            "model": self.model,
            "prompt": prompt,
            "stream": False,
            "options": {"temperature": 0.2, "num_predict": 128},
        }).encode("utf-8")
        req = urllib.request.Request(
            self.url, data=payload, headers={"Content-Type": "application/json"}
        )
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                data = json.loads(resp.read().decode("utf-8"))
        except (urllib.error.URLError, TimeoutError, json.JSONDecodeError) as exc:
            raise BackendError(
                f"Ollama generate failed ({self.host}, model={self.model}): {exc}") from exc
        return (data.get("response") or "").strip()

    def chat(self, messages: Iterable[Message]) -> Iterator[str]:  # pragma: no cover - hits the network
        payload = json.dumps({
            "model": self.model,
            "messages": [m.to_wire() for m in messages],
            "stream": True,
        }).encode("utf-8")
        req = urllib.request.Request(
            self.chat_url, data=payload, headers={"Content-Type": "application/json"}
        )
        try:
            resp = urllib.request.urlopen(req, timeout=self.timeout)
        except (urllib.error.URLError, TimeoutError) as exc:
            raise BackendError(
                f"Ollama chat failed ({self.host}, model={self.model}): {exc}") from exc
        with resp:
            for line in resp:                       # one JSON object per line
                line = line.strip()
                if not line:
                    continue
                try:
                    chunk = json.loads(line)
                except json.JSONDecodeError:
                    continue                        # skip a malformed/keepalive line, keep streaming
                token = chunk.get("message", {}).get("content", "")
                if token:
                    yield token
                if chunk.get("done"):
                    break


class MLXBackend(LLMBackend):  # pragma: no cover - requires mlx-lm + a resident model
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

    def chat(self, messages: Iterable[Message]) -> Iterator[str]:
        from mlx_lm import stream_generate  # lazy: keep this module importable without mlx-lm

        text = self.tokenizer.apply_chat_template(
            [m.to_wire() for m in messages], add_generation_prompt=True, tokenize=False
        )
        for response in stream_generate(
            self._model, self.tokenizer, prompt=text, max_tokens=1024
        ):
            yield response.text


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
        case Backend.MLX:  # pragma: no cover - constructs a resident MLX model
            return MLXBackend(model=model)
    raise AssertionError(f"unhandled backend: {backend!r}")  # pragma: no cover
