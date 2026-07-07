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


def _as_bool(v):
    # TOML has native booleans; reject strings like "false" (which bool() would
    # silently read as True — a real footgun for e.g. `redact = "false"`).
    if not isinstance(v, bool):
        raise TypeError(f"expected true/false, got {v!r}")
    return v


def _as_tuple(v):
    if not isinstance(v, list):
        raise TypeError(f"expected a list, got {v!r}")
    return tuple(v)


def _as_frozenset(v):
    if not isinstance(v, list):
        raise TypeError(f"expected a list, got {v!r}")
    return frozenset(v)

# The shipped config file lives in a top-level `config/` directory (repo root).
DEFAULT_CONFIG_PATH = Path(__file__).resolve().parent.parent / "config" / "cortana.toml"


@dataclass
class Config:
    # --- perception ---
    interval: float = 1.0                         # seconds between captures
    ocr_languages: tuple[str, ...] = ("en-US",)
    ocr_max_chars: int = 6000                     # truncate OCR before store AND prompt
    image_dedup: bool = True                      # skip OCR on byte-identical frames (battery);
                                                  # exact hash, safe — any change still OCRs

    # --- meaning extraction (LLM) ---
    backend: str = "ollama"                       # "ollama" | "mlx" | "fake"
    model: str = "qwen2.5:7b-instruct"            # feasible default; 72B is opt-in
    ollama_host: str = "http://127.0.0.1:11434"
    batch_size: int = 4                           # observations per extract_meaning call
    batch_window: float = 60.0                    # ...or flush after this many seconds
    embed: bool = False                           # opt-in: semantic/hybrid retrieval
    embed_model: str = "nomic-embed-text"         # local Ollama embedding model

    # --- chat app (local ChatGPT-style web UI) ---
    chat_host: str = "127.0.0.1"                  # bind address — localhost only
    chat_port: int = 8808                         # web UI port
    chat_system_prompt: str = (
        "You are Cortana, a private assistant running fully locally on the user's "
        "Mac. You are given a log of the user's recent on-screen activity below as "
        "CONTEXT; treat it as ground truth for what they are doing, and cite the "
        "time and app when you use it. If the context is empty, say nothing has been "
        "recorded yet (they may need to start tracking) — never claim you cannot see "
        "their screen. Be concise and accurate."
    )

    # --- memory ---
    db_path: Path = field(default_factory=lambda: Path.home() / ".local_mac_context.db")
    retention_days: int = 90                      # age bound
    max_db_bytes: int = 2 * GIB                   # size bound
    queue_max: int = 256                          # backpressure bound
    working_memory_max: int = 200                 # recent distinct observations kept in RAM (short-term memory)

    # --- privacy ---
    redact: bool = True                           # scrub high-confidence secrets pre-store
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
        ("perception", "ocr_languages"): ("ocr_languages", _as_tuple),
        ("perception", "ocr_max_chars"): ("ocr_max_chars", int),
        ("perception", "image_dedup"): ("image_dedup", _as_bool),
        ("llm", "backend"): ("backend", str),
        ("llm", "model"): ("model", str),
        ("llm", "ollama_host"): ("ollama_host", str),
        ("llm", "batch_size"): ("batch_size", int),
        ("llm", "batch_window"): ("batch_window", float),
        ("llm", "embed"): ("embed", _as_bool),
        ("llm", "embed_model"): ("embed_model", str),
        ("chat", "host"): ("chat_host", str),
        ("chat", "port"): ("chat_port", int),
        ("chat", "system_prompt"): ("chat_system_prompt", str),
        ("memory", "db_path"): ("db_path", lambda v: Path(v).expanduser()),
        ("memory", "retention_days"): ("retention_days", int),
        ("memory", "max_db_gib"): ("max_db_bytes", lambda v: int(float(v) * GIB)),
        ("memory", "queue_max"): ("queue_max", int),
        ("memory", "working_memory_max"): ("working_memory_max", int),
        ("privacy", "redact"): ("redact", _as_bool),
        ("privacy", "excluded_bundles"): ("excluded_bundles", _as_frozenset),
    }

    @classmethod
    def from_dict(cls, data: dict) -> "Config":
        """Build a Config from a parsed TOML mapping, applying only present keys.
        A bad value fails loudly with its section/key, never silently."""
        kwargs: dict = {}
        for (section, key), (field_name, convert) in cls._FIELD_MAP.items():
            sec = data.get(section, {})
            if key not in sec:
                continue
            try:
                kwargs[field_name] = convert(sec[key])
            except (ValueError, TypeError) as exc:
                raise ValueError(f"config [{section}] {key}: {exc}") from exc
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
