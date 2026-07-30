"""First-run provisioning logic for the packaged app: readiness state machine,
model-cache detection, and the frozen-bundle MLX default. Native download/TCC bits
are pragma and verified on a real Mac."""

from cortana import runtime
from cortana.config import Config


# --- readiness state machine -------------------------------------------------

def test_readiness_ready_when_model_and_permission():
    state, _ = runtime.readiness(model_available=True, screen_recording=True)
    assert state is runtime.RuntimeState.READY


def test_readiness_model_missing_is_surfaced_first():
    # model is the long pole -> reported even if permission is also missing
    state, msg = runtime.readiness(model_available=False, screen_recording=False)
    assert state is runtime.RuntimeState.MODEL_MISSING
    assert "model" in msg.lower()


def test_readiness_permission_needed_when_model_present():
    state, msg = runtime.readiness(model_available=True, screen_recording=False)
    assert state is runtime.RuntimeState.PERMISSION_NEEDED
    assert "screen recording" in msg.lower()


# --- production backend default (frozen bundle) ------------------------------

def test_frozen_bundle_defaults_to_mlx(monkeypatch):
    monkeypatch.setattr(runtime, "is_frozen", lambda: True)
    cfg = Config()                                   # dev default: ollama + ollama tag
    runtime.apply_production_defaults(cfg)
    assert cfg.backend == "mlx"
    assert cfg.model == runtime.DEFAULT_MLX_MODEL


def test_frozen_bundle_respects_user_backend(monkeypatch):
    monkeypatch.setattr(runtime, "is_frozen", lambda: True)
    cfg = Config()
    cfg.backend = "fake"                             # user override must win
    runtime.apply_production_defaults(cfg)
    assert cfg.backend == "fake"


def test_source_run_keeps_ollama_default(monkeypatch):
    monkeypatch.setattr(runtime, "is_frozen", lambda: False)
    cfg = Config()
    runtime.apply_production_defaults(cfg)
    assert cfg.backend == "ollama"                   # from source, nothing changes


# --- model cache detection ---------------------------------------------------

def test_is_model_available_false_when_uncached(tmp_path, monkeypatch):
    monkeypatch.setattr(runtime, "hf_cache_dir", lambda: tmp_path)
    assert runtime.is_model_available("mlx-community/X") is False


def test_is_model_available_true_when_snapshot_present(tmp_path, monkeypatch):
    monkeypatch.setattr(runtime, "hf_cache_dir", lambda: tmp_path)
    repo = "mlx-community/Qwen2.5-7B-Instruct-4bit"
    snap = tmp_path / ("models--" + repo.replace("/", "--")) / "snapshots" / "deadbeef"
    snap.mkdir(parents=True)
    (snap / "config.json").write_text("{}")
    assert runtime.is_model_available(repo) is True


def test_is_model_available_false_for_empty_snapshot(tmp_path, monkeypatch):
    monkeypatch.setattr(runtime, "hf_cache_dir", lambda: tmp_path)
    repo = "mlx-community/Empty"
    (tmp_path / ("models--" + repo.replace("/", "--")) / "snapshots").mkdir(parents=True)
    assert runtime.is_model_available(repo) is False   # dir exists but no snapshot files


# --- small helpers -----------------------------------------------------------

def test_is_frozen_false_running_from_source():
    assert runtime.is_frozen() is False                # the test process isn't a bundle


def test_hf_cache_dir_honors_hf_home(monkeypatch, tmp_path):
    monkeypatch.setenv("HF_HOME", str(tmp_path))
    assert runtime.hf_cache_dir() == tmp_path / "hub"


def test_hf_cache_dir_defaults_to_home(monkeypatch):
    monkeypatch.delenv("HF_HOME", raising=False)
    assert runtime.hf_cache_dir().parts[-3:] == (".cache", "huggingface", "hub")


def test_frozen_bundle_keeps_custom_model(monkeypatch):
    monkeypatch.setattr(runtime, "is_frozen", lambda: True)
    cfg = Config()
    cfg.model = "mlx-community/Custom-3B"              # user picked a model, kept ollama backend
    runtime.apply_production_defaults(cfg)
    assert cfg.backend == "mlx"                        # backend still upgraded to bundled runtime
    assert cfg.model == "mlx-community/Custom-3B"      # but their model choice is preserved


def test_explicit_mlx_backend_gets_mlx_model_even_from_source(monkeypatch):
    # backend="mlx" with the untouched OLLAMA default tag is a config hole: an
    # Ollama tag is not a valid HF repo id, so mlx_lm.load would fail at first use.
    monkeypatch.setattr(runtime, "is_frozen", lambda: False)
    cfg = Config()
    cfg.backend = "mlx"                                # user chose mlx, left model default
    runtime.apply_production_defaults(cfg)
    assert cfg.model == runtime.DEFAULT_MLX_MODEL      # swapped to a loadable repo id
