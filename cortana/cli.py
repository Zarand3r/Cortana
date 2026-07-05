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

from cortana.advisor import recommend
from cortana.agent import AgentLoop
from cortana.backends import make_backend
from cortana.config import Config
from cortana.memory import Memory
from cortana.perception import make_demo_sensor
from cortana.reasoning import reason


def _add_common(p: argparse.ArgumentParser) -> None:
    p.add_argument("--config", help="path to a TOML config (default: config/cortana.toml)")
    p.add_argument("--backend", help="ollama | mlx | fake")
    p.add_argument("--model", help="ollama tag or MLX HF repo")
    p.add_argument("--db", help="path to the memory SQLite database")


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="cortana", description="Local on-device perceptual agent.")
    # No subcommand -> launch the desktop app (the one unified product).
    sub = parser.add_subparsers(dest="command")

    run_p = sub.add_parser("run", help="start the continuous perceive→remember loop")
    _add_common(run_p)
    run_p.add_argument("--interval", type=float, help="seconds between captures")
    run_p.add_argument("--no-redact", action="store_true",
                       help="disable secret redaction (NOT recommended)")
    run_p.add_argument("--ticks", type=int,
                       help="stop after N capture cycles (default: run forever)")
    run_p.add_argument("--demo", action="store_true",
                       help="use a synthetic sensor (no screen capture / PyObjC) — "
                            "for deps-free local testing; pair with --backend fake")

    ask_p = sub.add_parser("ask", help="ask about your past activity")
    ask_p.add_argument("question", help="natural-language question")
    _add_common(ask_p)
    ask_p.add_argument("--app", help="restrict to one app")
    ask_p.add_argument("--since", help="ISO timestamp lower bound")
    ask_p.add_argument("--until", help="ISO timestamp upper bound")
    ask_p.add_argument("--limit", type=int, default=20, help="max memories to retrieve")

    rec_p = sub.add_parser("recommend", help="suggest a next action from recent activity")
    _add_common(rec_p)
    rec_p.add_argument("--limit", type=int, default=12, help="recent memories to consider")

    chat_p = sub.add_parser("chat", help="serve a local ChatGPT-style web UI")
    _add_common(chat_p)
    chat_p.add_argument("--port", type=int, help="port for the web UI (default 8808)")
    chat_p.add_argument("--host", help="bind address (default 127.0.0.1)")

    desk_p = sub.add_parser("desktop", help="run the Cortana menu-bar desktop app")
    _add_common(desk_p)
    desk_p.add_argument("--port", type=int, help="port for the chat server (default 8808)")
    desk_p.add_argument("--host", help="bind address (default 127.0.0.1)")

    win_p = sub.add_parser("chat-window", help="(internal) open the chat window")
    win_p.add_argument("--url", required=True, help="chat server URL to display")
    return parser


def _config_from_args(args) -> Config:
    cfg = Config.load(getattr(args, "config", None))
    if getattr(args, "interval", None) is not None:
        cfg.interval = args.interval
    elif getattr(args, "demo", False):
        cfg.interval = 0.05          # bounded demo runs go fast; live runs use the config cadence
    if getattr(args, "backend", None) is not None:
        cfg.backend = args.backend
    if getattr(args, "model", None) is not None:
        cfg.model = args.model
    if getattr(args, "db", None) is not None:
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


def make_loop(cfg: Config, *, sensor=None,
              working_memory=None) -> tuple[AgentLoop, Memory]:
    """Wire Memory + backend + AgentLoop from a Config. ``sensor`` overrides the
    live native sensor; ``working_memory`` injects a shared short-term buffer so
    readers (chat/recommend) can see the loop's recent activity."""
    memory = open_memory(cfg, check_same_thread=False)   # writes funnel through the db executor
    backend = make_backend(cfg.backend, cfg)
    return AgentLoop(cfg, memory, backend, sensor, working_memory=working_memory), memory


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


def cmd_recommend(args) -> int:
    cfg = _config_from_args(args)
    backend = make_backend(cfg.backend, cfg)
    memory = open_memory(cfg)
    try:
        rec = recommend(memory, backend, limit=args.limit)
    finally:
        memory.close()
    print(rec.text)
    if rec.basis:
        print("\nbased on:")
        for c in rec.basis:
            print(f"  [{c['ts']}] {c['app_name']}")
    return 0


def cmd_chat(cfg: Config) -> int:  # pragma: no cover - binds a real socket
    from cortana.chatapp import serve

    backend = make_backend(cfg.backend, cfg)
    # Read-only recall over memory; single-user/low-concurrency, so one shared
    # connection (check_same_thread=False) across the threading server is fine.
    memory = open_memory(cfg, check_same_thread=False)
    url = f"http://{cfg.chat_host}:{cfg.chat_port}"
    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s %(levelname)-7s %(message)s", datefmt="%H:%M:%S")
    logging.info("cortana chat: open %s  (backend=%s, model=%s, memory=%s)",
                 url, cfg.backend, cfg.model, cfg.db_path)
    try:
        serve(backend, host=cfg.chat_host, port=cfg.chat_port,
              system_prompt=cfg.chat_system_prompt, memory=memory)
    finally:
        memory.close()
    return 0


def main(argv=None) -> int:
    args = _parser().parse_args(argv)
    command = args.command or "desktop"          # no subcommand -> the unified app
    if command == "ask":
        return cmd_ask(args)
    if command == "recommend":
        return cmd_recommend(args)
    if command == "chat":
        return cmd_chat(_config_from_args(args))
    if command == "desktop":  # pragma: no cover - native menu-bar app
        from cortana.desktop import run_app
        try:
            return run_app(_config_from_args(args))
        except ImportError as exc:
            print(f"error: the desktop app needs extra dependencies ({exc}).\n"
                  f"install them with:  pip install 'cortana[desktop]'   (or run ./setup.sh)",
                  file=sys.stderr)
            return 2
    if command == "chat-window":  # pragma: no cover - native webview
        from cortana.desktop import run_chat_window
        run_chat_window(args.url)
        return 0
    # command == "run"
    cfg = _config_from_args(args)
    try:
        validate(cfg)
    except ValueError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    sensor = make_demo_sensor() if getattr(args, "demo", False) else None
    return _serve(cfg, sensor=sensor, max_ticks=getattr(args, "ticks", None))


def _serve(cfg: Config, *, sensor=None,
           max_ticks=None) -> int:  # pragma: no cover - live run requires the native sensor
    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s %(levelname)-7s %(message)s", datefmt="%H:%M:%S")
    loop_obj, memory = make_loop(cfg, sensor=sensor)
    mode = "demo sensor" if sensor is not None else "live sensor"
    logging.info("cortana: tracking every %.0fs -> %s (backend=%s, model=%s, %s)",
                 cfg.interval, cfg.db_path, cfg.backend, cfg.model, mode)
    try:
        asyncio.run(loop_obj.run(max_ticks=max_ticks))
    except KeyboardInterrupt:
        pass
    finally:
        memory.close()
    return 0
