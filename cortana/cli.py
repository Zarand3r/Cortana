"""Command-line entrypoint for the Cortana agent.

`python -m cortana run` starts the continuous perceive→remember loop with the live
native sensor and the configured LLM backend. The pure parts (config building,
validation, wiring) are unit-tested; the live run requires PyObjC + a local model,
so it is exercised on a real Mac, not in CI.
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import sys
from pathlib import Path

from cortana.agent import AgentLoop
from cortana.backends import make_backend
from cortana.config import Config
from cortana.memory import Memory


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="cortana", description="Local on-device perceptual agent.")
    sub = parser.add_subparsers(dest="command", required=True)

    run_p = sub.add_parser("run", help="start the continuous perceive→remember loop")
    run_p.add_argument("--config", help="path to a TOML config (default: config/cortana.toml)")
    run_p.add_argument("--interval", type=float, help="seconds between captures")
    run_p.add_argument("--backend", help="ollama | mlx | fake")
    run_p.add_argument("--model", help="ollama tag or MLX HF repo")
    run_p.add_argument("--db", help="path to the memory SQLite database")
    return parser


def build_config(argv=None) -> Config:
    """Parse argv into a Config: load the TOML defaults, then apply CLI overrides."""
    args = _parser().parse_args(argv)
    cfg = Config.load(args.config)
    if args.interval is not None:
        cfg.interval = args.interval
    if args.backend is not None:
        cfg.backend = args.backend
    if args.model is not None:
        cfg.model = args.model
    if args.db is not None:
        cfg.db_path = Path(args.db)
    return cfg


def validate(cfg: Config) -> None:
    """Reject configurations that would misbehave in a live (unbounded) run."""
    if cfg.interval <= 0:
        raise ValueError(
            f"interval must be > 0 (got {cfg.interval}); a non-positive interval "
            "would capture without pause"
        )


def make_loop(cfg: Config) -> tuple[AgentLoop, Memory]:
    """Wire Memory + backend + AgentLoop from a Config (live native sensor by default)."""
    memory = Memory(
        cfg.db_path,
        ocr_max_chars=cfg.ocr_max_chars,
        retention_days=cfg.retention_days,
        max_db_bytes=cfg.max_db_bytes,
        check_same_thread=False,        # all writes funnel through the loop's db executor
    )
    backend = make_backend(cfg.backend, cfg)
    return AgentLoop(cfg, memory, backend), memory


def main(argv=None) -> int:
    cfg = build_config(argv)
    try:
        validate(cfg)
    except ValueError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    return _serve(cfg)  # pragma: no cover - live run requires the native sensor


def _serve(cfg: Config) -> int:  # pragma: no cover - live run requires the native sensor
    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s %(levelname)-7s %(message)s", datefmt="%H:%M:%S")
    loop_obj, memory = make_loop(cfg)
    logging.info("cortana: tracking every %.0fs -> %s (backend=%s, model=%s)",
                 cfg.interval, cfg.db_path, cfg.backend, cfg.model)
    try:
        asyncio.run(loop_obj.run())
    except KeyboardInterrupt:
        pass
    finally:
        memory.close()
    return 0
