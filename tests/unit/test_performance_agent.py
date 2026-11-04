from __future__ import annotations

from pathlib import Path

import pytest

from tsdb_optimizer.agents import PerformanceMonitoringAgent
from tsdb_optimizer.monitoring.plans import PlanParser
from tsdb_optimizer.monitoring.system import SystemSample
from tsdb_optimizer.schemas.common import TimingMetrics, utc_now
from tsdb_optimizer.schemas.execution import ExecutionResult, ExecutionStatus
from tsdb_optimizer.schemas.monitoring import (
    MetricUnavailableReason,
    PlanKind,
    PlanParseStatus,
    RecordUnavailableReason,
    SystemSnapshot,
    SystemSourceType,
)
from tsdb_optimizer.strategies import StrategyStatus, build_default_registry
from phase4_support import (
    PassThroughStrategyDouble,
    TEST_ONLY_STRATEGY_ID,
    make_test_only_spec,
)

PLAN_DIR = Path(__file__).resolve().parents[1] / "fixtures" / "plans"


def _client_snapshot(*, captured_at=None) -> SystemSnapshot:
    values = {
        "source_host": "client",
        "source_type": SystemSourceType.CLIENT_HOST,
        "cpu_percent": 10.0,
        "memory_percent": 20.0,
        "disk_usage_percent": 30.0,
        "disk_read_bytes_per_sec": 40.0,
        "disk_write_bytes_per_sec": 50.0,
        "active_queries": None,
        "valid": True,
        "unavailable_fields": {
            "active_queries": MetricUnavailableReason.NOT_APPLICABLE_TO_CLIENT_HOST
        },
    }
    if captured_at is not None:
        values["captured_at"] = captured_at
    return SystemSnapshot(**values)


def _db_snapshot() -> SystemSnapshot:
    return SystemSnapshot(
        source_host="unknown",
        source_type=SystemSourceType.UNKNOWN,
        valid=False,
        unavailable_fields={
            field: MetricUnavailableReason.NO_DB_HOST_AGENT
            for field in (
                "cpu_percent",
                "memory_percent",
                "disk_usage_percent",
                "disk_read_bytes_per_sec",
                "disk_write_bytes_per_sec",
            )
        }
        | {"active_queries": MetricUnavailableReason.NO_DB_METRICS_SOURCE},
    )


def _valid_db_snapshot(*, captured_at=None) -> SystemSnapshot:
    values = {
        "source_host": "db-host",
        "source_type": SystemSourceType.DB_HOST,
        "cpu_percent": 10.0,
        "memory_percent": 20.0,
        "disk_usage_percent": 30.0,
        "disk_read_bytes_per_sec": 40.0,
        "disk_write_bytes_per_sec": 50.0,
        "active_queries": 1,
        "valid": True,
    }
    if captured_at is not None:
        values["captured_at"] = captured_at
    return SystemSnapshot(**values)


def _sample(*, captured_at=None) -> SystemSample:
    return SystemSample(
        client_host=_client_snapshot(captured_at=captured_at),
        db_host=_db_snapshot(),
    )


class FakeMonitor:
    def __init__(self, sample: SystemSample):
        self._sample = sample
        self.sample_calls = 0
        self.start_calls = 0
        self.stop_calls = 0

    @property
    def is_running(self) -> bool:
        return False

    def start(self) -> None:
        self.start_calls += 1

    def stop(self, timeout: float = 2.0) -> None:
        self.stop_calls += 1

    def sample_once(self) -> SystemSample:
        self.sample_calls += 1
        return self._sample

    def latest_sample(self) -> SystemSample:
        return self._sample

    def history(self) -> tuple[SystemSample, ...]:
        return (self._sample,)


class FakeAdapter:
    def __init__(self, result: ExecutionResult):
        self.result = result
        self.calls: list[dict[str, object]] = []

    def execute_explain(self, sql, *, query_id=None, preview_rows=1000):
        self.calls.append(
            {"sql": sql, "query_id": query_id, "preview_rows": preview_rows}
        )
        return self.result


def _execution(
    *,
    status: ExecutionStatus = ExecutionStatus.SUCCESS,
    row_count: int = 1,
    rows_preview: list[list[object]] | None = None,
    preview_truncated: bool = False,
    timeout_ms: int | None = None,
    timed_out: bool = False,
    error_type: str | None = None,
    error_message: str | None = None,
    strategy_id: str = "S0_NATIVE",
) -> ExecutionResult:
    now = utc_now()
    return ExecutionResult(
        query_id="query-1",
        execution_id="exec-1",
        status=status,
        sql_sha256="a" * 64,
        strategy_id=strategy_id,
        row_count=row_count,
        rows_preview=rows_preview or [],
        preview_truncated=preview_truncated,
        metrics=TimingMetrics(
            execute_call_ms=1.0,
            result_fetch_ms=2.0,
            client_wall_ms=3.0,
        ),
        started_at=now,
        finished_at=now,
        timeout_ms=timeout_ms,
        timed_out=timed_out,
        request_dispatched=status is ExecutionStatus.SUCCESS,
        request_completed=status is ExecutionStatus.SUCCESS,
        error_type=error_type,
        error_message=error_message,
    )


def _plan_from_fixture(name: str = "native_scan.txt"):
    text = (PLAN_DIR / name).read_text(encoding="utf-8")
    parsed = PlanParser().parse(text, model="tree", kind=PlanKind.EXPLAIN)
    assert parsed.status is PlanParseStatus.AVAILABLE
    return parsed


def test_plan_from_execution_parses_real_explain_output() -> None:
    lines = (PLAN_DIR / "native_scan.txt").read_text(encoding="utf-8").splitlines()
    result = _execution(
        row_count=len(lines),
        rows_preview=[[line] for line in lines],
    )
    agent = PerformanceMonitoringAgent(monitor=FakeMonitor(_sample()))
    parsed = agent.plan_from_execution(result)
    assert parsed.status is PlanParseStatus.AVAILABLE
    assert parsed.signature_hash == _plan_from_fixture().signature_hash
    assert parsed.node_count == 1


def test_plan_from_execution_empty_and_truncated_are_not_hashed() -> None:
    agent = PerformanceMonitoringAgent(monitor=FakeMonitor(_sample()))
    empty = agent.plan_from_execution(_execution(row_count=0))
    assert empty.status is PlanParseStatus.EMPTY
    assert empty.signature_hash is None

    truncated = agent.plan_from_execution(
        _execution(
            row_count=100,
            rows_preview=[["AlignedSeriesScan-1"]],
            preview_truncated=True,
        )
    )
    assert truncated.status is PlanParseStatus.UNAVAILABLE
    assert truncated.signature_hash is None


def test_plan_from_execution_error_is_structured() -> None:
    agent = PerformanceMonitoringAgent(monitor=FakeMonitor(_sample()))
    parsed = agent.plan_from_execution(
        _execution(
            status=ExecutionStatus.ERROR,
            error_type="TimeoutError",
            error_message="EXPLAIN timed out",
        )
    )
    assert parsed.status is PlanParseStatus.ERROR
    assert parsed.error_type == "TimeoutError"
    assert "timed out" in (parsed.error_message or "")


def test_record_execution_links_all_ids_and_available_plan() -> None:
    sample = _sample()
    monitor = FakeMonitor(sample)
    agent = PerformanceMonitoringAgent(monitor=monitor)
    parsed = _plan_from_fixture()
    result = _execution(timeout_ms=2000)

    record = agent.record_execution(result, plan_result=parsed)

    assert record.query_id == "query-1"
    assert record.execution_id == "exec-1"
    assert record.decision_id is None
    assert record.unavailable_fields["decision_id"] == (
        RecordUnavailableReason.NO_DECISION_IN_PHASE_3
    )
    assert record.snapshot_ids == [
        sample.client_host.snapshot_id,
        sample.db_host.snapshot_id,
    ]
    assert record.timeout_ms == 2000
    assert "timeout_ms" not in record.unavailable_fields
    assert record.plan_status is PlanParseStatus.AVAILABLE
    assert record.actual_plan_hash == parsed.signature_hash
    assert "actual_plan_hash" not in record.unavailable_fields
    assert record.rows_returned == 1
    assert record.success is True
    assert record.timed_out is False
    assert agent.latest_record(query_id="query-1") is record


def test_record_execution_with_decision_and_explicit_snapshots() -> None:
    monitor = FakeMonitor(_sample())
    agent = PerformanceMonitoringAgent(monitor=monitor)
    snapshots = [_client_snapshot(), _db_snapshot()]
    record = agent.record_execution(
        _execution(timeout_ms=10),
        decision_id="decision-1",
        snapshots=snapshots,
    )
    assert record.decision_id == "decision-1"
    assert record.unavailable_fields == {
        "actual_plan_hash": RecordUnavailableReason.PLAN_UNAVAILABLE
    }
    assert record.plan_status is PlanParseStatus.UNAVAILABLE
    assert monitor.sample_calls == 0
    assert record.snapshot_ids == [snapshots[0].snapshot_id, snapshots[1].snapshot_id]


def test_record_execution_marks_missing_timeout_snapshots_and_plan() -> None:
    agent = PerformanceMonitoringAgent(monitor=FakeMonitor(_sample()))
    record = agent.record_execution(_execution(), snapshots=[])
    assert record.timeout_ms is None
    assert record.unavailable_fields == {
        "decision_id": RecordUnavailableReason.NO_DECISION_IN_PHASE_3,
        "timeout_ms": RecordUnavailableReason.TIMEOUT_NOT_CONFIGURED,
        "actual_plan_hash": RecordUnavailableReason.PLAN_UNAVAILABLE,
        "snapshot_ids": RecordUnavailableReason.NO_SNAPSHOT_ATTACHED,
    }
    assert record.snapshot_ids == []
    assert record.plan_status is PlanParseStatus.UNAVAILABLE


def test_record_execution_empty_plan_uses_empty_reason() -> None:
    agent = PerformanceMonitoringAgent(monitor=FakeMonitor(_sample()))
    empty = PlanParser().parse("", model="tree", kind=PlanKind.EXPLAIN)
    record = agent.record_execution(
        _execution(timeout_ms=1), plan_result=empty, snapshots=[]
    )
    assert record.plan_status is PlanParseStatus.EMPTY
    assert record.actual_plan_hash is None
    assert record.unavailable_fields["actual_plan_hash"] == (
        RecordUnavailableReason.PLAN_EMPTY
    )


def test_record_execution_refreshes_missing_snapshot_and_notes_staleness() -> None:
    stale_captured = utc_now().replace(year=2020)
    monitor = FakeMonitor(_sample(captured_at=stale_captured))
    agent = PerformanceMonitoringAgent(monitor=monitor)
    record = agent.record_execution(_execution(timeout_ms=1))
    assert record.snapshot_ids
    assert any("stale" in warning for warning in record.warnings)


def test_record_execution_notes_stale_db_host_snapshot() -> None:
    stale = utc_now().replace(year=2020)
    sample = SystemSample(
        client_host=_client_snapshot(),
        db_host=_valid_db_snapshot(captured_at=stale),
    )
    agent = PerformanceMonitoringAgent(monitor=FakeMonitor(sample))

    record = agent.record_execution(_execution(timeout_ms=1))

    assert any(
        "DB-host system snapshot was stale" in warning
        for warning in record.warnings
    )


def test_record_execution_certification_comes_from_registry() -> None:
    agent = PerformanceMonitoringAgent(monitor=FakeMonitor(_sample()))

    native = agent.record_execution(_execution(timeout_ms=1), snapshots=[])
    unknown = agent.record_execution(
        _execution(timeout_ms=1, strategy_id="NOT_REGISTERED"),
        snapshots=[],
    )

    assert native.strategy_certification_status == "PASSED"
    assert native.runtime_result_check == "NOT_CHECKED"
    assert unknown.strategy_certification_status == "NOT_CERTIFIED"
    assert unknown.runtime_result_check == "NOT_CHECKED"


def test_record_execution_unverified_strategy_is_not_certified() -> None:
    registry = build_default_registry(allow_uncertified_for_testing=True)
    registry.register(
        PassThroughStrategyDouble(
            spec=make_test_only_spec(
                enabled=False,
                verification_status=StrategyStatus.NOT_VERIFIED,
            )
        )
    )
    agent = PerformanceMonitoringAgent(
        monitor=FakeMonitor(_sample()), registry=registry
    )

    record = agent.record_execution(
        _execution(timeout_ms=1, strategy_id="TEST_ONLY_FAST"),
        snapshots=[],
    )

    assert record.strategy_certification_status == "NOT_CERTIFIED"
    assert record.runtime_result_check == "NOT_CHECKED"


def test_record_execution_disabled_verified_strategy_is_not_certified() -> None:
    registry = build_default_registry(allow_uncertified_for_testing=True)
    registry.register(
        PassThroughStrategyDouble(
            spec=make_test_only_spec(
                enabled=False,
                verification_status=StrategyStatus.VERIFIED,
            )
        )
    )
    agent = PerformanceMonitoringAgent(
        monitor=FakeMonitor(_sample()), registry=registry
    )

    record = agent.record_execution(
        _execution(strategy_id=TEST_ONLY_STRATEGY_ID),
        snapshots=[],
    )

    assert record.strategy_certification_status == "NOT_CERTIFIED"


def test_record_execution_success_does_not_earn_certification() -> None:
    agent = PerformanceMonitoringAgent(monitor=FakeMonitor(_sample()))
    record = agent.record_execution(
        _execution(status=ExecutionStatus.ERROR, strategy_id="NOT_REGISTERED"),
        snapshots=[],
    )
    assert record.success is False
    assert record.strategy_certification_status == "NOT_CERTIFIED"


def test_record_execution_rejects_conflicting_timeout() -> None:
    agent = PerformanceMonitoringAgent(monitor=FakeMonitor(_sample()))
    with pytest.raises(ValueError, match="conflicts"):
        agent.record_execution(
            _execution(timeout_ms=10), snapshots=[], timeout_ms=20
        )


def test_explain_calls_explain_adapter_only() -> None:
    lines = (PLAN_DIR / "native_scan.txt").read_text(encoding="utf-8").splitlines()
    result = _execution(
        row_count=len(lines),
        rows_preview=[[line] for line in lines],
    )
    adapter = FakeAdapter(result)
    agent = PerformanceMonitoringAgent(
        adapter=adapter, monitor=FakeMonitor(_sample())
    )

    parsed = agent.explain(
        "SELECT s_130 FROM root.test.g_0.d_0",
        query_id="query-1",
        preview_rows=500,
    )

    assert parsed.status is PlanParseStatus.AVAILABLE
    assert adapter.calls == [
        {
            "sql": "SELECT s_130 FROM root.test.g_0.d_0",
            "query_id": "query-1",
            "preview_rows": 500,
        }
    ]
    assert "ANALYZE" not in str(adapter.calls[0]["sql"]).upper()


def test_explain_requires_adapter() -> None:
    agent = PerformanceMonitoringAgent(monitor=FakeMonitor(_sample()))
    with pytest.raises(Exception, match="no IoTDB adapter"):
        agent.explain("SELECT s_130 FROM root.test.g_0.d_0")


def test_agent_start_stop_delegates_to_monitor() -> None:
    monitor = FakeMonitor(_sample())
    agent = PerformanceMonitoringAgent(monitor=monitor)
    agent.start()
    agent.stop()
    assert monitor.start_calls == 1
    assert monitor.stop_calls == 1
