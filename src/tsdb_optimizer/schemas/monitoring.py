"""Phase-3 monitoring contracts (Agent 3).

The models deliberately distinguish the machine running the Python client from
the machine running IoTDB.  A client-host sample never becomes a DB-host
sample.  Missing values are represented by ``None`` with an explicit reason.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from enum import StrEnum
from typing import Literal

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    computed_field,
    field_validator,
    model_validator,
    model_serializer,
)

from .common import RECORD_SCHEMA_VERSION, ensure_utc, new_id, utc_now


class SystemSourceType(StrEnum):
    CLIENT_HOST = "client_host"
    DB_HOST = "db_host"
    DB_METRICS = "db_metrics"
    UNKNOWN = "unknown"


class MetricUnavailableReason(StrEnum):
    BASELINE_NOT_ESTABLISHED = "BASELINE_NOT_ESTABLISHED"
    PSUTIL_ERROR = "PSUTIL_ERROR"
    COUNTER_RESET = "COUNTER_RESET"
    INVALID_VALUE = "INVALID_VALUE"
    NO_DB_HOST_AGENT = "NO_DB_HOST_AGENT"
    NO_DB_METRICS_SOURCE = "NO_DB_METRICS_SOURCE"
    NOT_APPLICABLE_TO_CLIENT_HOST = "NOT_APPLICABLE_TO_CLIENT_HOST"
    STALE_SNAPSHOT = "STALE_SNAPSHOT"
    FUTURE_SNAPSHOT = "FUTURE_SNAPSHOT"


_METRIC_FIELDS = (
    "cpu_percent",
    "memory_percent",
    "disk_usage_percent",
    "disk_read_bytes_per_sec",
    "disk_write_bytes_per_sec",
    "active_queries",
)

_CORE_VALIDITY_FIELDS = (
    "cpu_percent",
    "memory_percent",
    "disk_usage_percent",
)


class SystemSnapshot(BaseModel):
    """One resource sample with source, time, freshness and missing markers.

    ``disk_usage_percent`` is capacity usage (0-100).  The read/write fields are
    I/O *rates* in bytes/second and must not be confused with capacity usage.
    Client and verified local DB-host collectors sample independently; the default DB source remains unavailable.
    """

    model_config = ConfigDict(extra="forbid")

    snapshot_id: str = Field(default_factory=new_id)
    captured_at: datetime = Field(default_factory=utc_now)
    source_host: str = Field(min_length=1, max_length=255)
    source_type: SystemSourceType
    cpu_measurement: Literal["cpu-times-instance-v1"] | None = None
    cpu_percent: float | None = Field(default=None, ge=0.0, le=100.0)
    memory_percent: float | None = Field(default=None, ge=0.0, le=100.0)
    disk_usage_percent: float | None = Field(default=None, ge=0.0, le=100.0)
    disk_read_bytes_per_sec: float | None = Field(default=None, ge=0.0)
    disk_write_bytes_per_sec: float | None = Field(default=None, ge=0.0)
    active_queries: int | None = Field(default=None, ge=0)
    valid: bool = False
    stale_after_ms: int = Field(default=5000, ge=1, le=3_600_000)
    unavailable_fields: dict[str, MetricUnavailableReason] = Field(default_factory=dict)
    warnings: list[str] = Field(default_factory=list)
    error_type: str | None = None
    error_message: str | None = None

    @model_serializer(mode="wrap")
    def serialize_measurement(self, handler):
        body=handler(self)
        if self.cpu_measurement is None:body.pop("cpu_measurement",None)
        return body

    @field_validator("captured_at")
    @classmethod
    def captured_at_must_be_timezone_aware(cls, value: datetime) -> datetime:
        if value.tzinfo is None:
            raise ValueError("captured_at must be timezone-aware UTC")
        return value.astimezone(timezone.utc)

    @model_validator(mode="after")
    def missing_values_need_reasons(self) -> "SystemSnapshot":
        for field_name in _METRIC_FIELDS:
            value = getattr(self, field_name)
            reason = self.unavailable_fields.get(field_name)
            if value is None and reason is None:
                raise ValueError(f"{field_name} is None but has no unavailable reason")
            if value is not None and reason is not None:
                raise ValueError(f"{field_name} has a value and an unavailable reason")
        unknown = set(self.unavailable_fields) - set(_METRIC_FIELDS)
        if unknown:
            raise ValueError(f"unknown unavailable metric fields: {sorted(unknown)}")
        if self.valid:
            if self.source_type is SystemSourceType.UNKNOWN:
                raise ValueError("an unknown source cannot produce a valid snapshot")
            missing_core = [
                name for name in _CORE_VALIDITY_FIELDS if getattr(self, name) is None
            ]
            if missing_core:
                raise ValueError(
                    "a valid snapshot needs CPU, memory and disk-usage values; "
                    f"missing: {missing_core}"
                )
        return self

    @computed_field  # type: ignore[prop-decorator]
    @property
    def valid_until(self) -> datetime:
        """UTC instant after which this snapshot is stale."""

        return self.captured_at + timedelta(milliseconds=self.stale_after_ms)

    def is_fresh(
        self,
        now: datetime | None = None,
        max_age_ms: int | None = None,
    ) -> bool:
        """Return whether the snapshot is within its validity window.

        ``max_age_ms`` can tighten, but never extend, the snapshot's own
        ``stale_after_ms``.
        """

        reference = now or utc_now()
        if reference.tzinfo is None:
            reference = reference.replace(tzinfo=timezone.utc)
        else:
            reference = reference.astimezone(timezone.utc)
        limit_ms = self.stale_after_ms
        if max_age_ms is not None:
            if max_age_ms < 1:
                raise ValueError("max_age_ms must be positive")
            limit_ms = min(limit_ms, max_age_ms)
        return self.captured_at <= reference < self.captured_at + timedelta(milliseconds=limit_ms)

    def is_usable(
        self,
        now: datetime | None = None,
        max_age_ms: int | None = None,
    ) -> bool:
        return self.valid and self.is_fresh(now=now, max_age_ms=max_age_ms)

    def unavailable_copy(
        self,
        reason: MetricUnavailableReason,
        warning: str,
    ) -> "SystemSnapshot":
        """Return a same-source snapshot with every metric withheld.

        This is used when a stale DB-host or DB-metrics snapshot must not be
        exposed as live load evidence.  It preserves identity/time/source so the
        audit trail still shows which sample was degraded.
        """

        return SystemSnapshot(
            snapshot_id=self.snapshot_id,
            captured_at=self.captured_at,
            source_host=self.source_host,
            source_type=self.source_type,
            valid=False,
            stale_after_ms=self.stale_after_ms,
            unavailable_fields={field: reason for field in _METRIC_FIELDS},
            warnings=[*self.warnings, warning],
        )


class SystemSample(BaseModel):
    """A client-host snapshot paired with a separate DB-host snapshot."""

    model_config = ConfigDict(extra="forbid")

    client_host: SystemSnapshot
    db_host: SystemSnapshot
    db_metrics: SystemSnapshot | None = None

    @model_validator(mode="after")
    def sources_must_not_be_confused(self) -> "SystemSample":
        if self.client_host.source_type is not SystemSourceType.CLIENT_HOST:
            raise ValueError("client_host snapshot must have source_type=client_host")
        if self.db_host.source_type is SystemSourceType.CLIENT_HOST:
            raise ValueError("db_host snapshot must not use client_host psutil data")
        if self.db_host.source_type not in {
            SystemSourceType.DB_HOST,
            SystemSourceType.UNKNOWN,
        }:
            raise ValueError("db_host snapshot has an invalid source type")
        if self.db_metrics is not None:
            if self.db_metrics.source_type is not SystemSourceType.DB_METRICS:
                raise ValueError("db_metrics snapshot must have source_type=db_metrics")
        return self

    @computed_field  # type: ignore[prop-decorator]
    @property
    def db_host_metrics_available(self) -> bool:
        return self.db_host.valid and self.db_host.source_type is SystemSourceType.DB_HOST


class PlanKind(StrEnum):
    EXPLAIN = "EXPLAIN"
    EXPLAIN_ANALYZE = "EXPLAIN_ANALYZE"
    UNKNOWN = "UNKNOWN"


class PlanParseStatus(StrEnum):
    AVAILABLE = "AVAILABLE"
    EMPTY = "EMPTY"
    UNAVAILABLE = "UNAVAILABLE"
    ERROR = "ERROR"


class PlanUnavailableReason(StrEnum):
    TABLE_MODEL_EXPLAIN_NOT_VERIFIED = "TABLE_MODEL_EXPLAIN_NOT_VERIFIED"
    NO_RECOGNIZED_PLAN_NODES = "NO_RECOGNIZED_PLAN_NODES"
    EXPLAIN_EXECUTION_FAILED = "EXPLAIN_EXECUTION_FAILED"
    PLAN_PREVIEW_TRUNCATED = "PLAN_PREVIEW_TRUNCATED"
    PLAN_PARSE_ERROR = "PLAN_PARSE_ERROR"
    UNKNOWN_DATABASE_MODEL = "UNKNOWN_DATABASE_MODEL"


class PlanNode(BaseModel):
    """Stable structural node extracted from an EXPLAIN plan.

    ``parent_index`` and ``depth`` encode the operator tree without retaining
    ASCII-art coordinates, which are volatile presentation details.
    """

    model_config = ConfigDict(extra="forbid")

    operator: str = Field(min_length=1)
    attributes: dict[str, str] = Field(default_factory=dict)
    parent_index: int | None = Field(default=None, ge=0)
    depth: int = Field(default=0, ge=0)


class PlanSignature(BaseModel):
    """Canonical plan representation used for hashing.

    Volatile values (node IDs, partitions, addresses, costs, runtime counters)
    are excluded before this object is constructed.
    """

    model_config = ConfigDict(extra="forbid")

    signature_version: int = Field(default=1, ge=1)
    model: Literal["tree", "table"]
    plan_kind: PlanKind
    nodes: list[PlanNode] = Field(min_length=1)

    @model_validator(mode="after")
    def tree_links_are_consistent(self) -> "PlanSignature":
        for index, node in enumerate(self.nodes):
            if index == 0:
                if node.parent_index is not None or node.depth != 0:
                    raise ValueError("the first plan node must be a depth-0 root")
                continue
            if node.parent_index is None:
                if node.depth != 0:
                    raise ValueError("a root plan node must have depth 0")
                continue
            if node.parent_index >= index:
                raise ValueError("a plan node parent must precede the child")
            parent = self.nodes[node.parent_index]
            if parent.depth + 1 != node.depth:
                raise ValueError("plan node depth does not match its parent")
        return self


class PlanParseResult(BaseModel):
    """Result of parsing one EXPLAIN output without executing the query again."""

    model_config = ConfigDict(extra="forbid")

    status: PlanParseStatus
    model: Literal["tree", "table", "unknown"]
    plan_kind: PlanKind = PlanKind.UNKNOWN
    signature: PlanSignature | None = None
    signature_hash: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    node_count: int = Field(default=0, ge=0)
    raw_line_count: int = Field(default=0, ge=0)
    excluded_attributes: list[str] = Field(default_factory=list)
    unavailable_reason: PlanUnavailableReason | None = None
    warnings: list[str] = Field(default_factory=list)
    error_type: str | None = None
    error_message: str | None = None

    @model_validator(mode="after")
    def status_matches_payload(self) -> "PlanParseResult":
        if self.status is PlanParseStatus.AVAILABLE:
            if self.signature is None or self.signature_hash is None:
                raise ValueError("an available plan needs a signature and hash")
            if self.node_count < 1:
                raise ValueError("an available plan needs at least one node")
            if self.node_count != len(self.signature.nodes):
                raise ValueError("node_count does not match signature nodes")
            if self.unavailable_reason is not None:
                raise ValueError("an available plan cannot have an unavailable reason")
            if self.error_type is not None or self.error_message is not None:
                raise ValueError("an available plan cannot carry an error")
        else:
            if self.signature is not None or self.signature_hash is not None:
                raise ValueError("only an available plan may expose a signature")
            if self.node_count != 0:
                raise ValueError("a non-available plan must have node_count=0")
            if self.status is PlanParseStatus.UNAVAILABLE:
                if self.unavailable_reason is None:
                    raise ValueError("an unavailable plan needs a reason")
            elif self.unavailable_reason is not None:
                raise ValueError("only an unavailable plan may have a reason")
            if self.status is PlanParseStatus.ERROR:
                if self.error_type is None:
                    raise ValueError("an error plan needs error_type")
            elif self.error_type is not None or self.error_message is not None:
                raise ValueError("only an error plan may carry an error")
        return self


class PlanComparisonStatus(StrEnum):
    MATCHED = "MATCHED"
    MISMATCHED = "MISMATCHED"
    UNAVAILABLE = "UNAVAILABLE"


class PlanComparison(BaseModel):
    """Comparison of two stable plan hashes; no raw-text comparison."""

    model_config = ConfigDict(extra="forbid")

    status: PlanComparisonStatus
    expected_hash: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    actual_hash: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    reason: str | None = None


class RecordUnavailableReason(StrEnum):
    NO_DECISION_IN_PHASE_3 = "NO_DECISION_IN_PHASE_3"
    TIMEOUT_NOT_CONFIGURED = "TIMEOUT_NOT_CONFIGURED"
    PLAN_UNAVAILABLE = "PLAN_UNAVAILABLE"
    PLAN_EMPTY = "PLAN_EMPTY"
    NO_SNAPSHOT_ATTACHED = "NO_SNAPSHOT_ATTACHED"


_RECORD_OPTIONAL_FIELDS = {
    "decision_id": RecordUnavailableReason.NO_DECISION_IN_PHASE_3,
    "timeout_ms": RecordUnavailableReason.TIMEOUT_NOT_CONFIGURED,
    "actual_plan_hash": RecordUnavailableReason.PLAN_UNAVAILABLE,
    "snapshot_ids": RecordUnavailableReason.NO_SNAPSHOT_ATTACHED,
}


class ExecutionRecord(BaseModel):
    """Agent 3's query-level record, linked to snapshots and (when present) a decision.

    Phase 3 has no Agent 2, so ``decision_id`` is ``None`` with an explicit
    reason unless a caller supplies one.  This model never invents a decision.
    """

    model_config = ConfigDict(extra="forbid")

    schema_version: int = Field(default=RECORD_SCHEMA_VERSION, ge=1)
    query_id: str = Field(min_length=1)
    decision_id: str | None = None
    execution_id: str = Field(min_length=1)
    strategy_id: str = Field(default="S0_NATIVE", min_length=1)
    created_at: datetime = Field(default_factory=utc_now)
    started_at: datetime
    finished_at: datetime | None = None
    execute_call_ms: float | None = Field(default=None, ge=0.0)
    result_fetch_ms: float | None = Field(default=None, ge=0.0)
    client_wall_ms: float | None = Field(default=None, ge=0.0)
    rows_returned: int | None = Field(default=None, ge=0)
    success: bool
    timed_out: bool
    timeout_ms: int | None = Field(default=None, ge=0)
    error_type: str | None = None
    error_message: str | None = None
    plan_status: PlanParseStatus | None = None
    actual_plan_hash: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    strategy_certification_status: Literal[
        "PASSED", "FAILED", "NOT_CERTIFIED"
    ] = "NOT_CERTIFIED"
    runtime_result_check: Literal["NOT_CHECKED", "MATCHED", "MISMATCHED"] = "NOT_CHECKED"
    snapshot_ids: list[str] = Field(default_factory=list)
    unavailable_fields: dict[str, RecordUnavailableReason] = Field(default_factory=dict)
    warnings: list[str] = Field(default_factory=list)

    @field_validator("created_at", "started_at", "finished_at")
    @classmethod
    def timestamps_must_be_utc(cls, value: datetime | None) -> datetime | None:
        return ensure_utc(value)

    @field_validator("snapshot_ids")
    @classmethod
    def snapshot_ids_must_be_unique(cls, value: list[str]) -> list[str]:
        if len(value) != len(set(value)):
            raise ValueError("snapshot_ids must be unique")
        return value

    @model_validator(mode="after")
    def missing_fields_need_reasons(self) -> "ExecutionRecord":
        unknown = set(self.unavailable_fields) - set(_RECORD_OPTIONAL_FIELDS)
        if unknown:
            raise ValueError(f"unknown unavailable record fields: {sorted(unknown)}")

        expected_missing = {
            name
            for name in _RECORD_OPTIONAL_FIELDS
            if getattr(self, name) is None
            or (name == "snapshot_ids" and not self.snapshot_ids)
        }
        for name in expected_missing:
            if name not in self.unavailable_fields:
                raise ValueError(f"{name} is missing but has no unavailable reason")
        for name in set(self.unavailable_fields) - expected_missing:
            raise ValueError(f"{name} has a value but an unavailable reason")

        if self.plan_status is PlanParseStatus.AVAILABLE:
            if self.actual_plan_hash is None:
                raise ValueError("an available plan needs actual_plan_hash")
        elif self.actual_plan_hash is not None:
            raise ValueError("actual_plan_hash requires plan_status=AVAILABLE")

        if self.finished_at is not None and self.finished_at < self.started_at:
            raise ValueError("finished_at must not precede started_at")
        if (self.success or self.timed_out) and self.finished_at is None:
            raise ValueError(
                "a completed or timed-out record needs finished_at"
            )
        if self.success and self.timed_out:
            raise ValueError("a successful record cannot be timed out")
        if self.success and (
            self.error_type is not None or self.error_message is not None
        ):
            raise ValueError("a successful record cannot carry an error")
        return self
