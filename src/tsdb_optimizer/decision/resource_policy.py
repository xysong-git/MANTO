"""Versioned resource-pressure policy for Agent 2.

The policy is deliberately small and deterministic.  It does not claim to know
IoTDB's internal safe limits; it defines conservative, operator-editable
thresholds that veto non-baseline strategies when a *usable* resource snapshot
exceeds them.  A snapshot is usable only when its source type matches the
expected source, ``valid`` is true, and it is fresh at decision time.

Client-host, DB-host and DB-metrics snapshots are evaluated separately.  A
client psutil sample is never treated as a DB-host sample, and missing or
invalid values are skipped rather than interpreted as low load.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from functools import lru_cache
from pathlib import Path

import yaml
from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator

from ..errors import ConfigError
from ..schemas.common import utc_now
from ..schemas.monitoring import SystemSample, SystemSnapshot, SystemSourceType

RESOURCE_POLICY_SCHEMA_VERSION = 1
DEFAULT_RESOURCE_POLICY_PATH = (
    Path(__file__).resolve().parents[3] / "configs" / "resource_policy.yaml"
)

_CORE_METRICS = ("cpu_percent", "memory_percent", "disk_usage_percent")


class ResourceThresholds(BaseModel):
    """Explicit upper bounds for the three comparable percentage metrics.

    Values equal to a threshold are not treated as exceeded; only a strictly
    greater value triggers the conservative gate.  I/O rates and active-query
    counts are intentionally not part of this minimal policy because no
    verified universal safe limit exists for them in the current project.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    cpu_percent: float = Field(ge=0.0, le=100.0)
    memory_percent: float = Field(ge=0.0, le=100.0)
    disk_usage_percent: float = Field(ge=0.0, le=100.0)


class ResourcePolicy(BaseModel):
    """Versioned thresholds, separately configured for each metric source."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: int = Field(ge=1)
    policy_version: str = Field(min_length=1, max_length=100)
    enabled: bool = True
    client_host: ResourceThresholds
    db_host: ResourceThresholds
    db_metrics: ResourceThresholds

    @model_validator(mode="after")
    def schema_version_is_supported(self) -> "ResourcePolicy":
        if self.schema_version != RESOURCE_POLICY_SCHEMA_VERSION:
            raise ValueError(
                f"unsupported resource policy schema_version "
                f"{self.schema_version}; expected {RESOURCE_POLICY_SCHEMA_VERSION}"
            )
        return self


@dataclass(frozen=True)
class ResourcePressure:
    """The result of evaluating one sample against one policy version."""

    policy_version: str
    breaches: tuple[str, ...] = ()

    @property
    def breached(self) -> bool:
        return bool(self.breaches)

    @property
    def details(self) -> str:
        return ", ".join(self.breaches)

    @property
    def warning(self) -> str | None:
        if not self.breached:
            return None
        return (
            "RESOURCE_THRESHOLD_EXCEEDED: "
            f"policy_version={self.policy_version}; {self.details}; "
            "conservative baseline forced"
        )


def load_resource_policy(path: Path | str | None = None) -> ResourcePolicy:
    """Load and strictly validate the external, versioned policy.

    Missing/unreadable files, malformed YAML and schema violations fail closed
    as :class:`ConfigError`.  A partial policy is never returned.
    """

    policy_path = Path(path) if path is not None else DEFAULT_RESOURCE_POLICY_PATH
    try:
        raw_text = policy_path.read_text(encoding="utf-8")
    except OSError as exc:
        raise ConfigError(
            f"could not read resource policy {policy_path}: {exc}"
        ) from exc
    try:
        raw = yaml.safe_load(raw_text)
    except yaml.YAMLError as exc:
        raise ConfigError(
            f"resource policy is not valid YAML: {policy_path}: {exc}"
        ) from exc
    if not isinstance(raw, dict):
        raise ConfigError(f"resource policy root must be a mapping: {policy_path}")
    try:
        return ResourcePolicy.model_validate(raw)
    except ValidationError as exc:
        raise ConfigError(
            f"resource policy failed validation: {policy_path}: {exc}"
        ) from exc


@lru_cache(maxsize=1)
def load_default_resource_policy() -> ResourcePolicy:
    """Load the repository policy once per process."""

    return load_resource_policy(DEFAULT_RESOURCE_POLICY_PATH)


def _snapshot_breaches(
    *,
    source_label: str,
    snapshot: SystemSnapshot,
    expected_source: SystemSourceType,
    thresholds: ResourceThresholds,
    now: datetime,
) -> list[str]:
    # Source identity, validity and freshness are all required.  In particular,
    # an invalid UNKNOWN DB snapshot with stale copied values is not evidence
    # of current DB load and must never be compared with a threshold.
    if snapshot.source_type is not expected_source:
        return []
    if not snapshot.valid or not snapshot.is_fresh(now=now):
        return []

    breaches: list[str] = []
    for metric_name in _CORE_METRICS:
        value = getattr(snapshot, metric_name)
        if value is None:
            continue
        limit = getattr(thresholds, metric_name)
        if value > limit:
            breaches.append(
                f"{source_label}.{metric_name}={value:.3f}>{limit:.3f}"
            )
    return breaches


def assess_resource_pressure(
    sample: SystemSample | None,
    policy: ResourcePolicy,
    *,
    now: datetime | None = None,
) -> ResourcePressure:
    """Evaluate only usable snapshots against the configured thresholds.

    Missing samples, unavailable metrics, invalid snapshots, stale snapshots
    and source mismatches are skipped.  They are not converted to zero load,
    and this function does not fabricate a breach from absent evidence.
    """

    if sample is None or not policy.enabled:
        return ResourcePressure(policy_version=policy.policy_version)

    reference = now or utc_now()
    breaches: list[str] = []
    breaches.extend(
        _snapshot_breaches(
            source_label=SystemSourceType.CLIENT_HOST.value,
            snapshot=sample.client_host,
            expected_source=SystemSourceType.CLIENT_HOST,
            thresholds=policy.client_host,
            now=reference,
        )
    )
    breaches.extend(
        _snapshot_breaches(
            source_label=SystemSourceType.DB_HOST.value,
            snapshot=sample.db_host,
            expected_source=SystemSourceType.DB_HOST,
            thresholds=policy.db_host,
            now=reference,
        )
    )
    if sample.db_metrics is not None:
        breaches.extend(
            _snapshot_breaches(
                source_label=SystemSourceType.DB_METRICS.value,
                snapshot=sample.db_metrics,
                expected_source=SystemSourceType.DB_METRICS,
                thresholds=policy.db_metrics,
                now=reference,
            )
        )
    return ResourcePressure(
        policy_version=policy.policy_version,
        breaches=tuple(breaches),
    )


__all__ = [
    "DEFAULT_RESOURCE_POLICY_PATH",
    "RESOURCE_POLICY_SCHEMA_VERSION",
    "ResourcePolicy",
    "ResourcePressure",
    "ResourceThresholds",
    "assess_resource_pressure",
    "load_default_resource_policy",
    "load_resource_policy",
]
