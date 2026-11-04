from pathlib import Path

import pytest

from tsdb_optimizer.config import Settings, load_settings
from tsdb_optimizer.errors import ConfigError


def test_settings_redacts_password() -> None:
    settings = Settings(iotdb_password="s3cret")
    dumped = settings.redacted()
    assert dumped["iotdb_password"] == "***"
    assert "s3cret" not in str(dumped)
    assert "s3cret" not in str(settings.safe_summary())
    assert "s3cret" not in repr(settings)


def test_env_file_and_process_env_precedence(tmp_path: Path) -> None:
    env_file = tmp_path / ".env"
    env_file.write_text(
        "IOTDB_HOST=from-file\n"
        "IOTDB_PORT=16667\n"
        "IOTDB_USER=file-user\n"
        "IOTDB_PASSWORD=file-password\n",
        encoding="utf-8",
    )
    settings = load_settings(
        env_file=env_file,
        environ={
            "IOTDB_HOST": "from-env",
            "IOTDB_PORT": "6668",
            "IOTDB_PASSWORD": "env-password",
        },
    )
    assert settings.iotdb_host == "from-env"
    assert settings.iotdb_port == 6668
    assert settings.iotdb_user == "file-user"
    assert settings.iotdb_password.get_secret_value() == "env-password"


def test_missing_password_is_config_error(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.chdir(tmp_path)
    with pytest.raises(ConfigError, match="IOTDB_PASSWORD"):
        load_settings(environ={})


def test_explicit_missing_env_file_is_error(tmp_path: Path) -> None:
    with pytest.raises(ConfigError, match="does not exist"):
        load_settings(env_file=tmp_path / "missing.env", environ={})


def test_invalid_port_and_log_level(tmp_path: Path) -> None:
    with pytest.raises(ConfigError, match="invalid configuration"):
        load_settings(
            environ={
                "IOTDB_PASSWORD": "x",
                "IOTDB_PORT": "70000",
            }
        )
    with pytest.raises(ConfigError, match="invalid configuration"):
        load_settings(
            environ={
                "IOTDB_PASSWORD": "x",
                "TSDB_OPTIMIZER_LOG_LEVEL": "LOUD",
            }
        )


def test_cache_epoch_is_read_from_environment_and_normalized() -> None:
    settings = load_settings(
        environ={
            "IOTDB_PASSWORD": "x",
            "TSDB_OPTIMIZER_CACHE_EPOCH": "  epoch-2026-11-01  ",
        }
    )
    assert settings.cache_epoch == "epoch-2026-11-01"
    assert settings.safe_summary()["cache_epoch"] == "epoch-2026-11-01"

    default = load_settings(environ={"IOTDB_PASSWORD": "x"})
    assert default.cache_epoch is None


def test_blank_cache_epoch_is_a_config_error() -> None:
    with pytest.raises(ConfigError, match="invalid configuration"):
        load_settings(
            environ={
                "IOTDB_PASSWORD": "x",
                "TSDB_OPTIMIZER_CACHE_EPOCH": "   ",
            }
        )
