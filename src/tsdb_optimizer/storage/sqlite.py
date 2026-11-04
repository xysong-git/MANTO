"""SQLite persistence for query, decision, execution and learning records.

Schema v7 adds structured experiences, strategy-version quarantine, per-context
revisions and atomically published L1 model snapshots. Agent 4 uses a dedicated
repository; existing query/execution evidence remains unchanged.

Schema v6 persists the report-level execution outcome that is not carried by a
single attempt: whether a Native fallback was used, its trigger and reason, a
blocked-fallback reason, the final status/execution, total and end-to-end wall
time, and the report warnings.  Schema v5 records whether an execution was
served from the process-local repeat cache, which real execution populated the
cached result, and under which caller-declared dataset epoch.  Cache hits
dispatch no server request, so they are excluded from historical latency and
benchmark statistics.  Schema v4 adds the client result-consumption mode
(``FULL`` in the current adapter) so benchmark and historical-cost queries can
distinguish result fetching modes.  Schema v3 records the SHA-256 of the SQL
that was actually executed, so historical cost lookups match a rewritten
candidate rather than only the original query.  Schema v2 added Agent-2
decisions plus execution/fallback linkage and retry-safety evidence.  The store
never persists result rows or credentials.  Older databases are migrated in
place without deleting existing records.
"""

from __future__ import annotations

import json
import sqlite3
import threading
from contextlib import contextmanager
from pathlib import Path
from typing import TYPE_CHECKING, Any, Iterator

from ..errors import TSDBOptimizerError
from ..schemas import ExecutionResult, QueryRequest
from ..schemas.common import RECORD_SCHEMA_VERSION, utc_now

if TYPE_CHECKING:
    from ..decision.models import OptimizationDecision
    from ..execution.models import ExecutionReport

SCHEMA_VERSION = 8

_QUERIES_TABLE = """
CREATE TABLE IF NOT EXISTS queries (
    query_id      TEXT PRIMARY KEY,
    sql_text      TEXT NOT NULL,
    sql_sha256    TEXT NOT NULL,
    source        TEXT NOT NULL,
    requested_at  TEXT NOT NULL,
    record_schema_version INTEGER NOT NULL DEFAULT 1
);
"""

_DECISIONS_TABLE = """
CREATE TABLE IF NOT EXISTS decisions (
    decision_id             TEXT PRIMARY KEY,
    query_id                TEXT NOT NULL REFERENCES queries(query_id),
    selected_strategy_id    TEXT NOT NULL,
    strategy_version        TEXT NOT NULL,
    original_sql            TEXT NOT NULL,
    candidate_sql           TEXT NOT NULL,
    execution_options_json  TEXT NOT NULL,
    expected_latency_ms     REAL,
    confidence              REAL,
    decision_mode           TEXT NOT NULL,
    model_version           TEXT,
    fallback_strategy_id    TEXT NOT NULL,
    reason                  TEXT NOT NULL,
    decision_overhead_ms    REAL NOT NULL DEFAULT 0,
    decision_json           TEXT NOT NULL,
    created_at              TEXT NOT NULL,
    record_schema_version   INTEGER NOT NULL DEFAULT 1
);
"""

_EXECUTIONS_TABLE = """
CREATE TABLE IF NOT EXISTS executions (
    execution_id            TEXT PRIMARY KEY,
    query_id                TEXT NOT NULL REFERENCES queries(query_id),
    decision_id             TEXT REFERENCES decisions(decision_id),
    attempt_index           INTEGER NOT NULL DEFAULT 1,
    is_fallback             INTEGER NOT NULL DEFAULT 0,
    requested_strategy_id   TEXT,
    strategy_id             TEXT NOT NULL,
    candidate_sql_sha256    TEXT,
    adapter                 TEXT NOT NULL,
    status                  TEXT NOT NULL,
    started_at              TEXT NOT NULL,
    finished_at             TEXT NOT NULL,
    execute_call_ms         REAL NOT NULL,
    result_fetch_ms         REAL NOT NULL,
    client_wall_ms          REAL NOT NULL,
    row_count               INTEGER NOT NULL,
    column_count            INTEGER NOT NULL,
    fetch_mode              TEXT NOT NULL DEFAULT 'FULL',
    timed_out               INTEGER NOT NULL DEFAULT 0,
    timeout_ms              INTEGER,
    request_dispatched      INTEGER NOT NULL DEFAULT 0,
    request_completed       INTEGER NOT NULL DEFAULT 0,
    retry_safe              INTEGER NOT NULL DEFAULT 0,
    retry_unsafe_reason     TEXT,
    plan_status             TEXT,
    actual_plan_hash        TEXT,
    error_type              TEXT,
    error_message           TEXT,
    served_from_cache       INTEGER NOT NULL DEFAULT 0,
    cache_source_execution_id TEXT,
    cache_epoch             TEXT,
    record_schema_version   INTEGER NOT NULL DEFAULT 1
);
"""

_EXECUTION_REPORTS_TABLE = """
CREATE TABLE IF NOT EXISTS execution_reports (
    decision_id             TEXT PRIMARY KEY REFERENCES decisions(decision_id),
    query_id                TEXT NOT NULL REFERENCES queries(query_id),
    selected_strategy_id    TEXT NOT NULL,
    fallback_strategy_id    TEXT NOT NULL,
    fallback_used           INTEGER NOT NULL,
    fallback_trigger        TEXT NOT NULL,
    fallback_reason         TEXT,
    fallback_blocked_reason TEXT,
    final_status            TEXT NOT NULL,
    final_execution_id      TEXT NOT NULL,
    total_wall_ms           REAL NOT NULL,
    end_to_end_ms           REAL,
    warnings_json           TEXT NOT NULL,
    report_json             TEXT NOT NULL,
    created_at              TEXT NOT NULL,
    record_schema_version   INTEGER NOT NULL DEFAULT 1
);
"""

_INDEXES = """
CREATE INDEX IF NOT EXISTS idx_executions_query_id
    ON executions(query_id);
CREATE INDEX IF NOT EXISTS idx_execution_reports_query_id
    ON execution_reports(query_id);
CREATE INDEX IF NOT EXISTS idx_executions_started_at
    ON executions(started_at);
CREATE INDEX IF NOT EXISTS idx_executions_decision_id
    ON executions(decision_id);
CREATE INDEX IF NOT EXISTS idx_executions_candidate_sql_sha256
    ON executions(candidate_sql_sha256);
CREATE INDEX IF NOT EXISTS idx_decisions_query_id
    ON decisions(query_id);
CREATE INDEX IF NOT EXISTS idx_decisions_created_at
    ON decisions(created_at);
"""

_SCHEMA = (
    _QUERIES_TABLE
    + _DECISIONS_TABLE
    + _EXECUTIONS_TABLE
    + _EXECUTION_REPORTS_TABLE
    + _INDEXES
)

# Columns added by schema v2.  ALTER TABLE is used only for v1 migration; all
# names and definitions are fixed constants, never caller-controlled.
_V2_EXECUTION_COLUMNS: dict[str, str] = {
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


# Columns added by schema v3.  Old rows keep the default record version 1 for
# their protocol, and NULL for the candidate hash, because their executed SQL is
# unknown; it is never guessed from the original query hash.
_V3_QUERY_COLUMNS: dict[str, str] = {
    "record_schema_version": "INTEGER NOT NULL DEFAULT 1",
}
_V3_DECISION_COLUMNS: dict[str, str] = {
    "record_schema_version": "INTEGER NOT NULL DEFAULT 1",
}
_V3_EXECUTION_COLUMNS: dict[str, str] = {
    "candidate_sql_sha256": "TEXT",
    "record_schema_version": "INTEGER NOT NULL DEFAULT 1",
}
# Schema v4 records how the client consumed the result set.  Existing rows
# default to FULL because the only prior adapter path fully consumed every
# result set; this is a protocol default, not a claim about an old row's cache.
_V4_EXECUTION_COLUMNS: dict[str, str] = {
    "fetch_mode": "TEXT NOT NULL DEFAULT 'FULL'",
}
# Schema v5 records repeat-cache provenance.  Existing rows default to a cache
# miss because every execution written before v5 dispatched a real request.
_V5_EXECUTION_COLUMNS: dict[str, str] = {
    "served_from_cache": "INTEGER NOT NULL DEFAULT 0",
    "cache_source_execution_id": "TEXT",
    "cache_epoch": "TEXT",
}


def _table_columns(connection: sqlite3.Connection, table: str) -> set[str]:
    rows = connection.execute(f"PRAGMA table_info({table})").fetchall()
    return {str(row[1]) for row in rows}


def _ensure_columns(
    connection: sqlite3.Connection,
    table: str,
    definitions: dict[str, str],
) -> None:
    existing = _table_columns(connection, table)
    for name, definition in definitions.items():
        if name not in existing:
            connection.execute(
                f"ALTER TABLE {table} ADD COLUMN {name} {definition}"
            )


def _ensure_execution_v2_columns(connection: sqlite3.Connection) -> None:
    _ensure_columns(connection, "executions", _V2_EXECUTION_COLUMNS)


def _ensure_execution_v3_columns(connection: sqlite3.Connection) -> None:
    _ensure_columns(connection, "executions", _V3_EXECUTION_COLUMNS)


def _ensure_v3_record_columns(connection: sqlite3.Connection) -> None:
    _ensure_columns(connection, "queries", _V3_QUERY_COLUMNS)
    _ensure_columns(connection, "decisions", _V3_DECISION_COLUMNS)
    _ensure_execution_v3_columns(connection)


def _ensure_execution_v4_columns(connection: sqlite3.Connection) -> None:
    _ensure_columns(connection, "executions", _V4_EXECUTION_COLUMNS)


def _ensure_execution_v5_columns(connection: sqlite3.Connection) -> None:
    _ensure_columns(connection, "executions", _V5_EXECUTION_COLUMNS)


class SQLiteStore:
    def __init__(self, path: Path | str, *, wal_full: bool = False):
        self.path = Path(path)
        if wal_full and str(self.path) == ':memory:':
            raise ValueError('WAL requires a local file database')
        self.wal_full = wal_full
        self._request_connection = None
        self._request_owner = None
        self._initialized_schema = None
        # ``sqlite3.connect(':memory:')`` creates a *new* database per
        # connection.  A store that opens one connection per operation would
        # lose its schema immediately after initialize().  Keep one connection
        # alive for the lifetime of this store instead.
        self._memory_connection: sqlite3.Connection | None = None

    def _open_connection(self) -> sqlite3.Connection:
        if str(self.path) != ":memory:":
            self.path.parent.mkdir(parents=True, exist_ok=True)
        connection = sqlite3.connect(
            str(self.path),
            timeout=5.0,
            check_same_thread=(
                False if str(self.path) == ":memory:" else True
            ),
        )
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        if self.wal_full:
            try:
                mode = connection.execute("PRAGMA journal_mode=WAL").fetchone()[0]
                connection.execute("PRAGMA synchronous=FULL")
                if mode.lower() != 'wal' or connection.execute("PRAGMA synchronous").fetchone()[0] != 2:
                    raise RuntimeError('WAL/FULL configuration not effective')
            except BaseException:
                connection.close()
                raise
        return connection

    @contextmanager
    def request_session(self):
        """Reuse a connection, NOT a transaction. Each operation still commits.

        The connection closes (including any checkpoint) before run returns.
        Only explicit local WAL mode enables this optimization.
        """
        if not self.wal_full:
            yield
            return
        if self._request_connection is not None:
            raise RuntimeError('SQLiteStore request sessions are serial-owner only')
        connection = self._open_connection()
        self._request_connection = connection
        self._request_owner = threading.get_ident()
        try:
            yield
        finally:
            self._request_connection = None
            self._request_owner = None
            connection.close()

    @contextmanager
    def _connect(self) -> Iterator[sqlite3.Connection]:
        is_memory = str(self.path) == ":memory:"
        in_request = self._request_connection is not None and self._request_owner == threading.get_ident()
        if in_request:
            connection = self._request_connection
        elif is_memory:
            connection = self._memory_connection
            if connection is None:
                connection = self._open_connection()
                self._memory_connection = connection
        else:
            connection = self._open_connection()
        try:
            yield connection
            connection.commit()
        except Exception:
            connection.rollback()
            raise
        finally:
            if not is_memory and not in_request:
                connection.close()

    def close(self) -> None:
        """Close the retained in-memory connection, if any.

        File-backed stores open and close a connection per operation, so this
        method is a no-op for them.  Calling ``close`` on an in-memory store
        permanently discards its schema and rows.
        """

        connection, self._memory_connection = self._memory_connection, None
        self._initialized_schema = None
        if connection is not None:
            connection.close()

    def _schema_identity(self, connection):
        # Only reuse successful initialization for the same database and schema.
        # Never infer readiness merely from user_version or a boolean flag.
        if str(self.path) == ':memory:':
            identity = ('memory', id(connection))
        else:
            path = self.path.resolve()
            stat = path.stat()
            identity = (str(path), stat.st_dev, stat.st_ino)
        return (identity, connection.execute('PRAGMA schema_version').fetchone()[0])

    def initialize(self) -> None:
        with self._connect() as connection:
            version = connection.execute("PRAGMA user_version").fetchone()[0]
            identity = self._schema_identity(connection)
            if version == SCHEMA_VERSION and self._initialized_schema == identity:
                return
            # Failed migrations must never authorize a later fast path.
            self._initialized_schema = None
            if version == 0:
                connection.executescript(_SCHEMA)
            elif version in {1, 2, 3, 4, 5, 6, 7, 8}:
                # ``IF NOT EXISTS`` covers fresh/partially migrated databases;
                # the ALTER path adds only missing columns and never rewrites
                # existing records.
                connection.executescript(_QUERIES_TABLE)
                connection.executescript(_DECISIONS_TABLE)
                _ensure_execution_v2_columns(connection)
                _ensure_v3_record_columns(connection)
                _ensure_execution_v4_columns(connection)
                _ensure_execution_v5_columns(connection)
                connection.executescript(_EXECUTION_REPORTS_TABLE)
                connection.executescript(_INDEXES)
            else:
                raise TSDBOptimizerError(
                    f"unsupported SQLite schema version {version}; "
                    f"expected {SCHEMA_VERSION}"
                )
            from ..learning.schema import LEARNING_SCHEMA
            connection.executescript(LEARNING_SCHEMA)
            # Rejected inputs also have traces, so query_id intentionally has no FK.
            connection.execute("CREATE TABLE IF NOT EXISTS workflow_traces ("
                               "query_id TEXT PRIMARY KEY, trace_json TEXT NOT NULL)")
            connection.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")
            initialized_schema = self._schema_identity(connection)
        # Publish only after commit/close succeeded, not inside the transaction.
        self._initialized_schema = initialized_schema

    def record_workflow_trace(self, trace) -> None:
        from ..orchestration.models import WorkflowTrace
        validated = WorkflowTrace.model_validate(trace.model_dump())
        with self._connect() as connection:
            connection.execute("INSERT INTO workflow_traces(query_id, trace_json) VALUES (?, ?)",
                               (validated.query_id, validated.model_dump_json()))

    def get_workflow_trace(self, query_id: str):
        from ..orchestration.models import WorkflowTrace
        with self._connect() as connection:
            row = connection.execute("SELECT trace_json FROM workflow_traces WHERE query_id = ?",
                                     (query_id,)).fetchone()
        return WorkflowTrace.model_validate_json(row[0]) if row else None

    def record_query(self, request: QueryRequest, sql_sha256: str) -> None:
        with self._connect() as connection:
            connection.execute(
                """
                INSERT INTO queries
                    (query_id, sql_text, sql_sha256, source, requested_at,
                     record_schema_version)
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (
                    request.query_id,
                    request.sql,
                    sql_sha256,
                    request.source,
                    request.requested_at.isoformat(),
                    request.schema_version,
                ),
            )

    def record_decision(self, decision: "OptimizationDecision") -> None:
        """Persist every field needed to audit Agent 2's decision."""

        options_json = json.dumps(
            decision.execution_options,
            sort_keys=True,
            separators=(",", ":"),
        )
        with self._connect() as connection:
            connection.execute(
                """
                INSERT INTO decisions
                    (decision_id, query_id, selected_strategy_id,
                     strategy_version, original_sql, candidate_sql,
                     execution_options_json, expected_latency_ms, confidence,
                     decision_mode, model_version, fallback_strategy_id, reason,
                     decision_overhead_ms, decision_json, created_at,
                     record_schema_version)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    decision.decision_id,
                    decision.query_id,
                    decision.selected_strategy_id,
                    decision.strategy_version,
                    decision.original_sql,
                    decision.candidate_sql,
                    options_json,
                    decision.expected_latency_ms,
                    decision.confidence,
                    decision.decision_mode.value,
                    decision.model_version,
                    decision.fallback_strategy_id,
                    decision.reason,
                    decision.decision_overhead_ms,
                    decision.model_dump_json(),
                    decision.created_at.isoformat(),
                    decision.schema_version,
                ),
            )

    def get_decision(self, decision_id: str) -> dict[str, Any] | None:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT * FROM decisions WHERE decision_id = ?",
                (decision_id,),
            ).fetchone()
        if row is None:
            return None
        result = dict(row)
        result["execution_options"] = json.loads(result["execution_options_json"])
        result["decision"] = json.loads(result["decision_json"])
        return result

    def record_execution(
        self,
        result: ExecutionResult,
        *,
        decision_id: str | None = None,
        attempt_index: int = 1,
        is_fallback: bool = False,
        requested_strategy_id: str | None = None,
        plan_status: str | None = None,
        actual_plan_hash: str | None = None,
    ) -> None:
        if attempt_index < 1:
            raise ValueError("attempt_index must be positive")
        requested = requested_strategy_id or result.strategy_id
        unsafe_reason = (
            result.retry_unsafe_reason.value
            if result.retry_unsafe_reason is not None
            else None
        )
        with self._connect() as connection:
            connection.execute(
                """
                INSERT INTO executions
                    (execution_id, query_id, decision_id, attempt_index,
                     is_fallback, requested_strategy_id, strategy_id,
                     candidate_sql_sha256, adapter, status, started_at,
                     finished_at, execute_call_ms, result_fetch_ms,
                     client_wall_ms, row_count, column_count, fetch_mode,
                     timed_out, timeout_ms, request_dispatched,
                     request_completed, retry_safe, retry_unsafe_reason,
                     plan_status, actual_plan_hash, error_type, error_message,
                     served_from_cache, cache_source_execution_id, cache_epoch,
                     record_schema_version)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?,
                        ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    result.execution_id,
                    result.query_id,
                    decision_id,
                    attempt_index,
                    int(is_fallback),
                    requested,
                    result.strategy_id,
                    result.sql_sha256,
                    result.adapter,
                    result.status.value,
                    result.started_at.isoformat(),
                    result.finished_at.isoformat(),
                    result.metrics.execute_call_ms,
                    result.metrics.result_fetch_ms,
                    result.metrics.client_wall_ms,
                    result.row_count,
                    len(result.columns),
                    result.fetch_mode.value,
                    int(result.timed_out),
                    result.timeout_ms,
                    int(result.request_dispatched),
                    int(result.request_completed),
                    int(result.retry_safe),
                    unsafe_reason,
                    plan_status,
                    actual_plan_hash,
                    result.error_type,
                    result.error_message,
                    int(result.served_from_cache),
                    result.cache_source_execution_id,
                    result.cache_epoch,
                    result.schema_version,
                ),
            )

    def get_execution(self, execution_id: str) -> dict[str, Any] | None:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT * FROM executions WHERE execution_id = ?",
                (execution_id,),
            ).fetchone()
        return dict(row) if row is not None else None

    def get_executions_for_decision(self, decision_id: str) -> list[dict[str, Any]]:
        with self._connect() as connection:
            rows = connection.execute(
                """
                SELECT * FROM executions
                WHERE decision_id = ?
                ORDER BY attempt_index, started_at
                """,
                (decision_id,),
            ).fetchall()
        return [dict(row) for row in rows]

    def record_execution_report(self, report: "ExecutionReport") -> None:
        """Persist the report-level fallback/audit outcome for one decision.

        Per-attempt rows live in ``executions``.  This row records evidence that
        belongs to the whole report and would otherwise exist only in returned
        JSON: fallback trigger/reason/blocked reason, final status, total and
        end-to-end wall time, and warnings.  It never stores result rows.
        """

        warnings_json = json.dumps(
            report.warnings,
            ensure_ascii=False,
            separators=(",", ":"),
        )
        with self._connect() as connection:
            connection.execute(
                """
                INSERT INTO execution_reports
                    (decision_id, query_id, selected_strategy_id,
                     fallback_strategy_id, fallback_used, fallback_trigger,
                     fallback_reason, fallback_blocked_reason, final_status,
                     final_execution_id, total_wall_ms, end_to_end_ms,
                     warnings_json, report_json, created_at,
                     record_schema_version)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    report.decision_id,
                    report.query_id,
                    report.selected_strategy_id,
                    report.fallback_strategy_id,
                    int(report.fallback_used),
                    report.fallback_trigger.value,
                    report.fallback_reason,
                    report.fallback_blocked_reason,
                    report.final_status.value,
                    report.final_execution_id,
                    report.total_wall_ms,
                    report.end_to_end_ms,
                    warnings_json,
                    report.model_dump_json(),
                    utc_now().isoformat(),
                    RECORD_SCHEMA_VERSION,
                ),
            )

    def get_execution_report(self, decision_id: str) -> dict[str, Any] | None:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT * FROM execution_reports WHERE decision_id = ?",
                (decision_id,),
            ).fetchone()
        if row is None:
            return None
        result = dict(row)
        result["warnings"] = json.loads(result["warnings_json"])
        result["report"] = json.loads(result["report_json"])
        return result

    def recent_successful_latencies(
        self,
        *,
        sql_sha256: str,
        strategy_id: str,
        limit: int = 20,
    ) -> list[float]:
        """Return recent successful, non-fallback client wall times.

        The exact hash of the SQL that was actually executed is required, so a
        rewritten candidate cannot borrow the original query's history (and
        vice versa).  Fallback executions are excluded because they are
        conditioned on a first-attempt failure and would bias the baseline
        estimate.  Rows written before schema v3 have no candidate hash and are
        therefore never used.  Cache hits are excluded: they measure an
        in-process lookup, not a server execution, and would make a cached
        strategy look artificially cheap in its own historical evidence.
        """

        if limit < 1:
            raise ValueError("limit must be positive")
        with self._connect() as connection:
            rows = connection.execute(
                """
                SELECT e.client_wall_ms
                FROM executions AS e
                WHERE e.candidate_sql_sha256 = ?
                  AND e.strategy_id = ?
                  AND e.status = 'SUCCESS'
                  AND e.is_fallback = 0
                  AND e.served_from_cache = 0
                ORDER BY e.finished_at DESC
                LIMIT ?
                """,
                (sql_sha256, strategy_id, limit),
            ).fetchall()
        return [float(row[0]) for row in rows]

    def count(self, table: str) -> int:
        if table not in {
            "queries",
            "decisions",
            "executions",
            "execution_reports",
        }:
            raise ValueError(f"unsupported table: {table}")
        with self._connect() as connection:
            return int(
                connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
            )
