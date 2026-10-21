"""Config loading, env overrides and URL parsing."""

from __future__ import annotations

import json

import pytest

from tsql.config import Config, ModelConfig, guess_context_length
from tsql.errors import ConfigError


def test_defaults_are_sane():
    config = Config()
    assert config.model.api_mode is None
    assert config.state_db_path.endswith("state.db")
    assert config.policy.max_rows == 1000
    assert config.compression.threshold == 0.50


def test_json_config_file(tmp_path):
    path = tmp_path / "config.json"
    path.write_text(json.dumps({
        "model": {"model": "gpt-4o-mini", "base_url": "https://example.test/v1"},
        "db": {"dialect": "duckdb", "path": "/tmp/x.duckdb"},
    }))
    config = Config.load(path)
    assert config.model.model == "gpt-4o-mini"
    assert config.model.base_url == "https://example.test/v1"
    assert config.db.dialect == "duckdb"


def test_unknown_config_key_is_rejected(tmp_path):
    path = tmp_path / "config.json"
    path.write_text(json.dumps({"nonsense": 1}))
    with pytest.raises(ConfigError):
        Config.load(path)


def test_missing_config_file_is_an_error(tmp_path):
    with pytest.raises(ConfigError):
        Config.load(tmp_path / "absent.json")


def test_env_overrides_file(monkeypatch, tmp_path):
    path = tmp_path / "config.json"
    path.write_text(json.dumps({"model": {"model": "from-file"}}))
    monkeypatch.setenv("TSQL_MODEL", "from-env")
    monkeypatch.setenv("TSQL_BASE_URL", "https://env.test/v1")
    config = Config.load(path)
    assert config.model.model == "from-env"
    assert config.model.base_url == "https://env.test/v1"


def test_postgres_url_parsing():
    config = Config()
    config._apply_db_url("postgres://alice:s3cret@db.internal:6543/analytics")
    assert config.db.dialect == "postgres"
    assert config.db.host == "db.internal"
    assert config.db.port == 6543
    assert config.db.user == "alice"
    assert config.db.password == "s3cret"
    assert config.db.database == "analytics"


def test_sqlite_url_parsing():
    config = Config()
    config._apply_db_url("sqlite:///data/local.db")
    assert config.db.dialect == "sqlite"
    assert config.db.path == "/data/local.db"


def test_unknown_url_scheme_is_rejected():
    config = Config()
    with pytest.raises(ConfigError):
        config._apply_db_url("mongodb://host/db")


def test_context_length_guessing():
    assert guess_context_length("gpt-4o") == 128_000
    assert guess_context_length("claude-sonnet-4-20250514") == 1_000_000
    assert guess_context_length("unknown-model") == 128_000


def test_explicit_context_length_wins():
    assert ModelConfig(model="gpt-4o", context_length=999).resolved_context_length() == 999


def test_model_threshold_longest_match_wins():
    config = Config()
    config.compression.model_thresholds = {"gpt": 0.4, "gpt-4o": 0.3}
    assert config.compression.threshold_for("gpt-4o-mini") == 0.3
    assert config.compression.threshold_for("gpt-3.5-turbo") == 0.4
    assert config.compression.threshold_for("llama-3") == 0.50
