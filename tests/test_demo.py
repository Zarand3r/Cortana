"""Deps-free local test path: a synthetic sensor lets the full perceive→remember
loop run with no PyObjC and no model (--backend fake --demo), so the pipeline is
runnable + verifiable on any machine."""

import asyncio

from cortana.cli import _parser, make_loop
from cortana.config import Config
from cortana.perception import Observation, make_demo_sensor


def test_demo_sensor_cycles_through_distinct_screens():
    sensor = make_demo_sensor()
    obs = [sensor(f"2026-07-03T00:00:0{i}+00:00") for i in range(4)]
    assert all(isinstance(o, Observation) and o.captured for o in obs)
    assert len({o.ocr_text for o in obs}) > 1        # exercises change-detection


def test_demo_sensor_accepts_custom_script():
    sensor = make_demo_sensor([("App", "Win", "hello world")])
    o = sensor("2026-07-03T00:00:00+00:00")
    assert o.app_name == "App" and o.window_title == "Win" and o.ocr_text == "hello world"


def test_make_loop_runs_end_to_end_with_demo_sensor(tmp_path):
    cfg = Config()
    cfg.backend = "fake"
    cfg.interval = 0.0            # no inter-tick sleep so the test is instant
    cfg.db_path = tmp_path / "demo.db"
    loop_obj, mem = make_loop(cfg, sensor=make_demo_sensor())
    try:
        asyncio.run(loop_obj.run(max_ticks=6, install_signal_handlers=False))
        assert mem.counts()["context"] > 0          # memory got populated
        assert mem.recall()                         # and is queryable
    finally:
        mem.close()


def test_run_cli_accepts_demo_and_ticks():
    args = _parser().parse_args(["run", "--demo", "--ticks", "5", "--backend", "fake"])
    assert args.demo is True
    assert args.ticks == 5


def test_demo_defaults_to_fast_interval_but_explicit_wins():
    from cortana.cli import build_config
    assert build_config(["run", "--demo", "--backend", "fake"]).interval == 0.05
    assert build_config(["run", "--demo", "--interval", "2", "--backend", "fake"]).interval == 2
