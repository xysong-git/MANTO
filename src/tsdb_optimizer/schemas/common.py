"""Shared Phase-1 Pydantic contracts."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field, field_validator


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def new_id() -> str:
    return uuid4().hex


# Version of the Pydantic record protocols, independent of the SQLite schema
# version.  Every core record carries it so exported JSON remains interpretable
# after a future contract change.
RECORD_SCHEMA_VERSION = 1


def ensure_utc(value: datetime | None) -> datetime | None:
    """Normalize an aware timestamp to UTC; reject naive timestamps.

    Phase 1-4 records must never mix naive local times with UTC evidence.
    """

    if value is None:
        return None
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("timestamp must be timezone-aware UTC")
    return value.astimezone(timezone.utc)


class QueryRequest(BaseModel):
    """A single read-only query submitted by the CLI."""

    model_config = ConfigDict(extra="forbid")

    schema_version: int = Field(default=RECORD_SCHEMA_VERSION, ge=1)
    query_id: str = Field(default_factory=new_id)
    sql: str = Field(min_length=1)
    source: str = Field(default="cli", min_length=1, max_length=200)
    requested_at: datetime = Field(default_factory=utc_now)
    # Passed to the IoTDB client/server as the query timeout.  ``None`` means
    # this client did not set a timeout (the SDK default ``0`` is sent).  The
    # server-side meaning of ``0`` is UNKNOWN; see the timeout note in README.
    timeout_ms: int | None = Field(default=None, ge=0)

    @field_validator("sql")
    @classmethod
    def sql_must_not_blank(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("sql must not be blank")
        return value

    @field_validator("requested_at")
    @classmethod
    def requested_at_must_be_utc(cls, value: datetime) -> datetime:
        normalized = ensure_utc(value)
        assert normalized is not None
        return normalized


class TimingMetrics(BaseModel):
    """Client-side timing split.  These are not server-side execution metrics."""

    model_config = ConfigDict(extra="forbid")

    execute_call_ms: float = Field(default=0.0, ge=0.0)
    result_fetch_ms: float = Field(default=0.0, ge=0.0)
    client_wall_ms: float = Field(default=0.0, ge=0.0)


class ColumnMeta(BaseModel):
    """Server-reported column metadata.

    ``data_type`` is the raw server token (for example ``"2"`` in 2.0.6); this
    layer deliberately does not invent a friendlier type mapping.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    name: str
    data_type: str | None = None


class PreviewRow(BaseModel):
    """A preview row whose values align with ``columns``.

    When the server returns a ``Time`` column, its value is the row timestamp;
    otherwise the row only contains the returned fields.
    """

    model_config = ConfigDict(extra="forbid")

    values: list[Any] = Field(default_factory=list)
