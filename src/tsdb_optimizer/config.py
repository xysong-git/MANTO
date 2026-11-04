"""Safe configuration loading.

Secrets are read from the process environment or an explicitly supplied `.env`
file.  They are never written to logs, SQLite, or JSON output.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any, Mapping

from dotenv import dotenv_values
from pydantic import BaseModel, ConfigDict, Field, SecretStr, ValidationError, field_validator

from .cache_epoch import normalize_cache_epoch
from .errors import ConfigError

_LOG_LEVELS = {"DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"}


class Settings(BaseModel):
    model_config = ConfigDict(extra="forbid")

    iotdb_host: str = Field(default="127.0.0.1", min_length=1)
    iotdb_port: int = Field(default=6667, ge=1, le=65535)
    iotdb_user: str = Field(default="root", min_length=1)
    iotdb_password: SecretStr
    iotdb_connection_timeout_ms: int = Field(default=5000, ge=1, le=600_000)
    sqlite_path: Path = Field(default=Path("data/tsdb_optimizer.sqlite3"))
    log_level: str = Field(default="INFO")
    preview_rows: int = Field(default=20, ge=1, le=1000)
    # Opt-in caller-declared immutable dataset epoch for S1_REPEAT_CACHE.  It is
    # not a server version and is never guessed; absent means caching is off.
    cache_epoch: str | None = Field(default=None, max_length=200)

    @field_validator("cache_epoch")
    @classmethod
    def normalize_optional_cache_epoch(cls, value: str | None) -> str | None:
        return normalize_cache_epoch(value)

    @field_validator("log_level")
    @classmethod
    def normalize_log_level(cls, value: str) -> str:
        level = value.strip().upper()
        if level not in _LOG_LEVELS:
            raise ValueError(f"log level must be one of {sorted(_LOG_LEVELS)}")
        return level

    @field_validator("iotdb_password")
    @classmethod
    def password_must_not_be_empty(cls, value: SecretStr) -> SecretStr:
        if not value.get_secret_value():
            raise ValueError("IOTDB_PASSWORD must not be empty")
        return value

    def redacted(self) -> dict[str, Any]:
        data = self.model_dump(mode="json")
        data["iotdb_password"] = "***"
        return data

    def safe_summary(self) -> dict[str, Any]:
        return {
            "host": self.iotdb_host,
            "port": self.iotdb_port,
            "user": self.iotdb_user,
            "sqlite_path": str(self.sqlite_path),
            "log_level": self.log_level,
            "preview_rows": self.preview_rows,
            "cache_epoch": self.cache_epoch,
        }


_ENV_MAP = {
    "IOTDB_HOST": "iotdb_host",
    "IOTDB_PORT": "iotdb_port",
    "IOTDB_USER": "iotdb_user",
    "IOTDB_PASSWORD": "iotdb_password",
    "IOTDB_CONNECTION_TIMEOUT_MS": "iotdb_connection_timeout_ms",
    "TSDB_OPTIMIZER_SQLITE_PATH": "sqlite_path",
    "TSDB_OPTIMIZER_LOG_LEVEL": "log_level",
    "TSDB_OPTIMIZER_PREVIEW_ROWS": "preview_rows",
    "TSDB_OPTIMIZER_CACHE_EPOCH": "cache_epoch",
}


def _read_env_file(path: Path) -> dict[str, str]:
    try:
        values = dotenv_values(path)
    except Exception as exc:  # noqa: BLE001 - surface a safe config error
        raise ConfigError(f"could not read env file {path}: {exc}") from exc
    return {key: value for key, value in values.items() if value is not None}


def load_settings(
    env_file: Path | None = None,
    environ: Mapping[str, str] | None = None,
) -> Settings:
    """Load settings with precedence: explicit environment > .env > defaults.

    If ``env_file`` is omitted, ``./.env`` is read when it exists.  If a path is
    supplied explicitly, a missing file is an error.
    """

    file_values: dict[str, str] = {}
    if env_file is None:
        default_file = Path(".env")
        if default_file.exists():
            file_values = _read_env_file(default_file)
    else:
        path = Path(env_file)
        if not path.exists():
            raise ConfigError(f"env file does not exist: {path}")
        file_values = _read_env_file(path)

    process_env = dict(os.environ if environ is None else environ)
    merged = {**file_values, **process_env}

    values: dict[str, Any] = {}
    for env_name, field_name in _ENV_MAP.items():
        raw = merged.get(env_name)
        if raw is None or raw == "":
            continue
        if field_name in {"iotdb_port", "iotdb_connection_timeout_ms", "preview_rows"}:
            try:
                values[field_name] = int(raw)
            except ValueError as exc:
                raise ConfigError(f"{env_name} must be an integer") from exc
        elif field_name == "sqlite_path":
            values[field_name] = Path(raw)
        else:
            values[field_name] = raw

    if "iotdb_password" not in values:
        raise ConfigError("IOTDB_PASSWORD is required (environment or .env)")

    try:
        return Settings(**values)
    except ValidationError as exc:
        raise ConfigError(f"invalid configuration: {exc}") from exc
