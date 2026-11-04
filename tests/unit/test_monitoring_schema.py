from __future__ import annotations

from datetime import datetime, timedelta

import pytest
from pydantic import ValidationError

from tsdb_optimizer.schemas.common import utc_now
from tsdb_optimizer.schemas.monitoring import (
    ExecutionRecord,
    MetricUnavailableReason,
    PlanKind,
    PlanNode,
    PlanParseResult,
    PlanParseStatus,
    PlanSignature,
    PlanUnavailableReason,
    RecordUnavailableReason,
    SystemSample,
    SystemSnapshot,
    SystemSourceType,
)


def _client_snapshot(**updates: object) -> SystemSnapshot:
    values: dict[str, object] = {
        "source_host": "client-1",
        "source_type": SystemSourceType.CLIENT_HOST,
        "cpu_percent": 12.5,
        "memory_percent": 42.0,
        "disk_usage_percent": 70.0,
        "disk_read_bytes_per_sec": 100.0,
        "disk_write_bytes_per_sec": 200.0,
        "active_queries": None,
        "valid": True,
        "unavailable_fields": {
            "active_queries": MetricUnavailableReason.NOT_APPLICABLE_TO_CLIENT_HOST
        },
    }
    values.update(updates)
    return SystemSnapshot(**values)


def _db_snapshot(**updates: object) -> SystemSnapshot:
    values: dict[str, object] = {
        "source_host": "unknown",
        "source_type": SystemSourceType.UNKNOWN,
        "valid": False,
        "unavailable_fields": {
            field: MetricUnavailableReason.NO_DB_HOST_AGENT
            for field in (
                "cpu_percent",
                "memory_percent",
                "disk_usage_percent",
                "disk_read_bytes_per_sec",
                "disk_write_bytes_per_sec",
            )
        }
        | {
            "active_queries": MetricUnavailableReason.NO_DB_METRICS_SOURCE
        },
    }
    values.update(updates)
    return SystemSnapshot(**values)


def _record(**updates: object) -> ExecutionRecord:
    now = utc_now()
    values: dict[str, object] = {
        "query_id": "q-1",
        "execution_id": "e-1",
        "started_at": now,
        "finished_at": now,
        "success": True,
        "timed_out": False,
        "plan_status": PlanParseStatus.UNAVAILABLE,
        "unavailable_fields": {
            "decision_id": RecordUnavailableReason.NO_DECISION_IN_PHASE_3,
            "timeout_ms": RecordUnavailableReason.TIMEOUT_NOT_CONFIGURED,
            "actual_plan_hash": RecordUnavailableReason.PLAN_UNAVAILABLE,
            "snapshot_ids": RecordUnavailableReason.NO_SNAPSHOT_ATTACHED,
        },
    }
    values.update(updates)
    return ExecutionRecord(**values)


def test_client_snapshot_serializes_source_time_and_validity() -> None:
    snapshot = _client_snapshot()
    payload = snapshot.model_dump(mode="json")
    assert payload["source_type"] == "client_host"
    assert payload["valid_until"] > payload["captured_at"]
    assert snapshot.is_usable()
    assert not snapshot.is_fresh(now=snapshot.valid_until)


def test_missing_metric_requires_explicit_reason() -> None:
    with pytest.raises(ValidationError, match="cpu_percent is None"):
        _client_snapshot(cpu_percent=None, valid=False)


def test_present_metric_cannot_be_marked_unavailable() -> None:
    with pytest.raises(ValidationError, match="value and an unavailable reason"):
        _client_snapshot(
            cpu_percent=10.0,
            unavailable_fields={
                "active_queries": MetricUnavailableReason.NOT_APPLICABLE_TO_CLIENT_HOST,
                "cpu_percent": MetricUnavailableReason.PSUTIL_ERROR,
            },
        )


def test_unknown_unavailable_metric_name_is_rejected() -> None:
    with pytest.raises(ValidationError, match="unknown unavailable metric"):
        _client_snapshot(
            unavailable_fields={
                "active_queries": MetricUnavailableReason.NOT_APPLICABLE_TO_CLIENT_HOST,
                "bogus": MetricUnavailableReason.PSUTIL_ERROR,
            }
        )


def test_valid_snapshot_needs_core_metrics() -> None:
    with pytest.raises(ValidationError, match="valid snapshot needs"):
        _client_snapshot(
            cpu_percent=None,
            unavailable_fields={
                "active_queries": MetricUnavailableReason.NOT_APPLICABLE_TO_CLIENT_HOST,
                "cpu_percent": MetricUnavailableReason.BASELINE_NOT_ESTABLISHED,
            },
        )


def test_unknown_source_cannot_be_valid() -> None:
    with pytest.raises(ValidationError, match="unknown source cannot"):
        _db_snapshot(valid=True)


def test_db_host_snapshot_cannot_reuse_client_source() -> None:
    with pytest.raises(ValidationError, match="must not use client_host"):
        db = _client_snapshot(source_host="db-1")
        SystemSample(client_host=_client_snapshot(), db_host=db)


def test_db_host_metrics_are_explicitly_unavailable() -> None:
    sample = SystemSample(client_host=_client_snapshot(), db_host=_db_snapshot())
    assert sample.db_host_metrics_available is False
    assert sample.db_host.source_type is SystemSourceType.UNKNOWN
    assert all(
        getattr(sample.db_host, field) is None
        for field in (
            "cpu_percent",
            "memory_percent",
            "disk_usage_percent",
            "disk_read_bytes_per_sec",
            "disk_write_bytes_per_sec",
            "active_queries",
        )
    )
    payload = sample.model_dump(mode="json")
    assert payload["db_host_metrics_available"] is False


def test_freshness_can_be_tightened_but_not_extended() -> None:
    captured = utc_now()
    snapshot = _client_snapshot(captured_at=captured, stale_after_ms=1000)
    assert snapshot.is_fresh(now=captured + timedelta(milliseconds=500))
    assert not snapshot.is_fresh(
        now=captured + timedelta(milliseconds=500), max_age_ms=100
    )
    assert not snapshot.is_fresh(now=captured + timedelta(milliseconds=1001))
    with pytest.raises(ValueError, match="positive"):
        snapshot.is_fresh(max_age_ms=0)


def test_execution_record_requires_explicit_missing_reasons() -> None:
    record = _record()
    assert record.schema_version == 1
    assert record.created_at.tzinfo is not None
    assert record.unavailable_fields["decision_id"] == (
        RecordUnavailableReason.NO_DECISION_IN_PHASE_3
    )
    with pytest.raises(ValidationError, match="decision_id is missing"):
        _record(
            unavailable_fields={
                "timeout_ms": RecordUnavailableReason.TIMEOUT_NOT_CONFIGURED,
                "actual_plan_hash": RecordUnavailableReason.PLAN_UNAVAILABLE,
                "snapshot_ids": RecordUnavailableReason.NO_SNAPSHOT_ATTACHED,
            }
        )


def test_execution_record_rejects_contradictory_completion_states() -> None:
    now = utc_now()
    with pytest.raises(ValidationError, match="must not precede"):
        _record(started_at=now, finished_at=now - timedelta(milliseconds=1))
    with pytest.raises(ValidationError, match="needs finished_at"):
        _record(finished_at=None)
    with pytest.raises(ValidationError, match="cannot be timed out"):
        _record(timed_out=True)
    with pytest.raises(ValidationError, match="cannot carry an error"):
        _record(error_type="RuntimeError")


def test_unavailable_copy_withholds_every_metric_but_keeps_identity() -> None:
    snapshot = _client_snapshot(valid=True)
    withheld = snapshot.unavailable_copy(
        MetricUnavailableReason.STALE_SNAPSHOT,
        "stale snapshot withheld",
    )
    assert withheld.snapshot_id == snapshot.snapshot_id
    assert withheld.captured_at == snapshot.captured_at
    assert withheld.source_host == snapshot.source_host
    assert withheld.source_type == snapshot.source_type
    assert withheld.valid is False
    assert withheld.cpu_percent is None
    assert withheld.memory_percent is None
    assert withheld.disk_usage_percent is None
    assert withheld.active_queries is None
    assert set(withheld.unavailable_fields) == {
        "cpu_percent",
        "memory_percent",
        "disk_usage_percent",
        "disk_read_bytes_per_sec",
        "disk_write_bytes_per_sec",
        "active_queries",
    }
    assert set(withheld.unavailable_fields.values()) == {
        MetricUnavailableReason.STALE_SNAPSHOT
    }
    assert "stale snapshot withheld" in withheld.warnings


def test_execution_record_rejects_naive_timestamps() -> None:
    with pytest.raises(ValidationError, match="timezone-aware UTC"):
        _record(started_at=datetime(2026, 11, 1, 12, 0))


def test_execution_record_links_decision_execution_and_snapshot() -> None:
    record = _record(
        decision_id="d-1",
        timeout_ms=1234,
        snapshot_ids=["snap-1"],
        unavailable_fields={
            "actual_plan_hash": RecordUnavailableReason.PLAN_UNAVAILABLE
        },
    )
    assert record.decision_id == "d-1"
    assert record.execution_id == "e-1"
    assert record.timeout_ms == 1234
    assert record.snapshot_ids == ["snap-1"]


def test_execution_record_rejects_reason_for_present_field() -> None:
    with pytest.raises(ValidationError, match="has a value but an unavailable reason"):
        _record(
            decision_id="d-1",
            unavailable_fields={
                "decision_id": RecordUnavailableReason.NO_DECISION_IN_PHASE_3,
                "timeout_ms": RecordUnavailableReason.TIMEOUT_NOT_CONFIGURED,
                "actual_plan_hash": RecordUnavailableReason.PLAN_UNAVAILABLE,
                "snapshot_ids": RecordUnavailableReason.NO_SNAPSHOT_ATTACHED,
            },
        )


def test_available_plan_hash_requires_available_status() -> None:
    with pytest.raises(ValidationError, match="actual_plan_hash requires"):
        _record(
            actual_plan_hash="a" * 64,
            unavailable_fields={
                "decision_id": RecordUnavailableReason.NO_DECISION_IN_PHASE_3,
                "timeout_ms": RecordUnavailableReason.TIMEOUT_NOT_CONFIGURED,
                "snapshot_ids": RecordUnavailableReason.NO_SNAPSHOT_ATTACHED,
            },
        )


def test_plan_signature_encodes_parent_depth() -> None:
    signature = PlanSignature(
        model="tree",
        plan_kind=PlanKind.EXPLAIN,
        nodes=[
            PlanNode(operator="Root", depth=0),
            PlanNode(operator="Scan", parent_index=0, depth=1),
        ],
    )
    assert signature.nodes[1].parent_index == 0

    with pytest.raises(ValidationError, match="parent must precede"):
        PlanSignature(
            model="tree",
            plan_kind=PlanKind.EXPLAIN,
            nodes=[
                PlanNode(operator="Root", depth=0),
                PlanNode(operator="Scan", parent_index=1, depth=1),
            ],
        )


def test_plan_parse_result_status_invariants() -> None:
    available = PlanParseResult(
        status=PlanParseStatus.AVAILABLE,
        model="tree",
        plan_kind=PlanKind.EXPLAIN,
        signature=PlanSignature(
            model="tree",
            plan_kind=PlanKind.EXPLAIN,
            nodes=[PlanNode(operator="Scan", depth=0)],
        ),
        signature_hash="a" * 64,
        node_count=1,
    )
    assert available.signature is not None

    with pytest.raises(ValidationError, match="available plan needs"):
        PlanParseResult(
            status=PlanParseStatus.AVAILABLE,
            model="tree",
            plan_kind=PlanKind.EXPLAIN,
        )
    with pytest.raises(ValidationError, match="unavailable plan needs a reason"):
        PlanParseResult(
            status=PlanParseStatus.UNAVAILABLE,
            model="tree",
            plan_kind=PlanKind.EXPLAIN,
        )
    with pytest.raises(ValidationError, match="error plan needs error_type"):
        PlanParseResult(
            status=PlanParseStatus.ERROR,
            model="tree",
            plan_kind=PlanKind.EXPLAIN,
        )


def test_monitoring_models_are_json_serializable() -> None:
    payload = _record().model_dump_json()
    assert '"query_id":"q-1"' in payload
    assert '"snapshot_ids":[]' in payload


def test_freshness_rejects_future_and_has_closed_open_boundaries():
    captured = utc_now()
    snapshot = _client_snapshot(captured_at=captured, stale_after_ms=1000)
    assert not snapshot.is_fresh(now=captured - timedelta(microseconds=1))
    assert not snapshot.is_usable(now=captured - timedelta(days=1))
    assert snapshot.is_fresh(now=captured)
    assert snapshot.is_fresh(now=captured + timedelta(milliseconds=999))
    assert not snapshot.is_fresh(now=captured + timedelta(milliseconds=1000))
    assert snapshot.is_fresh(now=captured + timedelta(milliseconds=99), max_age_ms=100)
    assert not snapshot.is_fresh(now=captured + timedelta(milliseconds=100), max_age_ms=100)
