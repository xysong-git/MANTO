import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path
from uuid import uuid4

import pytest

from tsdb_optimizer.agents import QueryAnalysisAgent
from tsdb_optimizer.decision import DecisionEngine, DecisionMode
from tsdb_optimizer.errors import TSDBOptimizerError
from tsdb_optimizer.execution import (
    ExecutionAttempt,
    ExecutionReport,
    FallbackTrigger,
)
from tsdb_optimizer.schemas import (
    ExecutionResult,
    QueryRequest,
    RetryUnsafeReason,
    TimingMetrics,
)
from tsdb_optimizer.schemas.execution import ExecutionStatus, FetchMode
from tsdb_optimizer.storage import SCHEMA_VERSION, SQLiteStore
from tsdb_optimizer.strategies import build_default_registry


def _store(tmp_path: Path) -> SQLiteStore:
    store = SQLiteStore(tmp_path / "nested" / "test.sqlite3")
    store.initialize()
    return store


def _query() -> QueryRequest:
    return QueryRequest(sql="SELECT s_130 FROM root.test.g_0.d_0", source="pytest")


def _result(
    query: QueryRequest,
    *,
    status: ExecutionStatus = ExecutionStatus.SUCCESS,
    client_wall_ms: float = 4.0,
    started_at: datetime | None = None,
    sql_sha256: str = "a" * 64,
) -> ExecutionResult:
    started = started_at or datetime.now(timezone.utc)
    return ExecutionResult(
        query_id=query.query_id,
        execution_id=uuid4().hex,
        status=status,
        sql_sha256=sql_sha256,
        row_count=3,
        metrics=TimingMetrics(
            execute_call_ms=1.5,
            result_fetch_ms=2.5,
            client_wall_ms=client_wall_ms,
        ),
        started_at=started,
        finished_at=started + timedelta(milliseconds=client_wall_ms),
        request_dispatched=True,
        request_completed=status is ExecutionStatus.SUCCESS,
        retry_safe=False,
        retry_unsafe_reason=(
            RetryUnsafeReason.REQUEST_COMPLETION_UNCONFIRMED
            if status is ExecutionStatus.ERROR
            else None
        ),
    )


def _cache_hit(
    query: QueryRequest,
    *,
    source_execution_id: str,
    client_wall_ms: float = 0.1,
    cache_epoch: str = "epoch-2026-11-01",
) -> ExecutionResult:
    started = datetime.now(timezone.utc)
    return ExecutionResult(
        query_id=query.query_id,
        execution_id=uuid4().hex,
        status=ExecutionStatus.SUCCESS,
        sql_sha256="a" * 64,
        strategy_id="S1_REPEAT_CACHE",
        row_count=3,
        rows_preview=[[1], [2], [3]],
        preview_truncated=False,
        served_from_cache=True,
        cache_source_execution_id=source_execution_id,
        cache_epoch=cache_epoch,
        metrics=TimingMetrics(
            execute_call_ms=0.0,
            result_fetch_ms=0.2,
            client_wall_ms=client_wall_ms,
        ),
        started_at=started,
        finished_at=started + timedelta(milliseconds=client_wall_ms),
        request_dispatched=False,
        request_completed=False,
        retry_safe=False,
    )


def _decision(query: QueryRequest):
    analysis = QueryAnalysisAgent().analyze(
        query.sql,
        query_id=query.query_id,
    )
    return DecisionEngine(build_default_registry()).decide(
        query_id=query.query_id,
        original_sql=query.sql,
        mode=DecisionMode.BASELINE,
        analysis=analysis,
    )


def test_initialize_is_idempotent_and_records(tmp_path: Path) -> None:
    store = _store(tmp_path)
    store.initialize()
    query = _query()
    store.record_query(query, "a" * 64)
    result = _result(query)
    store.record_execution(result)

    assert store.count("queries") == 1
    assert store.count("decisions") == 0
    assert store.count("executions") == 1
    row = store.get_execution(result.execution_id)
    assert row is not None
    assert row["status"] == "SUCCESS"
    assert row["execute_call_ms"] == 1.5
    assert row["result_fetch_ms"] == 2.5
    assert row["client_wall_ms"] == 4.0
    assert row["row_count"] == 3
    assert row["column_count"] == 0
    assert row["fetch_mode"] == "FULL"
    assert row["attempt_index"] == 1
    assert row["is_fallback"] == 0
    assert row["request_dispatched"] == 1
    assert row["request_completed"] == 1
    assert row["retry_safe"] == 0
    assert row["candidate_sql_sha256"] == "a" * 64
    assert row["record_schema_version"] == 1
    with store._connect() as connection:  # type: ignore[attr-defined]
        query_row = connection.execute(
            "SELECT record_schema_version FROM queries WHERE query_id = ?",
            (query.query_id,),
        ).fetchone()
    assert query_row["record_schema_version"] == 1


def test_foreign_key_requires_query(tmp_path: Path) -> None:
    store = _store(tmp_path)
    query = _query()
    result = _result(query)
    with pytest.raises(Exception):
        store.record_execution(result)


def test_decision_requires_query_and_is_parsed_on_read(tmp_path: Path) -> None:
    store = _store(tmp_path)
    query = _query()
    decision = _decision(query)
    with pytest.raises(sqlite3.IntegrityError):
        store.record_decision(decision)

    store.record_query(query, "a" * 64)
    store.record_decision(decision)
    stored = store.get_decision(decision.decision_id)
    assert stored is not None
    assert stored["selected_strategy_id"] == "S0_NATIVE"
    assert stored["decision_mode"] == "baseline"
    assert stored["execution_options"] == {}
    assert stored["decision"]["decision_id"] == decision.decision_id
    assert stored["decision"]["candidate_sql"] == decision.candidate_sql
    assert stored["decision"]["schema_version"] == 1
    assert stored["record_schema_version"] == 1


def test_decision_execution_and_fallback_linkage(tmp_path: Path) -> None:
    store = _store(tmp_path)
    query = _query()
    store.record_query(query, "a" * 64)
    decision = _decision(query)
    store.record_decision(decision)

    failed = _result(query, status=ExecutionStatus.ERROR)
    fallback = _result(query)
    store.record_execution(
        failed,
        decision_id=decision.decision_id,
        attempt_index=1,
        is_fallback=False,
        requested_strategy_id="TEST_ONLY_FAILED",
    )
    store.record_execution(
        fallback,
        decision_id=decision.decision_id,
        attempt_index=2,
        is_fallback=True,
        requested_strategy_id="S0_NATIVE",
    )

    rows = store.get_executions_for_decision(decision.decision_id)
    assert [row["attempt_index"] for row in rows] == [1, 2]
    assert rows[0]["requested_strategy_id"] == "TEST_ONLY_FAILED"
    assert rows[0]["is_fallback"] == 0
    assert rows[0]["retry_unsafe_reason"] == "REQUEST_COMPLETION_UNCONFIRMED"
    assert rows[1]["is_fallback"] == 1
    assert rows[1]["strategy_id"] == "S0_NATIVE"


def test_historical_latencies_exclude_failures_and_fallbacks(tmp_path: Path) -> None:
    store = _store(tmp_path)
    query = _query()
    store.record_query(query, "a" * 64)
    base = datetime(2026, 11, 1, tzinfo=timezone.utc)
    for index, latency in enumerate([10.0, 20.0, 30.0]):
        store.record_execution(
            _result(
                query,
                client_wall_ms=latency,
                started_at=base + timedelta(seconds=index),
            )
        )
    store.record_execution(
        _result(query, client_wall_ms=999.0, started_at=base + timedelta(seconds=4)),
        is_fallback=True,
        attempt_index=2,
    )
    store.record_execution(
        _result(
            query,
            status=ExecutionStatus.ERROR,
            client_wall_ms=888.0,
            started_at=base + timedelta(seconds=5),
        )
    )

    latencies = store.recent_successful_latencies(
        sql_sha256="a" * 64,
        strategy_id="S0_NATIVE",
        limit=10,
    )
    assert latencies == [30.0, 20.0, 10.0]

    with pytest.raises(ValueError):
        store.recent_successful_latencies(
            sql_sha256="a" * 64,
            strategy_id="S0_NATIVE",
            limit=0,
        )


def test_historical_latencies_match_executed_sql_not_original_query(
    tmp_path: Path,
) -> None:
    store = _store(tmp_path)
    query = _query()
    store.record_query(query, "b" * 64)
    base = datetime(2026, 11, 1, tzinfo=timezone.utc)
    candidate_hash = "c" * 64
    for index, latency in enumerate([10.0, 20.0, 30.0]):
        store.record_execution(
            _result(
                query,
                client_wall_ms=latency,
                started_at=base + timedelta(seconds=index),
                sql_sha256=candidate_hash,
            )
        )

    # The executed candidate's history is available under its own hash.
    assert store.recent_successful_latencies(
        sql_sha256=candidate_hash,
        strategy_id="S0_NATIVE",
        limit=10,
    ) == [30.0, 20.0, 10.0]

    # The original query hash must not inherit the rewritten candidate's
    # executions, which is the F3 defect this schema version fixes.
    assert store.recent_successful_latencies(
        sql_sha256="b" * 64,
        strategy_id="S0_NATIVE",
        limit=10,
    ) == []


def test_unknown_table_rejected(tmp_path: Path) -> None:
    store = _store(tmp_path)
    with pytest.raises(ValueError):
        store.count("secrets")


def test_schema_version_mismatch_rejected(tmp_path: Path) -> None:
    path = tmp_path / "bad.sqlite3"
    store = SQLiteStore(path)
    store.initialize()
    with store._connect() as connection:  # type: ignore[attr-defined]
        connection.execute("PRAGMA user_version = 99")
    with pytest.raises(TSDBOptimizerError, match="schema version"):
        store.initialize()


def test_v1_database_is_migrated_without_data_loss(tmp_path: Path) -> None:
    path = tmp_path / "v1.sqlite3"
    connection = sqlite3.connect(path)
    connection.executescript(
        """
        CREATE TABLE queries (
            query_id TEXT PRIMARY KEY,
            sql_text TEXT NOT NULL,
            sql_sha256 TEXT NOT NULL,
            source TEXT NOT NULL,
            requested_at TEXT NOT NULL
        );
        CREATE TABLE executions (
            execution_id TEXT PRIMARY KEY,
            query_id TEXT NOT NULL REFERENCES queries(query_id),
            strategy_id TEXT NOT NULL,
            adapter TEXT NOT NULL,
            status TEXT NOT NULL,
            started_at TEXT NOT NULL,
            finished_at TEXT NOT NULL,
            execute_call_ms REAL NOT NULL,
            result_fetch_ms REAL NOT NULL,
            client_wall_ms REAL NOT NULL,
            row_count INTEGER NOT NULL,
            column_count INTEGER NOT NULL,
            error_type TEXT,
            error_message TEXT
        );
        PRAGMA user_version = 1;
        """
    )
    connection.execute(
        "INSERT INTO queries VALUES (?, ?, ?, ?, ?)",
        ("legacy-q", "SELECT s_130 FROM root.test.g_0.d_0", "a" * 64, "v1", "t"),
    )
    connection.execute(
        """
        INSERT INTO executions VALUES
        ('legacy-e', 'legacy-q', 'S0_NATIVE', 'iotdb-tree', 'SUCCESS',
         't', 't', 1, 2, 3, 1, 0, NULL, NULL)
        """
    )
    connection.commit()
    connection.close()

    store = SQLiteStore(path)
    store.initialize()
    with store._connect() as migrated:  # type: ignore[attr-defined]
        version = migrated.execute("PRAGMA user_version").fetchone()[0]
        columns = {
            row[1]
            for row in migrated.execute("PRAGMA table_info(executions)").fetchall()
        }
        tables = {
            row[0]
            for row in migrated.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table'"
            ).fetchall()
        }
    assert version == 8
    assert "execution_reports" in tables
    assert store.count("queries") == 1
    assert store.count("executions") == 1
    assert store.count("decisions") == 0
    assert {
        "decision_id",
        "attempt_index",
        "is_fallback",
        "request_dispatched",
        "request_completed",
        "retry_safe",
        "retry_unsafe_reason",
        "candidate_sql_sha256",
        "fetch_mode",
        "served_from_cache",
        "cache_source_execution_id",
        "cache_epoch",
        "record_schema_version",
    } <= columns
    legacy = store.get_execution("legacy-e")
    assert legacy is not None
    assert legacy["status"] == "SUCCESS"
    assert legacy["attempt_index"] == 1
    assert legacy["candidate_sql_sha256"] is None
    assert legacy["fetch_mode"] == "FULL"
    assert legacy["served_from_cache"] == 0
    assert legacy["cache_source_execution_id"] is None
    assert legacy["cache_epoch"] is None
    assert legacy["record_schema_version"] == 1


_LEGACY_V1_TABLES = """
CREATE TABLE queries (
    query_id TEXT PRIMARY KEY,
    sql_text TEXT NOT NULL,
    sql_sha256 TEXT NOT NULL,
    source TEXT NOT NULL,
    requested_at TEXT NOT NULL
);
CREATE TABLE executions (
    execution_id TEXT PRIMARY KEY,
    query_id TEXT NOT NULL REFERENCES queries(query_id),
    strategy_id TEXT NOT NULL,
    adapter TEXT NOT NULL,
    status TEXT NOT NULL,
    started_at TEXT NOT NULL,
    finished_at TEXT NOT NULL,
    execute_call_ms REAL NOT NULL,
    result_fetch_ms REAL NOT NULL,
    client_wall_ms REAL NOT NULL,
    row_count INTEGER NOT NULL,
    column_count INTEGER NOT NULL,
    error_type TEXT,
    error_message TEXT
);
"""

_LEGACY_V2_DECISIONS = """
CREATE TABLE decisions (
    decision_id TEXT PRIMARY KEY,
    query_id TEXT NOT NULL REFERENCES queries(query_id),
    selected_strategy_id TEXT NOT NULL,
    strategy_version TEXT NOT NULL,
    original_sql TEXT NOT NULL,
    candidate_sql TEXT NOT NULL,
    execution_options_json TEXT NOT NULL,
    expected_latency_ms REAL,
    confidence REAL,
    decision_mode TEXT NOT NULL,
    model_version TEXT,
    fallback_strategy_id TEXT NOT NULL,
    reason TEXT NOT NULL,
    decision_overhead_ms REAL NOT NULL DEFAULT 0,
    decision_json TEXT NOT NULL,
    created_at TEXT NOT NULL
);
"""

_LEGACY_V2_EXECUTION_COLUMNS = {
    "decision_id": "TEXT REFERENCES decisions(decision_id)",
    "attempt_index": "INTEGER NOT NULL DEFAULT 1",
    "is_fallback": "INTEGER NOT NULL DEFAULT 0",
    "requested_strategy_id": "TEXT",
    "timed_out": "INTEGER NOT NULL DEFAULT 0",
    "timeout_ms": "INTEGER",
    "request_dispatched": "INTEGER NOT NULL DEFAULT 0",
    "request_completed": "INTEGER NOT NULL DEFAULT 0",
    "retry_safe": "INTEGER NOT NULL DEFAULT 0",
    "retry_unsafe_reason": "TEXT",
    "plan_status": "TEXT",
    "actual_plan_hash": "TEXT",
}


def _create_v2_to_v5_database(path: Path, version: int) -> None:
    """Create a legacy database with the real v2/v3/v4/v5 table shapes."""

    connection = sqlite3.connect(path)
    try:
        connection.executescript(_LEGACY_V1_TABLES)
        if version >= 2:
            connection.executescript(_LEGACY_V2_DECISIONS)
            for name, definition in _LEGACY_V2_EXECUTION_COLUMNS.items():
                connection.execute(
                    f"ALTER TABLE executions ADD COLUMN {name} {definition}"
                )
        if version >= 3:
            connection.execute(
                "ALTER TABLE queries ADD COLUMN "
                "record_schema_version INTEGER NOT NULL DEFAULT 1"
            )
            connection.execute(
                "ALTER TABLE decisions ADD COLUMN "
                "record_schema_version INTEGER NOT NULL DEFAULT 1"
            )
            connection.execute(
                "ALTER TABLE executions ADD COLUMN candidate_sql_sha256 TEXT"
            )
            connection.execute(
                "ALTER TABLE executions ADD COLUMN "
                "record_schema_version INTEGER NOT NULL DEFAULT 1"
            )
        if version >= 4:
            connection.execute(
                "ALTER TABLE executions ADD COLUMN "
                "fetch_mode TEXT NOT NULL DEFAULT 'FULL'"
            )
        if version >= 5:
            connection.execute(
                "ALTER TABLE executions ADD COLUMN "
                "served_from_cache INTEGER NOT NULL DEFAULT 0"
            )
            connection.execute(
                "ALTER TABLE executions ADD COLUMN cache_source_execution_id TEXT"
            )
            connection.execute(
                "ALTER TABLE executions ADD COLUMN cache_epoch TEXT"
            )
        connection.execute(f"PRAGMA user_version = {version}")
        connection.execute(
            """
            INSERT INTO queries
                (query_id, sql_text, sql_sha256, source, requested_at)
            VALUES (?, ?, ?, ?, ?)
            """,
            ("legacy-q", "SELECT s_130 FROM root.test.g_0.d_0", "a" * 64, f"v{version}", "t"),
        )
        if version >= 2:
            connection.execute(
                """
                INSERT INTO decisions
                    (decision_id, query_id, selected_strategy_id,
                     strategy_version, original_sql, candidate_sql,
                     execution_options_json, expected_latency_ms, confidence,
                     decision_mode, model_version, fallback_strategy_id, reason,
                     decision_overhead_ms, decision_json, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    "legacy-d",
                    "legacy-q",
                    "S0_NATIVE",
                    "0.1.0",
                    "SELECT s_130 FROM root.test.g_0.d_0",
                    "SELECT s_130 FROM root.test.g_0.d_0",
                    "{}",
                    None,
                    None,
                    "baseline",
                    None,
                    "S0_NATIVE",
                    "legacy decision",
                    0.0,
                    '{"decision_id":"legacy-d"}',
                    "t",
                ),
            )
            columns = [
                "execution_id",
                "query_id",
                "decision_id",
                "strategy_id",
                "adapter",
                "status",
                "started_at",
                "finished_at",
                "execute_call_ms",
                "result_fetch_ms",
                "client_wall_ms",
                "row_count",
                "column_count",
                "attempt_index",
                "is_fallback",
                "requested_strategy_id",
                "request_dispatched",
                "request_completed",
                "retry_safe",
            ]
            values: list[object] = [
                "legacy-e",
                "legacy-q",
                "legacy-d",
                "S0_NATIVE",
                "iotdb-tree",
                "SUCCESS",
                "t",
                "t",
                1.0,
                2.0,
                3.0,
                1,
                0,
                2,
                1,
                "S0_NATIVE",
                1,
                1,
                0,
            ]
            if version >= 3:
                columns.append("candidate_sql_sha256")
                values.append("c" * 64)
                columns.append("record_schema_version")
                values.append(1)
            if version >= 4:
                columns.append("fetch_mode")
                values.append("LIMITED")
            if version >= 5:
                columns.extend(
                    [
                        "served_from_cache",
                        "cache_source_execution_id",
                        "cache_epoch",
                    ]
                )
                values.extend([1, "source-e", "epoch-1"])
            placeholders = ", ".join("?" for _ in columns)
            connection.execute(
                f"INSERT INTO executions ({', '.join(columns)}) "
                f"VALUES ({placeholders})",
                tuple(values),
            )
        else:  # pragma: no cover - v1 is covered by its dedicated test
            connection.execute(
                """
                INSERT INTO executions VALUES
                ('legacy-e', 'legacy-q', 'S0_NATIVE', 'iotdb-tree', 'SUCCESS',
                 't', 't', 1, 2, 3, 1, 0, NULL, NULL)
                """
            )
    finally:
        connection.commit()
        connection.close()


def _assert_v2_to_v5_migration(path: Path, version: int) -> None:
    store = SQLiteStore(path)
    store.initialize()
    with store._connect() as migrated:  # type: ignore[attr-defined]
        assert migrated.execute("PRAGMA user_version").fetchone()[0] == 8
        tables = {
            row[0]
            for row in migrated.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table'"
            ).fetchall()
        }
    assert {"queries", "decisions", "executions", "execution_reports"} <= tables
    assert store.count("queries") == 1
    assert store.count("decisions") == 1
    assert store.count("executions") == 1
    assert store.count("execution_reports") == 0

    legacy = store.get_execution("legacy-e")
    assert legacy is not None
    assert legacy["decision_id"] == "legacy-d"
    assert legacy["strategy_id"] == "S0_NATIVE"
    assert legacy["attempt_index"] == 2
    assert legacy["is_fallback"] == 1
    assert legacy["requested_strategy_id"] == "S0_NATIVE"
    assert legacy["request_dispatched"] == 1
    assert legacy["request_completed"] == 1
    assert legacy["retry_safe"] == 0
    assert legacy["fetch_mode"] == ("LIMITED" if version >= 4 else "FULL")
    if version >= 5:
        assert legacy["served_from_cache"] == 1
        assert legacy["cache_source_execution_id"] == "source-e"
        assert legacy["cache_epoch"] == "epoch-1"
    else:
        assert legacy["served_from_cache"] == 0
        assert legacy["cache_source_execution_id"] is None
        assert legacy["cache_epoch"] is None
    assert legacy["record_schema_version"] == 1
    if version >= 3:
        assert legacy["candidate_sql_sha256"] == "c" * 64
    else:
        assert legacy["candidate_sql_sha256"] is None

    stored_decision = store.get_decision("legacy-d")
    assert stored_decision is not None
    assert stored_decision["selected_strategy_id"] == "S0_NATIVE"
    assert stored_decision["record_schema_version"] == 1
    assert stored_decision["decision"]["decision_id"] == "legacy-d"

    with store._connect() as migrated:  # type: ignore[attr-defined]
        query_row = migrated.execute(
            "SELECT record_schema_version FROM queries WHERE query_id = 'legacy-q'"
        ).fetchone()
        report_columns = {
            row[1]
            for row in migrated.execute(
                "PRAGMA table_info(execution_reports)"
            ).fetchall()
        }
    assert query_row["record_schema_version"] == 1
    assert {
        "fallback_used",
        "fallback_trigger",
        "fallback_reason",
        "fallback_blocked_reason",
        "final_status",
        "final_execution_id",
        "total_wall_ms",
        "end_to_end_ms",
        "warnings_json",
        "report_json",
    } <= report_columns


@pytest.mark.parametrize("version", [2, 3, 4, 5])
def test_v2_to_v5_databases_migrate_without_data_loss(
    tmp_path: Path, version: int
) -> None:
    path = tmp_path / f"v{version}.sqlite3"
    _create_v2_to_v5_database(path, version)
    _assert_v2_to_v5_migration(path, version)


def test_execution_report_fields_are_persisted(tmp_path: Path) -> None:
    store = _store(tmp_path)
    query = _query()
    store.record_query(query, "a" * 64)
    decision = _decision(query)
    store.record_decision(decision)
    result = _result(query)
    store.record_execution(result, decision_id=decision.decision_id)
    report = ExecutionReport(
        decision_id=decision.decision_id,
        query_id=query.query_id,
        selected_strategy_id="S0_NATIVE",
        fallback_strategy_id="S0_NATIVE",
        attempts=[
            ExecutionAttempt(
                attempt_index=1,
                is_fallback=False,
                requested_strategy_id="S0_NATIVE",
                execution=result,
            )
        ],
        fallback_used=False,
        fallback_trigger=FallbackTrigger.NONE,
        final_status=ExecutionStatus.SUCCESS,
        final_execution_id=result.execution_id,
        total_wall_ms=4.0,
        end_to_end_ms=5.5,
        warnings=["A_WARNING", "B_WARNING"],
    )

    store.record_execution_report(report)
    stored = store.get_execution_report(decision.decision_id)

    assert stored is not None
    assert stored["decision_id"] == decision.decision_id
    assert stored["query_id"] == query.query_id
    assert stored["selected_strategy_id"] == "S0_NATIVE"
    assert stored["fallback_strategy_id"] == "S0_NATIVE"
    assert stored["fallback_used"] == 0
    assert stored["fallback_trigger"] == "NONE"
    assert stored["fallback_reason"] is None
    assert stored["fallback_blocked_reason"] is None
    assert stored["final_status"] == "SUCCESS"
    assert stored["final_execution_id"] == result.execution_id
    assert stored["total_wall_ms"] == 4.0
    assert stored["end_to_end_ms"] == 5.5
    assert stored["warnings"] == ["A_WARNING", "B_WARNING"]
    assert stored["report"]["end_to_end_ms"] == 5.5
    assert stored["record_schema_version"] == 1


def test_fetch_mode_is_persisted(tmp_path: Path) -> None:
    store = _store(tmp_path)
    query = _query()
    store.record_query(query, "a" * 64)
    result = _result(query).model_copy(update={"fetch_mode": FetchMode.LIMITED})
    store.record_execution(result)
    row = store.get_execution(result.execution_id)
    assert row is not None
    assert row["fetch_mode"] == "LIMITED"


def test_in_memory_store_keeps_its_schema_and_closes() -> None:
    store = SQLiteStore(":memory:")
    try:
        store.initialize()
        query = _query()
        store.record_query(query, "a" * 64)
        result = _result(query)
        store.record_execution(result)
        assert store.count("queries") == 1
        assert store.count("executions") == 1
        assert store.get_execution(result.execution_id) is not None
    finally:
        store.close()
    store.close()  # idempotent


def test_cache_hit_provenance_is_persisted(tmp_path: Path) -> None:
    store = _store(tmp_path)
    query = _query()
    store.record_query(query, "a" * 64)
    result = _cache_hit(query, source_execution_id="source-execution")
    store.record_execution(result)
    row = store.get_execution(result.execution_id)
    assert row is not None
    assert row["served_from_cache"] == 1
    assert row["cache_source_execution_id"] == "source-execution"
    assert row["cache_epoch"] == "epoch-2026-11-01"
    assert row["request_dispatched"] == 0
    assert row["request_completed"] == 0


def test_cache_hits_are_excluded_from_historical_latency(tmp_path: Path) -> None:
    store = _store(tmp_path)
    query = _query()
    store.record_query(query, "a" * 64)
    real = _result(query, client_wall_ms=40.0)
    store.record_execution(real)
    hit = _cache_hit(
        query,
        source_execution_id=real.execution_id,
        client_wall_ms=0.1,
    )
    store.record_execution(hit)

    native = store.recent_successful_latencies(
        sql_sha256="a" * 64,
        strategy_id="S0_NATIVE",
        limit=10,
    )
    cache = store.recent_successful_latencies(
        sql_sha256="a" * 64,
        strategy_id="S1_REPEAT_CACHE",
        limit=10,
    )
    assert native == [40.0]
    assert cache == []


def test_schema_version_constant() -> None:
    assert SCHEMA_VERSION == 8
