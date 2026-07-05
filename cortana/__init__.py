"""Cortana — a local, private, on-device perceptual agent for macOS.

Package subsystems (see docs/DESIGN.md):
  - config:     runtime configuration
  - perception: the agent's senses (capture, OCR, change-detection, meaning extraction)
  - backends:   LLM backends (fake / Ollama / MLX)
  - memory:     tiered episodic + semantic long-term memory over SQLite
  - working_memory: short-term rolling buffer of recent observations (in RAM)
  - embeddings: local text embeddings + fusion for hybrid (keyword+semantic) recall
"""

__all__ = ["config", "perception", "backends", "memory", "working_memory", "embeddings"]
