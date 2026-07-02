"""Command-line entrypoint for the Cortana agent.

  python -m cortana run            start the continuous perceive→remember loop (live)
  python -m cortana ask "<q>"      recall & reason over memory (read-only)
  python -m cortana chat           serve a local ChatGPT-style web UI (on-device LLM)

The pure parts (config building, validation, wiring, `ask`) are unit-tested. `run`'s
live loop needs PyObjC + a local model, so it is exercised on a real Mac, not in CI.
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
from cortana.reasoning import reason


def _add_common(p: argparse.ArgumentParser) -> None:
    p.add_argument("--config", help="path to a TOML config (default: config/cortana.toml)")
    p.add_argument("--backend", help="ollama | mlx | fake")
    p.add_argument("--model", help="ollama tag or MLX HF repo")
    p.add_argument("--db", help="path to the memory SQLite database")


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="cortana", description="Local on-device perceptual agent.")
    sub = parser.add_subparsers(dest="command", required=True)

    run_p = sub.add_parser("run", help="start the continuous perceive→remember loop")
    _add_common(run_p)
    run_p.add_argument("--interval", type=float, help="seconds between captures")
    run_p.add_argument("--no-redact", action="store_true",
                       help="disable secret redaction (NOT recommended)")

    ask_p = sub.add_parser("ask", help="ask about your past activity")
    ask_p.add_argument("question", help="natural-language question")
    _add_common(ask_p)
    ask_p.add_argument("--app", help="restrict to one app")
    ask_p.add_argument("--since", help="ISO timestamp lower bound")
    ask_p.add_argument("--until", help="ISO timestamp upper bound")
    ask_p.add_argument("--limit", type=int, default=20, help="max memories to retrieve")

    chat_p = sub.add_parser("chat", help="serve a local ChatGPT-style web UI")
    _add_common(chat_p)
    chat_p.add_argument("--port", type=int, help="port for the web UI (default 8808)")
    chat_p.add_argument("--host", help="bind address (default 127.0.0.1)")
    return parser


def _config_from_args(args) -> Config:
    cfg = Config.load(args.config)
    if getattr(args, "interval", None) is not None:
        cfg.interval = args.interval
    if args.backend is not None:
        cfg.backend = args.backend
    if args.model is not None:
        cfg.model = args.model
    if args.db is not None:
        cfg.db_path = Path(args.db)
    if getattr(args, "no_redact", False):
        cfg.redact = False
    if getattr(args, "port", None) is not None:
        cfg.chat_port = args.port
    if getattr(args, "host", None) is not None:
        cfg.chat_host = args.host
    return cfg


def build_config(argv=None) -> Config:
    """Parse argv into a Config: load the TOML defaults, then apply CLI overrides."""
    return _config_from_args(_parser().parse_args(argv))


def validate(cfg: Config) -> None:
    """Reject configurations that would misbehave in a live (unbounded) run."""
    if cfg.interval <= 0:
        raise ValueError(
            f"interval must be > 0 (got {cfg.interval}); a non-positive interval "
            "would capture without pause"
        )


def open_memory(cfg: Config, *, check_same_thread: bool = True) -> Memory:
    """Build Memory from a Config — the single place config maps onto storage."""
    return Memory(
        cfg.db_path,
        ocr_max_chars=cfg.ocr_max_chars,
        retention_days=cfg.retention_days,
        max_db_bytes=cfg.max_db_bytes,
        check_same_thread=check_same_thread,
    )


def make_loop(cfg: Config) -> tuple[AgentLoop, Memory]:
    """Wire Memory + backend + AgentLoop from a Config (live native sensor by default)."""
    memory = open_memory(cfg, check_same_thread=False)   # writes funnel through the db executor
    backend = make_backend(cfg.backend, cfg)
    return AgentLoop(cfg, memory, backend), memory


def cmd_ask(args) -> int:
    cfg = _config_from_args(args)
    backend = make_backend(cfg.backend, cfg)
    memory = open_memory(cfg)
    try:
        answer = reason(args.question, memory, backend,
                        since=args.since, until=args.until, app=args.app, limit=args.limit)
    finally:
        memory.close()
    print(answer.text)
    if answer.citations:
        print("\nsources:")
        for c in answer.citations:
            print(f"  [{c['ts']}] {c['app_name']}")
    return 0


def cmd_chat(cfg: Config) -> int:  # pragma: no cover - binds a real socket
    from cortana.chatapp import serve

    backend = make_backend(cfg.backend, cfg)
    url = f"http://{cfg.chat_host}:{cfg.chat_port}"
    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s %(levelname)-7s %(message)s", datefmt="%H:%M:%S")
    logging.info("cortana chat: open %s  (backend=%s, model=%s)", url, cfg.backend, cfg.model)
    serve(backend, host=cfg.chat_host, port=cfg.chat_port,
          system_prompt=cfg.chat_system_prompt)
    return 0


def main(argv=None) -> int:
    args = _parser().parse_args(argv)
    if args.command == "ask":
        return cmd_ask(args)
    if args.command == "chat":
        return cmd_chat(_config_from_args(args))
    # command == "run"
    cfg = _config_from_args(args)
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
