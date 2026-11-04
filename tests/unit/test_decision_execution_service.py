"""Decision -> persistence -> execution service tests."""

from __future__ import annotations

import logging
import sqlite3

import pytest

from tsdb_optimizer.decision import DecisionEngine, DecisionMode
from tsdb_optimizer.errors import AdapterError, ExecutionError, UnsafeSQLError
from tsdb_optimizer.execution import DecisionExecutionService
from tsdb_optimizer.schemas import RetryUnsafeReason
from tsdb_optimizer.schemas.execution import ExecutionStatus
from tsdb_optimizer.storage import SQLiteStore
from tsdb_optimizer.strategies import build_default_registry
from phase4_support import (
    ScriptedAdapter,
    StaticCostEstimator,
    TEST_ONLY_STRATEGY_ID,
    TEST_SQL,
    PassThroughStrategyDouble,
    execution_result,
    make_test_only_spec,
)

SERVER_VERSION = "2.0.6"
DATABASE_MODEL = "tree"


def _fast_registry():
    registry = build_default_registry(allow_uncertified_for_testing=True)
    registry.register(PassThroughStrategyDouble(spec=make_test_only_spec()))
    return registry


def _fast_engine(registry):
    return DecisionEngine(
        registry,
        cost_estimator=StaticCostEstimator(
            {"S0_NATIVE": 10.0, TEST_ONLY_STRATEGY_ID: 1.0}
        ),
    )


def _service(tmp_path, adapter, *, registry=None, engine=None):
    store = SQLiteStore(tmp_path / "service.sqlite3")
    return (
        DecisionExecutionService(
            adapter=adapter,
            store=store,
            registry=registry,
            engine=engine,
            server_version=SERVER_VERSION,
            database_model=DATABASE_MODEL,
        ),
        store,
    )


def _success(query_id: str, strategy_id: str):
    return execution_result(query_id=query_id, strategy_id=strategy_id)


def test_default_service_executes_and_persists_native_run(tmp_path) -> None:
    adapter = ScriptedAdapter([])
    service, store = _service(tmp_path, adapter)
    adapter.outcomes.append(
        lambda request, strategy_id: _success(request.query_id, strategy_id)
    )

    run = service.run(TEST_SQL, mode=DecisionMode.RULE_BASED, source="pytest")

    assert run.decision.selected_strategy_id == "S0_NATIVE"
    assert run.execution.final_status is ExecutionStatus.SUCCESS
    assert run.execution.end_to_end_ms is not None
    assert run.execution.end_to_end_ms >= run.execution.total_wall_ms
    assert store.count("queries") == 1
    assert store.count("decisions") == 1
    assert store.count("executions") == 1
    row = store.get_execution(run.execution.final_execution_id)
    assert row is not None
    assert row["decision_id"] == run.decision.decision_id
    assert row["attempt_index"] == 1
    assert row["is_fallback"] == 0
    assert row["requested_strategy_id"] == "S0_NATIVE"
    assert store.get_executions_for_decision(run.decision.decision_id) == [row]
    stored_report = store.get_execution_report(run.decision.decision_id)
    assert stored_report is not None
    assert stored_report["fallback_used"] == 0
    assert stored_report["fallback_trigger"] == "NONE"
    assert stored_report["final_status"] == "SUCCESS"
    assert stored_report["final_execution_id"] == run.execution.final_execution_id
    assert stored_report["total_wall_ms"] == run.execution.total_wall_ms
    assert stored_report["end_to_end_ms"] == run.execution.end_to_end_ms
    assert stored_report["warnings"] == run.execution.warnings


def test_service_log_correlates_query_decision_and_final_execution(
    tmp_path, caplog
) -> None:
    caplog.set_level(logging.INFO)
    adapter = ScriptedAdapter([])
    service, _ = _service(tmp_path, adapter)
    adapter.outcomes.append(
        lambda request, strategy_id: _success(request.query_id, strategy_id)
    )

    run = service.run(TEST_SQL, mode=DecisionMode.RULE_BASED)

    logs = [
        record
        for record in caplog.records
        if getattr(record, "event", None) == "decision_execution"
    ]
    assert len(logs) == 1
    assert logs[0].query_id == run.decision.query_id
    assert logs[0].decision_id == run.decision.decision_id
    assert logs[0].execution_id == run.execution.final_execution_id
    assert logs[0].strategy_id == run.decision.selected_strategy_id


def test_service_persists_candidate_decision_before_execution(tmp_path) -> None:
    registry = _fast_registry()
    adapter = ScriptedAdapter([])
    service, store = _service(
        tmp_path,
        adapter,
        registry=registry,
        engine=_fast_engine(registry),
    )
    adapter.outcomes.append(
        lambda request, strategy_id: _success(request.query_id, strategy_id)
    )

    run = service.run(TEST_SQL, mode=DecisionMode.RULE_BASED)

    assert run.decision.selected_strategy_id == TEST_ONLY_STRATEGY_ID
    stored = store.get_decision(run.decision.decision_id)
    assert stored is not None
    assert stored["selected_strategy_id"] == TEST_ONLY_STRATEGY_ID
    assert stored["candidate_sql"] == TEST_SQL
    rows = store.get_executions_for_decision(run.decision.decision_id)
    assert len(rows) == 1
    assert rows[0]["strategy_id"] == TEST_ONLY_STRATEGY_ID
    assert rows[0]["is_fallback"] == 0


def test_service_persists_both_attempts_when_fallback_is_safe(tmp_path) -> None:
    registry = _fast_registry()
    adapter = ScriptedAdapter([])
    service, store = _service(
        tmp_path,
        adapter,
        registry=registry,
        engine=_fast_engine(registry),
    )

    def first(request, strategy_id):
        return execution_result(
            query_id=request.query_id,
            strategy_id=strategy_id,
            status=ExecutionStatus.ERROR,
            request_dispatched=False,
            request_completed=False,
        )

    adapter.outcomes.extend(
        [
            first,
            lambda request, strategy_id: _success(request.query_id, strategy_id),
        ]
    )

    run = service.run(TEST_SQL, mode=DecisionMode.RULE_BASED)

    assert run.execution.fallback_used is True
    assert run.execution.final_status is ExecutionStatus.SUCCESS
    rows = store.get_executions_for_decision(run.decision.decision_id)
    assert len(rows) == 2
    assert [row["attempt_index"] for row in rows] == [1, 2]
    assert [row["is_fallback"] for row in rows] == [0, 1]
    assert rows[0]["requested_strategy_id"] == TEST_ONLY_STRATEGY_ID
    assert rows[1]["requested_strategy_id"] == "S0_NATIVE"
    assert rows[0]["request_dispatched"] == 0
    assert rows[1]["status"] == "SUCCESS"
    assert store.count("decisions") == 1

    stored_report = store.get_execution_report(run.decision.decision_id)
    assert stored_report is not None
    assert stored_report["fallback_used"] == 1
    assert stored_report["fallback_trigger"] == "FIRST_REQUEST_NOT_DISPATCHED"
    assert stored_report["fallback_reason"] == run.execution.fallback_reason
    assert stored_report["fallback_blocked_reason"] is None
    assert stored_report["final_status"] == "SUCCESS"
    assert stored_report["final_execution_id"] == run.execution.final_execution_id
    assert stored_report["warnings"] == run.execution.warnings
    assert any(
        warning.startswith("NATIVE_FALLBACK_USED")
        for warning in stored_report["warnings"]
    )


def test_service_persists_one_attempt_when_timeout_blocks_fallback(tmp_path) -> None:
    registry = _fast_registry()
    adapter = ScriptedAdapter([])
    service, store = _service(
        tmp_path,
        adapter,
        registry=registry,
        engine=_fast_engine(registry),
    )

    def timeout(request, strategy_id):
        return execution_result(
            query_id=request.query_id,
            strategy_id=strategy_id,
            status=ExecutionStatus.ERROR,
            timed_out=True,
            request_dispatched=True,
            request_completed=False,
            retry_unsafe_reason=RetryUnsafeReason.TIMEOUT_OR_UNKNOWN_SERVER_STATE,
            timeout_ms=request.timeout_ms,
        )

    adapter.outcomes.append(timeout)

    run = service.run(TEST_SQL, mode=DecisionMode.RULE_BASED, timeout_ms=50)

    assert run.execution.fallback_used is False
    assert run.execution.final_status is ExecutionStatus.ERROR
    rows = store.get_executions_for_decision(run.decision.decision_id)
    assert len(rows) == 1
    assert rows[0]["timed_out"] == 1
    assert rows[0]["timeout_ms"] == 50
    assert rows[0]["retry_unsafe_reason"] == "TIMEOUT_OR_UNKNOWN_SERVER_STATE"
    assert store.count("queries") == 1
    assert store.count("decisions") == 1

    stored_report = store.get_execution_report(run.decision.decision_id)
    assert stored_report is not None
    assert stored_report["fallback_used"] == 0
    assert stored_report["fallback_trigger"] == "NONE"
    assert stored_report["fallback_blocked_reason"] == run.execution.fallback_blocked_reason
    assert "timed out" in (stored_report["fallback_blocked_reason"] or "")
    assert stored_report["final_status"] == "ERROR"
    assert stored_report["final_execution_id"] == run.execution.final_execution_id


def test_persistence_failure_does_not_hide_a_completed_execution(
    tmp_path, monkeypatch
) -> None:
    adapter = ScriptedAdapter([])
    service, store = _service(tmp_path, adapter)
    adapter.outcomes.append(
        lambda request, strategy_id: _success(request.query_id, strategy_id)
    )

    def boom(*args, **kwargs):
        del args, kwargs
        raise sqlite3.OperationalError("disk is full")

    monkeypatch.setattr(store, "record_execution", boom)

    run = service.run(TEST_SQL, mode=DecisionMode.RULE_BASED)

    assert run.execution.final_status is ExecutionStatus.SUCCESS
    assert run.execution.fallback_used is False
    assert any(
        warning.startswith("EXECUTION_PERSISTENCE_FAILED")
        and "disk is full" in warning
        for warning in run.execution.warnings
    )
    assert store.count("queries") == 1
    assert store.count("decisions") == 1
    assert store.count("executions") == 0


def test_report_persistence_failure_does_not_hide_a_completed_execution(
    tmp_path, monkeypatch
) -> None:
    adapter = ScriptedAdapter([])
    service, store = _service(tmp_path, adapter)
    adapter.outcomes.append(
        lambda request, strategy_id: _success(request.query_id, strategy_id)
    )

    def boom(*args, **kwargs):
        del args, kwargs
        raise sqlite3.OperationalError("report disk is full")

    monkeypatch.setattr(store, "record_execution_report", boom)

    run = service.run(TEST_SQL, mode=DecisionMode.RULE_BASED)

    assert run.execution.final_status is ExecutionStatus.SUCCESS
    assert any(
        warning.startswith("REPORT_PERSISTENCE_FAILED")
        and "report disk is full" in warning
        for warning in run.execution.warnings
    )
    # Attempt rows are still persisted even though the report row is not.
    assert store.count("executions") == 1
    assert store.count("execution_reports") == 0


def test_service_keeps_decision_when_executor_raises(tmp_path) -> None:
    registry = _fast_registry()
    adapter = ScriptedAdapter([])
    service, store = _service(
        tmp_path,
        adapter,
        registry=registry,
        engine=_fast_engine(registry),
    )

    adapter.outcomes.append(AdapterError("connection lost"))

    with pytest.raises(ExecutionError):
        # AdapterError before any ExecutionResult; no execution row may be
        # invented and the persisted decision must remain auditable.
        service.run(TEST_SQL, mode=DecisionMode.RULE_BASED)

    assert store.count("queries") == 1
    assert store.count("decisions") == 1
    assert store.count("executions") == 0


def test_service_rejects_invalid_arguments_and_unsafe_sql(tmp_path) -> None:
    adapter = ScriptedAdapter([])
    service, _ = _service(tmp_path, adapter)
    with pytest.raises(ValueError, match="preview_rows"):
        service.run(TEST_SQL, preview_rows=0)
    with pytest.raises(ValueError, match="timeout_ms"):
        service.run(TEST_SQL, timeout_ms=-1)
    with pytest.raises(UnsafeSQLError):
        service.run("DELETE FROM root.test.g_0.d_0")
    assert adapter.calls == []


def test_service_rejects_engine_registry_mismatch(tmp_path) -> None:
    registry = _fast_registry()
    engine = _fast_engine(build_default_registry())
    store = SQLiteStore(tmp_path / "mismatch.sqlite3")
    with pytest.raises(ValueError, match="share one registry"):
        DecisionExecutionService(
            adapter=ScriptedAdapter([]),
            store=store,
            registry=registry,
            engine=engine,
        )
