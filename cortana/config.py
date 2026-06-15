"""Runtime configuration for the Cortana agent.

Pure dataclass — imports with no native deps (P7). Native concerns (PyObjC,
Ollama) live behind lazy imports in their own modules.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

GIB = 1024 ** 3


@dataclass
class Config:
    # --- perception ---
    interval: float = 30.0                        # seconds between captures
    ocr_languages: tuple[str, ...] = ("en-US",)
    ocr_max_chars: int = 6000                     # truncate OCR before store AND prompt
    read_focused_text: bool = False               # opt-in Accessibility text grab

    # --- meaning extraction (LLM) ---
    backend: str = "ollama"                       # "ollama" | "mlx" | "fake"
    model: str = "qwen2.5:7b-instruct"            # feasible default; 72B is opt-in
    ollama_host: str = "http://127.0.0.1:11434"
    batch_size: int = 4                           # observations per extract_meaning call
    batch_window: float = 60.0                    # ...or flush after this many seconds

    # --- memory ---
    db_path: Path = field(default_factory=lambda: Path.home() / ".local_mac_context.db")
    retention_days: int = 90                      # age bound
    max_db_bytes: int = 2 * GIB                   # size bound
    queue_max: int = 256                          # backpressure bound

    # --- privacy (Phase 5; carried here so the seam is stable) ---
    excluded_bundles: frozenset[str] = field(default_factory=lambda: frozenset({
        "com.apple.keychainaccess",
        "com.1password.1password",
        "com.agilebits.onepassword7",
    }))
