"""Real 2.0.6 evidence for S1_REPEAT_CACHE and Native fallback.

Run only when explicitly enabled:
    IOTDB_INTEGRATION=1 IOTDB_PASSWORD=... pytest -m integration

Every statement comes from the repository workload under ``query/``.  The tests
only execute read-only SELECTs; they never write to IoTDB.  The fallback test
injects a pre-dispatch cache-lookup fault into the real adapter so the real
Executor must serve the same SQL through the Native baseline.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

import pytest

from tsdb_optimizer.adapters.iotdb.result_cache import ResultCache
from tsdb_optimizer.adapters.iotdb.tree_adapter import IoTDBTreeAdapter
from tsdb_optimizer.config import Settings, load_settings
from tsdb_optimizer.decision import DecisionMode
from tsdb_optimizer.errors import ConfigError
from tsdb_optimizer.execution import DecisionExecutionService, FallbackTrigger
from tsdb_optimizer.schemas import QueryRequest
from tsdb_optimizer.schemas.execution import ExecutionStatus
from tsdb_optimizer.storage import SQLiteStore
from tsdb_optimizer.strategies import build_default_registry
from tsdb_optimizer.sql_guard import validate_readonly_sql

pytestmark = pytest.mark.integration

EPOCH = "integration-epoch-2026-11-01"
PROJECT_ROOT = Path(__file__).resolve().parents[2]


def _require_integration() -> Settings:
    if os.environ.get("IOTDB_INTEGRATION") != "1":
        pytest.skip("set IOTDB_INTEGRATION=1 to run real IoTDB tests")
    try:
        return load_settings()
    except ConfigError:
        pytest.skip("IOTDB_PASSWORD is required for real IoTDB tests")


def _first_workload_sql(relative: str) -> str:
    path = PROJECT_ROOT / "query" / relative
    for line in path.read_text(encoding="utf-8").splitlines():
        candidate = line.strip()
        if not candidate or candidate.startswith("--"):
            continue
        if candidate.upper().startswith("SELECT"):
            return validate_readonly_sql(candidate)
    raise AssertionError(f"no SELECT statement found in {path}")


def _server_identity(adapter: IoTDBTreeAdapter) -> tuple[str, str]:
    report = adapter.doctor()
    assert report.ok is True, (report.error_type, report.error_message, report.warnings)
    assert report.server is not None
    assert report.server.version == "2.0.6"
    assert report.server.model == "tree"
    return report.server.version, report.server.model


@pytest.mark.parametrize(
    "workload",
    ["queries_dbs/q8.sql", "queries_dbs/q7.sql", "queries_dbs/q11.sql"],
)
def test_real_native_and_cache_results_are_identical(
    workload: str, tmp_path: Path
) -> None:
    settings = _require_integration()
    sql = _first_workload_sql(workload)
    adapter = IoTDBTreeAdapter(settings)
    try:
        server_version, database_model = _server_identity(adapter)
        assert (server_version, database_model) == ("2.0.6", "tree")

        native = adapter.execute_readonly(
            QueryRequest(sql=sql, source="pytest-cert-native"),
            preview_rows=1000,
        )
        miss = adapter.execute_readonly(
            QueryRequest(sql=sql, source="pytest-cert-miss"),
            preview_rows=1000,
            strategy_id="S1_REPEAT_CACHE",
            execution_options={"cache_epoch": EPOCH},
        )
        hit = adapter.execute_readonly(
            QueryRequest(sql=sql, source="pytest-cert-hit"),
            preview_rows=1000,
            strategy_id="S1_REPEAT_CACHE",
            execution_options={"cache_epoch": EPOCH},
        )

        assert native.status is ExecutionStatus.SUCCESS, native.error_message
        assert miss.status is ExecutionStatus.SUCCESS, miss.error_message
        assert hit.status is ExecutionStatus.SUCCESS, hit.error_message

        # Correctness evidence: the cached result equals the real Native result
        # in columns, row count and every previewed row (the preview covers the
        # entire result for these workload statements).
        assert native.preview_truncated is False
        assert miss.preview_truncated is False
        assert hit.preview_truncated is False
        assert native.row_count > 0
        assert miss.row_count == native.row_count
        assert hit.row_count == native.row_count
        assert [(c.name, c.data_type) for c in miss.columns] == [
            (c.name, c.data_type) for c in native.columns
        ]
        assert [(c.name, c.data_type) for c in hit.columns] == [
            (c.name, c.data_type) for c in native.columns
        ]
        assert miss.rows_preview == native.rows_preview
        assert hit.rows_preview == native.rows_preview

        # Effect evidence: the miss dispatched one real request; the hit did
        # not dispatch at all and names the real execution that populated it.
        assert native.served_from_cache is False
        assert native.request_dispatched is True
        assert miss.served_from_cache is False
        assert miss.request_dispatched is True
        assert miss.request_completed is True
        assert any("CACHE_MISS_STORED" in w for w in miss.warnings)
        assert hit.served_from_cache is True
        assert hit.request_dispatched is False
        assert hit.request_completed is False
        assert hit.retry_safe is False
        assert hit.metrics.execute_call_ms == 0.0
        assert hit.cache_source_execution_id == miss.execution_id
        assert hit.cache_epoch == EPOCH
        assert any("CACHE_HIT" in w for w in hit.warnings)
    finally:
        adapter.close()


def test_real_service_selects_native_then_cache_and_persists_provenance(
    tmp_path: Path,
) -> None:
    settings = _require_integration()
    sql = _first_workload_sql("queries_dbs/q8.sql")
    executed: list[str] = []

    def factory(settings_arg: Settings) -> _CountingSession:
        return _CountingSession(settings_arg, executed)

    adapter = IoTDBTreeAdapter(settings, session_factory=factory)
    store = SQLiteStore(tmp_path / "s1-certification.sqlite3")
    try:
        server_version, database_model = _server_identity(adapter)
        executed.clear()  # doctor's metadata probes are not this proof

        def explain_must_not_run(*args: Any, **kwargs: Any) -> None:
            del args, kwargs
            raise AssertionError("normal S1 execution must not call EXPLAIN")

        adapter.execute_explain = explain_must_not_run  # type: ignore[method-assign]

        registry = build_default_registry(cache_epoch=EPOCH)
        service = DecisionExecutionService(
            adapter=adapter,
            store=store,
            registry=registry,
            preview_rows=1000,
            server_version=server_version,
            database_model=database_model,
        )
        runs = [
            service.run(
                sql,
                mode=DecisionMode.RULE_BASED,
                source="pytest-cert-service",
                timeout_ms=30_000,
                preview_rows=1000,
            )
            for _ in range(3)
        ]

        first, second, third = runs
        assert [
            run.decision.query_occurrence for run in runs
        ] == [1, 2, 3]
        assert first.decision.selected_strategy_id == "S0_NATIVE"
        assert first.execution.attempts[0].execution.served_from_cache is False
        assert first.execution.attempts[0].execution.request_dispatched is True

        assert second.decision.selected_strategy_id == "S1_REPEAT_CACHE"
        second_attempt = second.execution.attempts[0].execution
        assert second_attempt.status is ExecutionStatus.SUCCESS
        assert second_attempt.served_from_cache is False
        assert second_attempt.request_dispatched is True
        assert second_attempt.request_completed is True
        assert any("CACHE_MISS_STORED" in w for w in second_attempt.warnings)

        assert third.decision.selected_strategy_id == "S1_REPEAT_CACHE"
        third_attempt = third.execution.attempts[0].execution
        assert third_attempt.status is ExecutionStatus.SUCCESS
        assert third_attempt.served_from_cache is True
        assert third_attempt.request_dispatched is False
        assert third_attempt.request_completed is False
        assert third_attempt.cache_source_execution_id == second_attempt.execution_id
        assert third_attempt.cache_epoch == EPOCH
        assert third_attempt.row_count == second_attempt.row_count
        assert third_attempt.rows_preview == second_attempt.rows_preview
        assert third_attempt.columns == second_attempt.columns
        assert third.execution.fallback_used is False
        assert all(run.execution.fallback_used is False for run in runs)

        # Observable effect, counted by the real Session wrapper: the first
        # Native run and the S1 miss each dispatched exactly one server
        # request; the S1 hit dispatched none.
        assert len(executed) == 2
        assert executed == [sql, sql]

        # Persistence evidence: the cache hit is an auditable execution row with
        # zero dispatch, and it is excluded from historical latency samples.
        assert store.count("queries") == 3
        assert store.count("decisions") == 3
        assert store.count("executions") == 3
        third_row = store.get_execution(third_attempt.execution_id)
        assert third_row is not None
        assert third_row["strategy_id"] == "S1_REPEAT_CACHE"
        assert third_row["served_from_cache"] == 1
        assert third_row["request_dispatched"] == 0
        assert third_row["request_completed"] == 0
        assert third_row["cache_source_execution_id"] == second_attempt.execution_id
        assert third_row["cache_epoch"] == EPOCH
        s1_history = store.recent_successful_latencies(
            sql_sha256=third_row["candidate_sql_sha256"],
            strategy_id="S1_REPEAT_CACHE",
            limit=10,
        )
        assert len(s1_history) == 1
        assert s1_history[0] == second_attempt.metrics.client_wall_ms
    finally:
        adapter.close()


class _FailingLookupCache(ResultCache):
    """Fault injection: the cache cannot answer, before any dispatch."""

    def get(self, cache_key: str) -> None:
        del cache_key
        raise RuntimeError("injected cache lookup failure")


class _CountingSession:
    """A real IoTDB Session that counts real server query dispatches."""

    def __init__(self, settings: Settings, executed: list[str]):
        from iotdb.Session import Session as RealSession

        self._inner = RealSession(
            settings.iotdb_host,
            settings.iotdb_port,
            settings.iotdb_user,
            settings.iotdb_password.get_secret_value(),
            connection_timeout_in_ms=settings.iotdb_connection_timeout_ms,
        )
        self.executed = executed

    def open(self, flag: bool = False) -> None:
        return self._inner.open(flag)

    def close(self) -> None:
        return self._inner.close()

    def reconnect(self) -> bool:
        return self._inner.reconnect()

    def execute_query_statement(self, sql: str, timeout: int = 0) -> Any:
        self.executed.append(sql)
        return self._inner.execute_query_statement(sql, timeout=timeout)


def test_real_native_fallback_after_pre_dispatch_cache_failure(
    tmp_path: Path,
) -> None:
    settings = _require_integration()
    sql = _first_workload_sql("queries_dbs/q8.sql")
    executed: list[str] = []

    def factory(settings_arg: Settings) -> _CountingSession:
        return _CountingSession(settings_arg, executed)

    adapter = IoTDBTreeAdapter(
        settings,
        session_factory=factory,
        result_cache=_FailingLookupCache(),
    )
    store = SQLiteStore(tmp_path / "s1-fallback.sqlite3")
    try:
        server_version, database_model = _server_identity(adapter)
        executed.clear()  # doctor's metadata probes are not this proof

        registry = build_default_registry(cache_epoch=EPOCH)
        service = DecisionExecutionService(
            adapter=adapter,
            store=store,
            registry=registry,
            preview_rows=1000,
            server_version=server_version,
            database_model=database_model,
        )

        first = service.run(
            sql,
            mode=DecisionMode.RULE_BASED,
            source="pytest-cert-fallback-first",
            timeout_ms=30_000,
            preview_rows=1000,
        )
        assert first.decision.selected_strategy_id == "S0_NATIVE"
        assert first.execution.final_status is ExecutionStatus.SUCCESS
        assert len(executed) == 1

        second = service.run(
            sql,
            mode=DecisionMode.RULE_BASED,
            source="pytest-cert-fallback-second",
            timeout_ms=30_000,
            preview_rows=1000,
        )

        assert second.decision.selected_strategy_id == "S1_REPEAT_CACHE"
        assert second.execution.fallback_used is True
        assert second.execution.fallback_trigger is (
            FallbackTrigger.FIRST_REQUEST_NOT_DISPATCHED
        )
        assert len(second.execution.attempts) == 2

        failed = second.execution.attempts[0]
        assert failed.is_fallback is False
        assert failed.requested_strategy_id == "S1_REPEAT_CACHE"
        assert failed.execution.status is ExecutionStatus.ERROR
        assert failed.execution.request_dispatched is False
        assert failed.execution.request_completed is False
        assert failed.execution.served_from_cache is False
        assert any(
            "CACHE_LOOKUP_FAILED" in warning
            for warning in failed.execution.warnings
        )

        fallback = second.execution.attempts[1]
        assert fallback.is_fallback is True
        assert fallback.requested_strategy_id == "S0_NATIVE"
        assert fallback.execution.status is ExecutionStatus.SUCCESS
        assert fallback.execution.served_from_cache is False
        assert fallback.execution.request_dispatched is True
        assert fallback.execution.request_completed is True
        assert second.execution.final_status is ExecutionStatus.SUCCESS

        # Exactly one real server request occurred in the fallback run: the
        # Native retry.  The failed cache attempt never reached IoTDB.
        assert len(executed) == 2
        assert executed[0] == executed[1]
        assert executed[1] == sql

        rows = store.get_executions_for_decision(
            second.decision.decision_id
        )
        assert [row["is_fallback"] for row in rows] == [0, 1]
        assert [row["strategy_id"] for row in rows] == [
            "S1_REPEAT_CACHE",
            "S0_NATIVE",
        ]
        assert rows[0]["request_dispatched"] == 0
        assert rows[1]["request_dispatched"] == 1
        assert rows[1]["status"] == "SUCCESS"
    finally:
        adapter.close()


def test_real_service_without_epoch_never_selects_the_cache(
    tmp_path: Path,
) -> None:
    settings = _require_integration()
    sql = _first_workload_sql("queries_dbs/q8.sql")
    adapter = IoTDBTreeAdapter(settings)
    store = SQLiteStore(tmp_path / "s1-no-epoch.sqlite3")
    try:
        server_version, database_model = _server_identity(adapter)
        service = DecisionExecutionService(
            adapter=adapter,
            store=store,
            registry=build_default_registry(),
            preview_rows=10,
            server_version=server_version,
            database_model=database_model,
        )
        for _ in range(2):
            run = service.run(
                sql,
                mode=DecisionMode.RULE_BASED,
                source="pytest-cert-no-epoch",
                timeout_ms=30_000,
                preview_rows=10,
            )
            assert run.decision.selected_strategy_id == "S0_NATIVE"
            assert run.execution.final_status is ExecutionStatus.SUCCESS
            assert run.execution.attempts[0].execution.served_from_cache is False
            assert run.execution.attempts[0].execution.request_dispatched is True
        assert adapter.result_cache.entries == 0
    finally:
        adapter.close()
