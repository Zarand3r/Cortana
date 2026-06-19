"""Phase 3.5: the `cortana run` entrypoint. The live run needs the native sensor;
here we test the parts that don't — config building, validation, and wiring."""

from pathlib import Path

import pytest

from cortana.agent import AgentLoop
from cortana.backends import FakeLLMBackend
from cortana.cli import build_config, main, make_loop, validate
from cortana.config import Config
from cortana.memory import Memory


def test_build_config_defaults_match_config_file():
    cfg = build_config(["run"])
    assert cfg.interval == 30.0
    assert cfg.backend == "ollama"
    assert cfg.model == "qwen2.5:7b-instruct"


def test_build_config_applies_overrides(tmp_path):
    db = tmp_path / "x.db"
    cfg = build_config(["run", "--interval", "15", "--backend", "fake",
                        "--model", "m", "--db", str(db)])
    assert cfg.interval == 15.0
    assert cfg.backend == "fake"
    assert cfg.model == "m"
    assert cfg.db_path == db


def test_validate_rejects_nonpositive_interval():
    with pytest.raises(ValueError):
        validate(Config(interval=0))
    with pytest.raises(ValueError):
        validate(Config(interval=-5))
    validate(Config(interval=30))            # ok, no raise


def test_make_loop_builds_components(tmp_path):
    cfg = build_config(["run", "--backend", "fake", "--db", str(tmp_path / "m.db")])
    loop_obj, mem = make_loop(cfg)
    try:
        assert isinstance(loop_obj, AgentLoop)
        assert isinstance(mem, Memory)
        assert isinstance(loop_obj._backend, FakeLLMBackend)
    finally:
        mem.close()


def test_main_rejects_bad_interval(capsys):
    rc = main(["run", "--interval", "0"])
    assert rc == 2
    assert "interval" in capsys.readouterr().err
