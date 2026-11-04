"""F-1: versioned resource thresholds and source-aware pressure evaluation."""

from __future__ import annotations

from datetime import timedelta

import pytest
import yaml

from tsdb_optimizer.decision.resource_policy import (
    DEFAULT_RESOURCE_POLICY_PATH,
    ResourcePolicy,
    ResourceThresholds,
    assess_resource_pressure,
    load_default_resource_policy,
    load_resource_policy,
)
from tsdb_optimizer.errors import ConfigError
from tsdb_optimizer.schemas.common import utc_now
from tsdb_optimizer.schemas.monitoring import (
    MetricUnavailableReason,
    SystemSample,
    SystemSnapshot,
    SystemSourceType,
)

_METRIC_FIELDS = (
    "cpu_percent",
    "memory_percent",
    "disk_usage_percent",
    "disk_read_bytes_per_sec",
    "disk_write_bytes_per_sec",
    "active_queries",
)


def _thresholds(value: float = 90.0) -> ResourceThresholds:
    return ResourceThresholds(
        cpu_percent=value,
        memory_percent=value,
        disk_usage_percent=value,
    )


def _policy(
    *,
    client: float = 90.0,
    db_host: float = 90.0,
    db_metrics: float = 90.0,
    enabled: bool = True,
) -> ResourcePolicy:
    return ResourcePolicy(
        schema_version=1,
        policy_version="test-v1",
        enabled=enabled,
        client_host=_thresholds(client),
        db_host=_thresholds(db_host),
        db_metrics=_thresholds(db_metrics),
    )


def _snapshot(
    source_type: SystemSourceType,
    *,
    host: str = "host",
    cpu: float = 10.0,
    memory: float = 20.0,
    disk: float = 30.0,
    disk_read: float = 1.0,
    disk_write: float = 2.0,
    active_queries: int = 0,
    valid: bool = True,
    captured_at=None,
) -> SystemSnapshot:
    return SystemSnapshot(
        source_host=host,
        source_type=source_type,
        cpu_percent=cpu,
        memory_percent=memory,
        disk_usage_percent=disk,
        disk_read_bytes_per_sec=disk_read,
        disk_write_bytes_per_sec=disk_write,
        active_queries=active_queries,
        valid=valid,
        captured_at=captured_at or utc_now(),
    )


def _unavailable_snapshot(
    source_type: SystemSourceType,
    *,
    host: str = "host",
) -> SystemSnapshot:
    return SystemSnapshot(
        source_host=host,
        source_type=source_type,
        valid=False,
        unavailable_fields={
            field: MetricUnavailableReason.NO_DB_HOST_AGENT
            for field in _METRIC_FIELDS
        },
    )


def _sample(
    *,
    client: SystemSnapshot | None = None,
    db_host: SystemSnapshot | None = None,
    db_metrics: SystemSnapshot | None = None,
) -> SystemSample:
    return SystemSample(
        client_host=client
        or _snapshot(SystemSourceType.CLIENT_HOST, host="client"),
        db_host=db_host
        or _unavailable_snapshot(SystemSourceType.DB_HOST, host="db"),
        db_metrics=db_metrics,
    )


def test_default_resource_policy_is_versioned_and_loaded_from_file() -> None:
    assert DEFAULT_RESOURCE_POLICY_PATH.is_file()
    policy = load_default_resource_policy()
    assert policy.schema_version == 1
    assert policy.policy_version == "2026-11-02.1"
    assert policy.enabled is True
    for thresholds in (policy.client_host, policy.db_host, policy.db_metrics):
        assert thresholds.cpu_percent == 90.0
        assert thresholds.memory_percent == 90.0
        assert thresholds.disk_usage_percent == 90.0


def test_threshold_is_exceeded_only_by_a_strictly_greater_value() -> None:
    policy = _policy(client=90.0)

    at_limit = assess_resource_pressure(
        _sample(
            client=_snapshot(
                SystemSourceType.CLIENT_HOST,
                cpu=90.0,
                memory=90.0,
                disk=90.0,
            )
        ),
        policy,
    )
    above_limit = assess_resource_pressure(
        _sample(
            client=_snapshot(
                SystemSourceType.CLIENT_HOST,
                cpu=90.001,
                memory=90.0,
                disk=90.0,
            )
        ),
        policy,
    )

    assert at_limit.breached is False
    assert above_limit.breached is True
    assert above_limit.breaches == (
        "client_host.cpu_percent=90.001>90.000",
    )
    assert "policy_version=test-v1" in (above_limit.warning or "")


def test_each_metric_source_is_evaluated_against_its_own_threshold() -> None:
    policy = _policy(client=50.0, db_host=60.0, db_metrics=70.0)

    client_pressure = assess_resource_pressure(
        _sample(
            client=_snapshot(SystemSourceType.CLIENT_HOST, cpu=51.0),
        ),
        policy,
    )
    db_host_pressure = assess_resource_pressure(
        _sample(
            db_host=_snapshot(SystemSourceType.DB_HOST, cpu=61.0),
        ),
        policy,
    )
    db_metrics_pressure = assess_resource_pressure(
        _sample(
            db_metrics=_snapshot(SystemSourceType.DB_METRICS, cpu=71.0),
        ),
        policy,
    )

    assert client_pressure.breaches == (
        "client_host.cpu_percent=51.000>50.000",
    )
    assert db_host_pressure.breaches == (
        "db_host.cpu_percent=61.000>60.000",
    )
    assert db_metrics_pressure.breaches == (
        "db_metrics.cpu_percent=71.000>70.000",
    )


def test_invalid_stale_and_source_mismatched_snapshots_are_not_evaluated() -> None:
    policy = _policy(client=50.0, db_host=50.0, db_metrics=50.0)
    stale_at = utc_now() - timedelta(minutes=1)
    invalid_client = _sample(
        client=_snapshot(
            SystemSourceType.CLIENT_HOST,
            cpu=99.0,
            valid=False,
        )
    )
    stale_client = _sample(
        client=_snapshot(
            SystemSourceType.CLIENT_HOST,
            cpu=99.0,
            captured_at=stale_at,
        )
    )
    stale_db_host = _sample(
        db_host=_snapshot(
            SystemSourceType.DB_HOST,
            cpu=99.0,
            captured_at=stale_at,
        )
    )
    invalid_db_metrics = _sample(
        db_metrics=_snapshot(
            SystemSourceType.DB_METRICS,
            cpu=99.0,
            valid=False,
        )
    )
    unknown_db_source = _sample(
        db_host=_snapshot(
            SystemSourceType.UNKNOWN,
            cpu=99.0,
            valid=False,
        )
    )

    for sample in (
        invalid_client,
        stale_client,
        stale_db_host,
        invalid_db_metrics,
        unknown_db_source,
    ):
        pressure = assess_resource_pressure(sample, policy)
        assert pressure.breached is False
        assert pressure.warning is None


def test_missing_sample_and_disabled_policy_do_not_breach() -> None:
    high_client = _sample(
        client=_snapshot(SystemSourceType.CLIENT_HOST, cpu=99.0)
    )

    assert assess_resource_pressure(None, _policy()).breached is False
    assert (
        assess_resource_pressure(high_client, _policy(enabled=False)).breached
        is False
    )


def test_unmodelled_io_and_active_query_values_do_not_trigger_threshold() -> None:
    pressure = assess_resource_pressure(
        _sample(
            client=_snapshot(
                SystemSourceType.CLIENT_HOST,
                cpu=10.0,
                memory=20.0,
                disk=30.0,
                disk_read=10**9,
                disk_write=10**9,
                active_queries=10_000,
            )
        ),
        _policy(client=90.0),
    )

    assert pressure.breached is False


def test_invalid_yaml_root_and_missing_fields_fail_closed(tmp_path) -> None:
    malformed = tmp_path / "malformed.yaml"
    malformed.write_text("schema_version: [\n", encoding="utf-8")
    with pytest.raises(ConfigError, match="not valid YAML"):
        load_resource_policy(malformed)

    non_mapping = tmp_path / "list.yaml"
    non_mapping.write_text("- not-a-policy\n", encoding="utf-8")
    with pytest.raises(ConfigError, match="root must be a mapping"):
        load_resource_policy(non_mapping)

    raw = yaml.safe_load(DEFAULT_RESOURCE_POLICY_PATH.read_text(encoding="utf-8"))
    unsupported = dict(raw)
    unsupported["schema_version"] = 2
    unsupported_path = tmp_path / "unsupported.yaml"
    unsupported_path.write_text(
        yaml.safe_dump(unsupported),
        encoding="utf-8",
    )
    with pytest.raises(ConfigError, match="schema_version"):
        load_resource_policy(unsupported_path)

    incomplete = dict(raw)
    incomplete.pop("db_metrics")
    incomplete_path = tmp_path / "incomplete.yaml"
    incomplete_path.write_text(
        yaml.safe_dump(incomplete),
        encoding="utf-8",
    )
    with pytest.raises(ConfigError, match="failed validation"):
        load_resource_policy(incomplete_path)


@pytest.mark.parametrize('source', ['client_host', 'db_host', 'db_metrics'])
def test_future_high_pressure_is_not_current_load(source):
    now = utc_now()
    kind = {'client_host': SystemSourceType.CLIENT_HOST,
            'db_host': SystemSourceType.DB_HOST,
            'db_metrics': SystemSourceType.DB_METRICS}[source]
    future = _snapshot(kind, cpu=99.0, captured_at=now + timedelta(days=1))
    sample = _sample(**{'client' if source == 'client_host' else source: future})
    assert not assess_resource_pressure(sample, _policy(), now=now).breached
