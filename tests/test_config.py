"""Config loads from a top-level TOML file, overriding in-code defaults."""

from pathlib import Path

from cortana.config import GIB, DEFAULT_CONFIG_PATH, Config


def _write(tmp_path, body: str) -> Path:
    p = tmp_path / "cortana.toml"
    p.write_text(body)
    return p


def test_defaults_when_no_file():
    cfg = Config()
    assert cfg.interval == 1.0
    assert cfg.model == "qwen2.5:7b-instruct"
    assert cfg.retention_days == 90
    assert cfg.max_db_bytes == 2 * GIB


def test_load_missing_path_returns_defaults(tmp_path):
    cfg = Config.load(tmp_path / "does_not_exist.toml")
    assert cfg == Config()


def test_from_toml_overrides_only_present_keys(tmp_path):
    path = _write(tmp_path, """
        [perception]
        interval = 15.0

        [llm]
        model = "qwen2.5:72b-instruct-q6_K"

        [memory]
        retention_days = 30
    """)
    cfg = Config.from_toml(path)
    assert cfg.interval == 15.0                     # overridden
    assert cfg.model == "qwen2.5:72b-instruct-q6_K"  # overridden
    assert cfg.retention_days == 30                  # overridden
    assert cfg.batch_size == 4                        # untouched default


def test_type_conversions(tmp_path):
    path = _write(tmp_path, """
        [perception]
        ocr_languages = ["en-US", "fr-FR"]

        [memory]
        db_path = "~/somewhere.db"
        max_db_gib = 0.5

        [privacy]
        excluded_bundles = ["com.foo.bar"]
    """)
    cfg = Config.from_toml(path)
    assert cfg.ocr_languages == ("en-US", "fr-FR")        # list -> tuple
    assert isinstance(cfg.db_path, Path)                   # str -> Path
    assert "~" not in str(cfg.db_path)                     # expanduser applied
    assert cfg.max_db_bytes == int(0.5 * GIB)              # gib -> bytes
    assert cfg.excluded_bundles == frozenset({"com.foo.bar"})  # list -> frozenset


def test_shipped_config_loads_and_matches_code_defaults():
    """The committed config/cortana.toml is a template of the in-code defaults;
    loading it must reproduce them (keeps the file and code in sync)."""
    assert DEFAULT_CONFIG_PATH.exists()
    assert Config.load() == Config()
