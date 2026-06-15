"""Runtime configuration for the Cortana agent.

In-code defaults are authoritative (the agent runs with zero config). A top-level
``config/cortana.toml`` overrides them; missing or partial files fall back to the
defaults. TOML is read with the stdlib ``tomllib`` (Python 3.11+) — no dependency.
"""

from __future__ import annotations

import tomllib
from dataclasses import dataclass, field
from pathlib import Path

GIB = 1024 ** 3

# The shipped config file lives in a top-level `config/` directory (repo root).
DEFAULT_CONFIG_PATH = Path(__file__).resolve().parent.parent / "config" / "cortana.toml"


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

    # --- loading -------------------------------------------------------------
    # Map (toml_section, toml_key) -> (Config field, converter). Single source of
    # truth for the file<->struct binding.
    _FIELD_MAP = {
        ("perception", "interval"): ("interval", float),
        ("perception", "ocr_languages"): ("ocr_languages", tuple),
        ("perception", "ocr_max_chars"): ("ocr_max_chars", int),
        ("perception", "read_focused_text"): ("read_focused_text", bool),
        ("llm", "backend"): ("backend", str),
        ("llm", "model"): ("model", str),
        ("llm", "ollama_host"): ("ollama_host", str),
        ("llm", "batch_size"): ("batch_size", int),
        ("llm", "batch_window"): ("batch_window", float),
        ("memory", "db_path"): ("db_path", lambda v: Path(v).expanduser()),
        ("memory", "retention_days"): ("retention_days", int),
        ("memory", "max_db_gib"): ("max_db_bytes", lambda v: int(float(v) * GIB)),
        ("memory", "queue_max"): ("queue_max", int),
        ("privacy", "excluded_bundles"): ("excluded_bundles", frozenset),
    }

    @classmethod
    def from_dict(cls, data: dict) -> "Config":
        """Build a Config from a parsed TOML mapping, applying only present keys."""
        kwargs: dict = {}
        for (section, key), (field_name, convert) in cls._FIELD_MAP.items():
            sec = data.get(section, {})
            if key in sec:
                kwargs[field_name] = convert(sec[key])
        return cls(**kwargs)

    @classmethod
    def from_toml(cls, path) -> "Config":
        """Load config from a TOML file (must exist)."""
        with open(path, "rb") as fh:
            return cls.from_dict(tomllib.load(fh))

    @classmethod
    def load(cls, path=None) -> "Config":
        """Load from ``path`` (default: the shipped config/cortana.toml). Returns
        in-code defaults when the file is absent."""
        path = Path(path) if path is not None else DEFAULT_CONFIG_PATH
        return cls.from_toml(path) if path.exists() else cls()
