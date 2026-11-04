"""Execution result contract."""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from .common import (
    RECORD_SCHEMA_VERSION,
    ColumnMeta,
    TimingMetrics,
    ensure_utc,
    utc_now,
)


class ExecutionStatus(StrEnum):
    SUCCESS = "SUCCESS"
    ERROR = "ERROR"


class FetchMode(StrEnum):
    """How the client consumed the result set.

    ``FULL`` means every row was consumed; ``LIMITED`` means the caller
    intentionally stopped early; ``STREAMING`` means a caller-owned streaming
    interface consumed the result.  The Phase-4 adapter only implements FULL.
    """

    FULL = "FULL"
    STREAMING = "STREAMING"
    LIMITED = "LIMITED"


class RetryUnsafeReason(StrEnum):
    """Why the client cannot prove that a retry would be safe."""

    TIMEOUT_OR_UNKNOWN_SERVER_STATE = "TIMEOUT_OR_UNKNOWN_SERVER_STATE"
    REQUEST_COMPLETION_UNCONFIRMED = "REQUEST_COMPLETION_UNCONFIRMED"


class ExecutionResult(BaseModel):
    """One adapter execution and its complete-consumption summary.

    ``row_count`` counts every consumed row; ``rows_preview`` only retains up to
    the configured preview limit.  Full rows are intentionally not persisted.
    """

    model_config = ConfigDict(extra="forbid")

    schema_version: int = Field(default=RECORD_SCHEMA_VERSION, ge=1)
    query_id: str
    execution_id: str
    status: ExecutionStatus
    sql_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    strategy_id: str = "S0_NATIVE"
    adapter: str = "iotdb-tree"
    columns: list[ColumnMeta] = Field(default_factory=list)
    rows_preview: list[list[Any]] = Field(default_factory=list)
    preview_truncated: bool = False
    row_count: int = Field(default=0, ge=0)
    fetch_mode: FetchMode = FetchMode.FULL
    # A cache hit is a successful result that was NOT dispatched to IoTDB.  It
    # must name the real execution that populated the cache and the caller's
    # dataset epoch.  A cache miss executes normally and may carry cache_epoch
    # without either of the other two fields.
    served_from_cache: bool = False
    cache_source_execution_id: str | None = Field(default=None, max_length=200)
    cache_epoch: str | None = Field(default=None, max_length=200)
    metrics: TimingMetrics = Field(default_factory=TimingMetrics)
    started_at: datetime = Field(default_factory=utc_now)
    finished_at: datetime = Field(default_factory=utc_now)
    error_type: str | None = None
    error_message: str | None = None
    # Echo of the timeout requested from the IoTDB client; ``None`` means the
    # caller did not request a timeout.  ``timed_out`` records whether this
    # execution was observed to fail with a timeout, not a cancellation claim.
    timeout_ms: int | None = Field(default=None, ge=0)
    timed_out: bool = False
    # Retry-safety is conservative client-side evidence, not a server-side
    # cancellation proof.  Phase 4 only permits Native fallback when the first
    # request is known not to have been dispatched, or the adapter explicitly
    # marks a completed request as safe to retry.
    request_dispatched: bool = False
    request_completed: bool = False
    retry_safe: bool = False
    retry_unsafe_reason: RetryUnsafeReason | None = None
    warnings: list[str] = Field(default_factory=list)

    @field_validator("started_at", "finished_at")
    @classmethod
    def timestamps_must_be_utc(cls, value: datetime) -> datetime:
        normalized = ensure_utc(value)
        assert normalized is not None
        return normalized

    @model_validator(mode="after")
    def retry_safety_is_conservative(self) -> "ExecutionResult":
        if self.finished_at < self.started_at:
            raise ValueError("finished_at must not precede started_at")
        if self.served_from_cache:
            if self.status is not ExecutionStatus.SUCCESS:
                raise ValueError("a cache hit must be successful")
            if self.request_dispatched or self.request_completed:
                raise ValueError("a cache hit must not dispatch a server request")
            if self.retry_safe:
                raise ValueError("a cache hit is not a retryable server request")
            if self.fetch_mode is not FetchMode.FULL:
                raise ValueError("a cache hit must serve a fully consumed result")
            if not (self.cache_source_execution_id or "").strip():
                raise ValueError(
                    "a cache hit must name the execution that populated it"
                )
            if self.cache_source_execution_id == self.execution_id:
                raise ValueError(
                    "a cache hit cannot be its own population execution"
                )
            if not (self.cache_epoch or "").strip():
                raise ValueError("a cache hit must carry its dataset epoch")
            if self.preview_truncated != (
                self.row_count > len(self.rows_preview)
            ):
                raise ValueError(
                    "a cache hit preview_truncated flag must match its row counts"
                )
        else:
            if self.cache_source_execution_id is not None:
                raise ValueError(
                    "only a cache hit may name a cache population execution"
                )
        if self.status is ExecutionStatus.SUCCESS:
            if self.served_from_cache:
                # The dispatch/completion invariants were checked above.
                pass
            else:
                if not self.request_dispatched:
                    raise ValueError(
                        "a successful execution must have been dispatched"
                    )
                if not self.request_completed:
                    raise ValueError("a successful execution must have completed")
            if self.timed_out:
                raise ValueError("a successful execution cannot be timed out")
            if self.error_type is not None or self.error_message is not None:
                raise ValueError("a successful execution cannot carry an error")
        if self.request_completed and not self.request_dispatched:
            raise ValueError("a completed request must have been dispatched")
        if self.retry_safe and self.timed_out:
            raise ValueError("a timed-out request cannot be marked retry-safe")
        if (
            self.retry_safe
            and self.request_dispatched
            and not self.request_completed
        ):
            raise ValueError(
                "a request with unconfirmed completion cannot be retry-safe"
            )
        if self.timed_out and self.retry_unsafe_reason is None:
            raise ValueError("a timed-out request needs an explicit unsafe reason")
        if self.retry_safe and self.retry_unsafe_reason is not None:
            raise ValueError("a retry-safe result cannot carry an unsafe reason")
        return self
