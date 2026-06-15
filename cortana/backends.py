"""LLM backends for meaning extraction and (later) reasoning.

Sync `generate(prompt) -> str` is the tool-layer seam; the Phase-3 agent loop wraps
it in an executor. `FakeLLMBackend` makes the pipeline testable with no Ollama/MLX.
Filled in at Step 1.
"""

from __future__ import annotations
