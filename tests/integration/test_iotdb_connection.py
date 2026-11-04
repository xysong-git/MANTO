"""Integration tests against the real IoTDB test instance.

Run only when explicitly enabled:
    IOTDB_INTEGRATION=1 IOTDB_PASSWORD=... pytest -m integration

The default SQL is a known Phase-0 fixture query.  Override with IOTDB_TEST_SQL.
The test never writes to IoTDB.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from tsdb_optimizer.adapters.iotdb.tree_adapter import IoTDBTreeAdapter
from tsdb_optimizer.agents import PerformanceMonitoringAgent
from tsdb_optimizer.cli import main
from tsdb_optimizer.config import load_settings
from tsdb_optimizer.decision import DecisionMode
from tsdb_optimizer.errors import ConfigError
from tsdb_optimizer.execution import DecisionExecutionService
from tsdb_optimizer.monitoring.system import (
    PsutilSystemSampler,
    UnavailableDbHostSampler,
)
from tsdb_optimizer.schemas import QueryRequest
from tsdb_optimizer.schemas.monitoring import (
    RecordUnavailableReason,
    SystemSourceType,
)
from tsdb_optimizer.sql_guard import sql_sha256, validate_readonly_sql
from tsdb_optimizer.storage import SQLiteStore

pytestmark = pytest.mark.integration

DEFAULT_SQL = (
    "SELECT s_1666, s_1766 FROM root.db800.g_0.d_0, "
    "root.db800.g_0.d_1 WHERE time = 1640966450000"
)


def _require_integration() -> None:
    if os.environ.get("IOTDB_INTEGRATION") != "1":
        pytest.skip("set IOTDB_INTEGRATION=1 to run real IoTDB tests")
    try:
        load_settings()
    except ConfigError:
        pytest.skip("IOTDB_PASSWORD is required for real IoTDB tests")


def _test_sql() -> str:
    return os.environ.get("IOTDB_TEST_SQL", DEFAULT_SQL)


def test_doctor_and_readonly_query(tmp_path: Path) -> None:
    _require_integration()
    settings = load_settings()
    adapter = IoTDBTreeAdapter(settings)
    try:
        report = adapter.doctor()
        assert report.connected is True, report.error_message
        assert report.ok is True, report.warnings
        assert report.server is not None
        assert report.server.model == "tree"
        assert report.server.version
        assert report.capabilities.show_version is True
        assert report.capabilities.show_databases is True

        cleaned = validate_readonly_sql(_test_sql())
        request = QueryRequest(sql=cleaned, source="pytest-integration")
        result = adapter.execute_readonly(request, preview_rows=5)
        assert result.status.value == "SUCCESS", result.error_message
        assert result.row_count > 0
        assert result.metrics.execute_call_ms >= 0
        assert result.metrics.result_fetch_ms >= 0
        assert result.metrics.client_wall_ms >= 0

        store = SQLiteStore(tmp_path / "integration.sqlite3")
        store.initialize()
        store.record_query(request, sql_sha256(cleaned))
        store.record_execution(result)
        stored = store.get_execution(result.execution_id)
        assert stored is not None
        assert stored["status"] == "SUCCESS"
        assert stored["row_count"] == result.row_count
    finally:
        adapter.close()


def test_phase3_explain_monitoring_and_record_association() -> None:
    _require_integration()
    settings = load_settings()
    adapter = IoTDBTreeAdapter(settings)
    agent = PerformanceMonitoringAgent(
        adapter=adapter,
        monitor_interval_s=1.0,
    )
    try:
        # The first CPU/I-O sample establishes baselines; the second provides
        # real client-host values.  DB-host metrics remain explicitly unknown.
        first = agent.sample_once()
        agent.sample_once()
        latest = agent.latest_sample()
        assert latest is not None
        assert first.client_host.source_type is SystemSourceType.CLIENT_HOST
        assert latest.db_host.source_type is SystemSourceType.UNKNOWN
        assert latest.db_host.valid is False
        assert latest.db_host.cpu_percent is None
        assert latest.db_host.memory_percent is None
        assert latest.db_host.disk_read_bytes_per_sec is None
        assert latest.db_metrics is None
        assert latest.db_host_metrics_available is False

        cleaned = validate_readonly_sql(_test_sql())
        request = QueryRequest(
            sql=cleaned,
            source="pytest-integration-monitoring",
            timeout_ms=30_000,
        )
        result = adapter.execute_readonly(request, preview_rows=5)
        assert result.status.value == "SUCCESS", result.error_message
        assert result.timeout_ms == 30_000
        assert result.timed_out is False
        assert result.row_count > 0

        plan = agent.explain(
            cleaned,
            query_id=request.query_id,
            preview_rows=1000,
        )
        assert plan.status.value == "AVAILABLE", (
            plan.unavailable_reason,
            plan.error_type,
            plan.error_message,
            plan.warnings,
        )
        assert plan.signature_hash is not None
        assert plan.node_count > 0

        record = agent.record_execution(
            result,
            plan_result=plan,
            snapshots=agent.latest_sample(),
        )
        assert record.query_id == request.query_id
        assert record.execution_id == result.execution_id
        assert record.decision_id is None
        assert record.unavailable_fields["decision_id"] == (
            RecordUnavailableReason.NO_DECISION_IN_PHASE_3
        )
        assert record.timeout_ms == 30_000
        assert record.snapshot_ids
        assert record.plan_status.value == "AVAILABLE"
        assert record.actual_plan_hash == plan.signature_hash
        assert record.rows_returned == result.row_count
        assert record.runtime_result_check == "NOT_CHECKED"
    finally:
        agent.stop()
        adapter.close()


def test_cli_query_end_to_end(tmp_path: Path, capsys) -> None:
    _require_integration()
    cleaned = validate_readonly_sql(_test_sql())
    database = tmp_path / "cli.sqlite3"
    code = main(
        [
            "--sqlite",
            str(database),
            "query",
            "--sql",
            cleaned,
            "--source",
            "pytest-integration",
        ]
    )
    output = json.loads(capsys.readouterr().out)
    assert code == 0, output
    assert output["status"] == "SUCCESS"
    assert output["row_count"] > 0
    assert database.exists()

    store = SQLiteStore(database)
    assert store.count("queries") == 1
    assert store.count("executions") == 1


def test_phase4_native_decision_execution_and_persistence(tmp_path: Path) -> None:
    _require_integration()
    settings = load_settings()
    adapter = IoTDBTreeAdapter(settings)
    store = SQLiteStore(tmp_path / "phase4-service.sqlite3")
    try:
        report = adapter.doctor()
        assert report.ok is True, report.warnings
        assert report.server is not None
        server_version = report.server.version
        database_model = report.server.model

        def explain_must_not_run(*args, **kwargs):
            del args, kwargs
            raise AssertionError("Phase-4 normal execution must not call EXPLAIN")

        adapter.execute_explain = explain_must_not_run  # type: ignore[method-assign]

        service = DecisionExecutionService(
            adapter=adapter,
            store=store,
            preview_rows=5,
            server_version=server_version,
            database_model=database_model,
        )
        cleaned = validate_readonly_sql(_test_sql())
        run = service.run(
            cleaned,
            mode=DecisionMode.RULE_BASED,
            source="pytest-integration-phase4",
            timeout_ms=30_000,
            preview_rows=5,
        )

        assert run.execution.final_status.value == "SUCCESS"
        assert run.execution.fallback_used is False
        assert run.execution.selected_strategy_id == "S0_NATIVE"
        assert run.decision.selected_strategy_id == "S0_NATIVE"
        assert run.decision.strategy_version == "0.1.0"
        assert run.decision.candidate_sql == cleaned
        assert run.decision.original_sql == cleaned
        assert run.decision.decision_mode is DecisionMode.RULE_BASED
        assert run.decision.expected_latency_ms is None
        assert run.decision.cost_estimate.source.value == "UNKNOWN"
        assert "NO_VERIFIED_NON_NATIVE_STRATEGY" not in run.decision.warnings
        assert any(
            warning.startswith("COST_UNKNOWN")
            for warning in run.decision.warnings
        )
        assert [entry.strategy_id for entry in run.decision.considered] == [
            "S0_NATIVE",
            "S1_REPEAT_CACHE",
        ]
        considered = run.decision.considered[0]
        assert considered.strategy_id == "S0_NATIVE"
        assert considered.selected is True
        assert considered.validation is not None
        assert considered.validation.passed is True
        cache_entry = run.decision.considered[1]
        assert cache_entry.applicable is False
        assert cache_entry.selected is False
        assert "dataset epoch" in (cache_entry.rejection_reason or "")

        attempt = run.execution.attempts[0]
        assert attempt.attempt_index == 1
        assert attempt.is_fallback is False
        assert attempt.requested_strategy_id == "S0_NATIVE"
        assert attempt.execution.row_count > 0
        assert attempt.execution.timeout_ms == 30_000
        assert attempt.execution.timed_out is False
        assert attempt.execution.request_dispatched is True
        assert attempt.execution.request_completed is True
        assert attempt.execution.retry_safe is False

        assert store.count("queries") == 1
        assert store.count("decisions") == 1
        assert store.count("executions") == 1
        stored = store.get_decision(run.decision.decision_id)
        assert stored is not None
        assert stored["selected_strategy_id"] == "S0_NATIVE"
        assert stored["candidate_sql"] == cleaned
        rows = store.get_executions_for_decision(run.decision.decision_id)
        assert len(rows) == 1
        assert rows[0]["strategy_id"] == "S0_NATIVE"
        assert rows[0]["attempt_index"] == 1
        assert rows[0]["is_fallback"] == 0
        assert rows[0]["row_count"] == attempt.execution.row_count
        assert rows[0]["request_dispatched"] == 1
        assert rows[0]["request_completed"] == 1
    finally:
        adapter.close()


def test_phase4_cli_optimize_end_to_end(tmp_path: Path, capsys) -> None:
    _require_integration()
    cleaned = validate_readonly_sql(_test_sql())
    database = tmp_path / "phase4-cli.sqlite3"

    code = main(
        [
            "--sqlite",
            str(database),
            "optimize",
            "--sql",
            cleaned,
            "--source",
            "pytest-integration-phase4-cli",
            "--mode",
            "baseline",
        ]
    )
    output = json.loads(capsys.readouterr().out)
    assert code == 0, output
    assert output["decision"]["selected_strategy_id"] == "S0_NATIVE"
    assert output["decision"]["candidate_sql"] == cleaned
    assert output["execution"]["final_status"] == "SUCCESS"
    assert output["execution"]["fallback_used"] is False
    assert output["execution"]["attempts"][0]["requested_strategy_id"] == (
        "S0_NATIVE"
    )

    store = SQLiteStore(database)
    assert store.count("queries") == 1
    assert store.count("decisions") == 1
    assert store.count("executions") == 1
