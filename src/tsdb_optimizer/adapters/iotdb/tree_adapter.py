"""Apache IoTDB 2.0.6 tree-model adapter (read-only S0/S1 execution)."""

from __future__ import annotations

import logging
import re
import time
from collections.abc import Mapping
from dataclasses import dataclass, field
from importlib.metadata import PackageNotFoundError, version as package_version
from typing import Any, Callable
from uuid import uuid4

from ...cache_epoch import normalize_cache_epoch
from ...config import Settings
from ...errors import AdapterError
from ...schemas import (
    CapabilityReport,
    ColumnMeta,
    DoctorReport,
    ExecutionResult,
    QueryRequest,
    ServerInfo,
    TimingMetrics,
)
from ...schemas.common import utc_now
from ...schemas.execution import ExecutionStatus, FetchMode, RetryUnsafeReason
from ...sql_guard import sql_sha256, validate_readonly_sql
from ..base import TSDBAdapter
from .result_cache import (
    CachedResult,
    ResultCache,
    estimate_row_bytes,
    result_cache_key,
)

logger = logging.getLogger(__name__)

SessionFactory = Callable[[Settings], Any]

# Doctor probes compose EXPLAIN SQL from a server-discovered series path.  Only
# this conservative unquoted path subset is accepted; anything else (quoted
# identifiers, whitespace, punctuation, comment markers, NUL, ...) skips the
# probe rather than interpolating server data into SQL.  This is intentionally
# narrower than IoTDB's full identifier grammar, and failing closed merely
# leaves the EXPLAIN capability unprobed.
_SAFE_TREE_PATH_SEGMENT = re.compile(r"[A-Za-z_][A-Za-z0-9_]*\Z")


def _is_safe_tree_path(path: str) -> bool:
    if not path or len(path) > 1024:
        return False
    return all(
        _SAFE_TREE_PATH_SEGMENT.fullmatch(segment) is not None
        for segment in path.split(".")
    )


def _default_session_factory(settings: Settings) -> Any:
    # Keep the client import lazy so unit tests and --help do not need IoTDB.
    from iotdb.Session import Session

    return Session(
        settings.iotdb_host,
        settings.iotdb_port,
        settings.iotdb_user,
        settings.iotdb_password.get_secret_value(),
        connection_timeout_in_ms=settings.iotdb_connection_timeout_ms,
    )


def _decode(value: Any) -> Any:
    if isinstance(value, (bytes, bytearray)):
        return bytes(value).decode("utf-8", errors="replace")
    return value


def _installed_client_version() -> str | None:
    try:
        return package_version("apache-iotdb")
    except PackageNotFoundError:
        return None


@dataclass
class _CacheCapture:
    """Bounded, fail-closed capture of a complete result for cache admission.

    Once a row/byte limit is exceeded the partial rows are cleared immediately
    and no further values are decoded.  A partial result is never cached.
    """

    max_rows: int
    max_bytes: int
    rows: list[tuple[Any, ...]] = field(default_factory=list)
    size_bytes: int = 0
    overflowed: bool = False
    reason: str | None = None

    def add(self, values: list[Any]) -> None:
        if self.overflowed:
            return
        if len(self.rows) + 1 > self.max_rows:
            self._overflow(
                f"row count exceeds the cache entry limit of {self.max_rows}"
            )
            return
        row = tuple(values)
        row_bytes = estimate_row_bytes(row)
        if self.size_bytes + row_bytes > self.max_bytes:
            self._overflow(
                f"estimated result size exceeds the cache entry limit of "
                f"{self.max_bytes} bytes"
            )
            return
        self.rows.append(row)
        self.size_bytes += row_bytes

    def _overflow(self, reason: str) -> None:
        self.overflowed = True
        self.reason = reason
        self.rows.clear()
        self.size_bytes = 0


def _disable_sdk_auto_reconnect(session: Any) -> bool | None:
    """Disable the pinned SDK's silent reconnect-and-resend path.

    ``apache-iotdb==2.0.6`` catches ``TTransport.TException`` inside
    ``Session.execute_query_statement`` and, if ``reconnect()`` succeeds,
    resends the *same* request.  That makes a client-level "one adapter call"
    claim incompatible with a server-level at-most-once claim.  The SDK exposes
    no constructor flag, so the adapter shadows the instance method with a
    function that always returns ``False``.  The SDK then raises
    ``IoTDBConnectionException`` and the adapter records completion as unknown.

    Returns ``True`` when the guard was installed, ``False`` when the session
    exposes a reconnect method that could not be shadowed, and ``None`` when the
    session is a test double without such a method.
    """

    try:
        if not hasattr(session, "reconnect"):
            return None
        session.reconnect = lambda: False
    except Exception:  # noqa: BLE001 - fail closed but let connect() report it
        logger.warning(
            "could not disable SDK auto-reconnect; at-most-once is unproven",
            extra={"event": "sdk_reconnect_guard_failed"},
        )
        return False
    return True


class IoTDBTreeAdapter(TSDBAdapter):
    """Thin, explicit wrapper around ``iotdb.Session``.

    It intentionally implements only the operations verified in Phase 0:
    connecting, running a single SELECT/metadata query, fully consuming the
    result set, and closing the operation handle.  It does not rewrite SQL,
    retry queries, or call EXPLAIN ANALYZE.
    """

    adapter_name = "iotdb-tree"

    def __init__(
        self,
        settings: Settings,
        session_factory: SessionFactory | None = None,
        *,
        result_cache: ResultCache | None = None,
    ):
        self.settings = settings
        self._session_factory = session_factory or _default_session_factory
        self._session: Any | None = None
        self._sdk_reconnect_disabled: bool | None = None
        self._client_version = _installed_client_version()
        # The cache is process-local and only activated by the validated
        # ``cache_epoch`` execution option.  It never replaces the Native path.
        self.result_cache = result_cache or ResultCache()

    @property
    def connected(self) -> bool:
        return self._session is not None

    def connect(self) -> None:
        if self._session is not None:
            return
        started = time.perf_counter()
        session = self._session_factory(self.settings)
        try:
            session.open(False)
        except Exception as exc:  # noqa: BLE001 - convert to a safe adapter error
            try:
                session.close()
            except Exception:  # noqa: BLE001 - best-effort cleanup
                logger.warning("session cleanup after failed open also failed")
            raise AdapterError(
                f"could not connect to IoTDB: {type(exc).__name__}: {exc}"
            ) from exc
        self._sdk_reconnect_disabled = _disable_sdk_auto_reconnect(session)
        if self._sdk_reconnect_disabled is not True:
            try:
                session.close()
            except Exception:  # noqa: BLE001 - best-effort cleanup
                logger.warning("session cleanup after reconnect guard failure also failed")
            raise AdapterError(
                "could not disable the IoTDB SDK auto-reconnect path; "
                "refusing to execute without an at-most-once guarantee"
            )
        self._session = session
        logger.info(
            "iotdb connected",
            extra={
                "event": "connect",
                "adapter": self.adapter_name,
                "elapsed_ms": round((time.perf_counter() - started) * 1000, 3),
            },
        )

    def close(self) -> None:
        session, self._session = self._session, None
        if session is None:
            return
        try:
            session.close()
            logger.info("iotdb closed", extra={"event": "close", "adapter": self.adapter_name})
        except Exception as exc:  # noqa: BLE001 - cleanup must not hide query result
            logger.warning(
                "iotdb close failed: %s: %s",
                type(exc).__name__,
                exc,
                extra={"event": "close_error", "adapter": self.adapter_name},
            )

    def execute_readonly(
        self,
        request: QueryRequest,
        preview_rows: int = 20,
        *,
        strategy_id: str = "S0_NATIVE",
        execution_options: Mapping[str, str | int | float | bool] | None = None,
    ) -> ExecutionResult:
        cleaned = validate_readonly_sql(request.sql)
        if preview_rows < 1:
            raise ValueError("preview_rows must be positive")
        if not strategy_id.strip():
            raise ValueError("strategy_id must not be blank")
        method_started = time.perf_counter()
        started_at = utc_now()
        options = dict(execution_options or {})
        unsupported = sorted(set(options) - {"cache_epoch"})
        if unsupported:
            raise ValueError(
                f"unsupported execution options: {unsupported}; only cache_epoch "
                "is implemented by this adapter"
            )

        cache_epoch: str | None = None
        if "cache_epoch" in options:
            raw_epoch = options["cache_epoch"]
            if not isinstance(raw_epoch, str):
                return self._pre_dispatch_cache_error(
                    request=request,
                    sql=cleaned,
                    strategy_id=strategy_id,
                    started_at=started_at,
                    method_started=method_started,
                    cache_epoch=None,
                    error_type="InvalidCacheEpoch",
                    error_message="cache_epoch must be a string",
                    warning="CACHE_OPTION_INVALID: no request was dispatched to IoTDB",
                )
            try:
                cache_epoch = normalize_cache_epoch(raw_epoch)
            except ValueError as exc:
                return self._pre_dispatch_cache_error(
                    request=request,
                    sql=cleaned,
                    strategy_id=strategy_id,
                    started_at=started_at,
                    method_started=method_started,
                    cache_epoch=None,
                    error_type="InvalidCacheEpoch",
                    error_message=str(exc),
                    warning="CACHE_OPTION_INVALID: no request was dispatched to IoTDB",
                )

        sql_hash = sql_sha256(cleaned)
        lookup_ms = 0.0
        if cache_epoch is not None:
            key = result_cache_key(
                cache_epoch=cache_epoch,
                sql_sha256=sql_hash,
                strategy_id=strategy_id,
            )
            lookup_started = time.perf_counter()
            try:
                entry = self.result_cache.get(key)
            except Exception as exc:  # noqa: BLE001 - fail before dispatch
                lookup_ms = (time.perf_counter() - lookup_started) * 1000
                return self._pre_dispatch_cache_error(
                    request=request,
                    sql=cleaned,
                    strategy_id=strategy_id,
                    started_at=started_at,
                    method_started=method_started,
                    cache_epoch=cache_epoch,
                    error_type=type(exc).__name__,
                    error_message=str(exc)[:4000],
                    warning=(
                        "CACHE_LOOKUP_FAILED: no request was dispatched to IoTDB"
                    ),
                    lookup_ms=lookup_ms,
                )
            lookup_ms = (time.perf_counter() - lookup_started) * 1000
            if entry is not None:
                if (
                    entry.cache_epoch != cache_epoch
                    or entry.sql_sha256 != sql_hash
                    or entry.strategy_id != strategy_id
                ):
                    return self._pre_dispatch_cache_error(
                        request=request,
                        sql=cleaned,
                        strategy_id=strategy_id,
                        started_at=started_at,
                        method_started=method_started,
                        cache_epoch=cache_epoch,
                        error_type="ResultCacheIdentityMismatch",
                        error_message=(
                            "cache entry identity does not match the requested "
                            "epoch/SQL/strategy; refusing to serve it"
                        ),
                        warning=(
                            "CACHE_LOOKUP_FAILED: no request was dispatched to IoTDB"
                        ),
                        lookup_ms=lookup_ms,
                    )
                return self._cache_hit_result(
                    entry=entry,
                    request=request,
                    strategy_id=strategy_id,
                    preview_rows=preview_rows,
                    started_at=started_at,
                    method_started=method_started,
                    lookup_ms=lookup_ms,
                )

        self.connect()
        return self._run_raw(
            sql=cleaned,
            query_id=request.query_id,
            execution_id=uuid4().hex,
            started_at=started_at,
            preview_rows=preview_rows,
            timeout_ms=request.timeout_ms,
            strategy_id=strategy_id,
            cache_epoch=cache_epoch,
            cache_capture=(
                _CacheCapture(
                    max_rows=self.result_cache.max_entry_rows,
                    max_bytes=self.result_cache.max_entry_bytes,
                )
                if cache_epoch is not None
                else None
            ),
        )

    def _cache_hit_result(
        self,
        *,
        entry: CachedResult,
        request: QueryRequest,
        strategy_id: str,
        preview_rows: int,
        started_at: Any,
        method_started: float,
        lookup_ms: float,
    ) -> ExecutionResult:
        preview = [list(row) for row in entry.rows[:preview_rows]]
        columns = [
            ColumnMeta(name=column.name, data_type=column.data_type)
            for column in entry.columns
        ]
        finished_at = utc_now()
        execution_id = uuid4().hex
        warning = (
            "CACHE_HIT: served from execution "
            f"{entry.source_execution_id} under epoch {entry.cache_epoch!r}; "
            f"cache lookup {lookup_ms:.3f} ms; no IoTDB request was dispatched"
        )
        logger.info(
            "repeat-cache hit",
            extra={
                "event": "cache_hit",
                "query_id": request.query_id,
                # This is the execution id of the hit itself, not the miss that
                # populated the entry.  Keep both IDs explicit for audit.
                "execution_id": execution_id,
                "cache_source_execution_id": entry.source_execution_id,
                "adapter": self.adapter_name,
                "strategy_id": strategy_id,
                "cache_epoch": entry.cache_epoch,
                "row_count": entry.row_count,
                "cache_lookup_ms": round(lookup_ms, 3),
                "elapsed_ms": round(
                    (time.perf_counter() - method_started) * 1000, 3
                ),
            },
        )
        return ExecutionResult(
            query_id=request.query_id,
            execution_id=execution_id,
            status=ExecutionStatus.SUCCESS,
            sql_sha256=entry.sql_sha256,
            strategy_id=strategy_id,
            adapter=self.adapter_name,
            columns=columns,
            rows_preview=preview,
            preview_truncated=entry.row_count > len(preview),
            row_count=entry.row_count,
            fetch_mode=FetchMode.FULL,
            served_from_cache=True,
            cache_source_execution_id=entry.source_execution_id,
            cache_epoch=entry.cache_epoch,
            metrics=TimingMetrics(
                execute_call_ms=0.0,
                # A cache hit does not traverse a server result set.  The
                # lookup cost is reported separately in the structured log and
                # the CACHE_HIT warning; it is not result-fetch time.
                result_fetch_ms=0.0,
                client_wall_ms=round(
                    (time.perf_counter() - method_started) * 1000, 3
                ),
            ),
            started_at=started_at,
            finished_at=finished_at,
            timeout_ms=request.timeout_ms,
            request_dispatched=False,
            request_completed=False,
            retry_safe=False,
            retry_unsafe_reason=None,
            warnings=[warning],
        )

    def _pre_dispatch_cache_error(
        self,
        *,
        request: QueryRequest,
        sql: str,
        strategy_id: str,
        started_at: Any,
        method_started: float,
        cache_epoch: str | None,
        error_type: str,
        error_message: str,
        warning: str,
        lookup_ms: float = 0.0,
    ) -> ExecutionResult:
        finished_at = utc_now()
        client_wall_ms = (time.perf_counter() - method_started) * 1000
        execution_id = uuid4().hex
        logger.warning(
            "repeat-cache pre-dispatch failure",
            extra={
                "event": "cache_error",
                "query_id": request.query_id,
                "execution_id": execution_id,
                "adapter": self.adapter_name,
                "strategy_id": strategy_id,
                "cache_epoch": cache_epoch,
                "cache_lookup_ms": round(lookup_ms, 3),
                "error_type": error_type,
                "elapsed_ms": round(client_wall_ms, 3),
            },
        )
        return ExecutionResult(
            query_id=request.query_id,
            execution_id=execution_id,
            status=ExecutionStatus.ERROR,
            sql_sha256=sql_sha256(sql),
            strategy_id=strategy_id,
            adapter=self.adapter_name,
            columns=[],
            rows_preview=[],
            preview_truncated=False,
            row_count=0,
            fetch_mode=FetchMode.FULL,
            served_from_cache=False,
            cache_source_execution_id=None,
            cache_epoch=cache_epoch,
            metrics=TimingMetrics(
                execute_call_ms=0.0,
                # A pre-dispatch cache failure has no server result set.
                result_fetch_ms=0.0,
                client_wall_ms=round(client_wall_ms, 3),
            ),
            started_at=started_at,
            finished_at=finished_at,
            error_type=error_type,
            error_message=error_message[:4000],
            timeout_ms=request.timeout_ms,
            timed_out=False,
            request_dispatched=False,
            request_completed=False,
            retry_safe=False,
            retry_unsafe_reason=None,
            warnings=[warning],
        )

    def execute_explain(
        self,
        sql: str,
        *,
        query_id: str | None = None,
        preview_rows: int = 1000,
    ) -> ExecutionResult:
        """Run ``EXPLAIN`` for one already read-only SELECT.

        The input is validated as a single SELECT first; the adapter itself
        prefixes exactly ``EXPLAIN``.  It never creates or runs
        ``EXPLAIN ANALYZE`` because that would execute the query.
        """

        cleaned = validate_readonly_sql(sql)
        if preview_rows < 1:
            raise ValueError("preview_rows must be positive")
        self.connect()
        return self._run_raw(
            sql=f"EXPLAIN {cleaned}",
            query_id=query_id or f"explain-{uuid4().hex}",
            execution_id=uuid4().hex,
            started_at=utc_now(),
            preview_rows=preview_rows,
            timeout_ms=None,
        )

    def _run_raw(
        self,
        sql: str,
        query_id: str,
        execution_id: str,
        started_at: Any,
        preview_rows: int,
        timeout_ms: int | None = None,
        strategy_id: str = "S0_NATIVE",
        cache_epoch: str | None = None,
        cache_capture: _CacheCapture | None = None,
    ) -> ExecutionResult:
        if self._session is None:
            raise AdapterError("adapter is not connected")

        wall_started = time.perf_counter()
        execute_started = wall_started
        execute_call_ms = 0.0
        fetch_started: float | None = None
        result_fetch_ms = 0.0
        row_count = 0
        preview: list[list[Any]] = []
        columns: list[ColumnMeta] = []
        warnings: list[str] = []
        error_type: str | None = None
        error_message: str | None = None
        timed_out = False
        status = ExecutionStatus.SUCCESS
        dataset: Any | None = None
        dispatch_started = False
        request_completed = False
        retry_safe = False
        retry_unsafe_reason: RetryUnsafeReason | None = None

        try:
            # The installed 2.0.6 SDK forwards ``timeout`` into
            # TSExecuteStatementReq and TSFetchResultsReq (milliseconds).
            # ``None`` is sent as the SDK default ``0``.  What the server means
            # by ``0`` (no timeout vs. server default) is UNKNOWN and is not
            # asserted here; ``timed_out`` is only a client-side observation.
            dispatch_started = True
            dataset = self._session.execute_query_statement(
                sql, timeout=timeout_ms or 0
            )
            execute_call_ms = (time.perf_counter() - execute_started) * 1000
            names = [str(name) for name in dataset.get_column_names()]
            try:
                types = [str(item) for item in dataset.get_column_types()]
            except Exception as exc:  # noqa: BLE001 - type metadata is optional
                types = [None] * len(names)
                warnings.append(
                    "column type metadata unavailable: "
                    f"{type(exc).__name__}: {exc}"
                )
                logger.warning(
                    "column type metadata unavailable",
                    extra={
                        "event": "column_metadata_error",
                        "query_id": query_id,
                        "execution_id": execution_id,
                        "error_type": type(exc).__name__,
                    },
                )
            if len(types) != len(names):
                warnings.append(
                    "column type metadata length mismatch: "
                    f"{len(types)} types for {len(names)} columns"
                )
                types = [None] * len(names)
            columns = [
                ColumnMeta(name=name, data_type=data_type)
                for name, data_type in zip(names, types)
            ]

            fetch_started = time.perf_counter()
            while dataset.has_next():
                row = dataset.next()
                row_count += 1
                need_preview = len(preview) < preview_rows
                need_capture = (
                    cache_capture is not None and not cache_capture.overflowed
                )
                if need_preview or need_capture:
                    values = self._values_for_row(columns, row)
                    if need_preview:
                        preview.append(values)
                    if need_capture:
                        cache_capture.add(values)
            result_fetch_ms = (time.perf_counter() - fetch_started) * 1000
            # All rows were consumed.  The operation handle is still closed in
            # ``finally``; a close warning does not erase completion evidence.
            request_completed = True
        except Exception as exc:  # noqa: BLE001 - return a structured error
            status = ExecutionStatus.ERROR
            error_type = type(exc).__name__
            error_message = str(exc)[:4000]
            timed_out = self._looks_like_timeout(exc)
            retry_unsafe_reason = (
                RetryUnsafeReason.TIMEOUT_OR_UNKNOWN_SERVER_STATE
                if timed_out
                else RetryUnsafeReason.REQUEST_COMPLETION_UNCONFIRMED
            )
            if execute_call_ms == 0.0:
                execute_call_ms = (time.perf_counter() - execute_started) * 1000
            if fetch_started is not None:
                result_fetch_ms = (time.perf_counter() - fetch_started) * 1000
        finally:
            if dataset is not None:
                try:
                    dataset.close_operation_handle()
                except Exception as exc:  # noqa: BLE001 - record cleanup failure
                    warnings.append(
                        f"result set close failed: {type(exc).__name__}: {exc}"
                    )
            finished_at = utc_now()

        if (
            status is ExecutionStatus.SUCCESS
            and cache_epoch is not None
            and cache_capture is not None
        ):
            if cache_capture.overflowed:
                warnings.append(
                    "CACHE_NOT_STORED: "
                    + (cache_capture.reason or "result exceeded cache limits")
                    + "; a partial result was not cached"
                )
            else:
                try:
                    put_result = self.result_cache.put(
                        cache_epoch=cache_epoch,
                        sql_sha256=sql_sha256(sql),
                        strategy_id=strategy_id,
                        columns=columns,
                        rows=cache_capture.rows,
                        source_execution_id=execution_id,
                        stored_at=finished_at,
                    )
                except Exception as exc:  # noqa: BLE001 - query already succeeded
                    warnings.append(
                        "CACHE_STORE_FAILED: "
                        f"{type(exc).__name__}: {exc}; the query result is still valid"
                    )
                else:
                    if put_result.stored:
                        warnings.append(
                            "CACHE_MISS_STORED: complete result stored under "
                            f"epoch {cache_epoch!r}"
                        )
                    else:
                        warnings.append(
                            f"CACHE_NOT_STORED: {put_result.reason}"
                        )

        client_wall_ms = (time.perf_counter() - wall_started) * 1000
        logger.info(
            "query executed",
            extra={
                "event": "query",
                "query_id": query_id,
                "execution_id": execution_id,
                "status": status.value,
                "adapter": self.adapter_name,
                "strategy_id": strategy_id,
                "row_count": row_count,
                "error_type": error_type,
                "timed_out": timed_out,
                "timeout_ms": timeout_ms,
                "served_from_cache": False,
                "cache_epoch": cache_epoch,
                "elapsed_ms": round(client_wall_ms, 3),
            },
        )
        return ExecutionResult(
            query_id=query_id,
            execution_id=execution_id,
            status=status,
            sql_sha256=sql_sha256(sql),
            strategy_id=strategy_id,
            adapter=self.adapter_name,
            columns=columns,
            rows_preview=preview,
            preview_truncated=row_count > len(preview),
            row_count=row_count,
            fetch_mode=FetchMode.FULL,
            served_from_cache=False,
            cache_source_execution_id=None,
            cache_epoch=cache_epoch,
            metrics=TimingMetrics(
                execute_call_ms=round(execute_call_ms, 3),
                result_fetch_ms=round(result_fetch_ms, 3),
                client_wall_ms=round(client_wall_ms, 3),
            ),
            started_at=started_at,
            finished_at=finished_at,
            error_type=error_type,
            error_message=error_message,
            timeout_ms=timeout_ms,
            timed_out=timed_out,
            request_dispatched=dispatch_started,
            request_completed=request_completed,
            retry_safe=retry_safe,
            retry_unsafe_reason=retry_unsafe_reason,
            warnings=warnings,
        )

    @staticmethod
    def _looks_like_timeout(exc: BaseException) -> bool:
        type_name = type(exc).__name__.lower()
        if "timeout" in type_name or "timedout" in type_name:
            return True
        return bool(
            re.search(
                r"\btimeout\b|\btimed\s+out\b|\btime\s+out\b",
                str(exc),
                flags=re.IGNORECASE,
            )
        )

    @staticmethod
    def _values_for_row(columns: list[ColumnMeta], row: Any) -> list[Any]:
        values = [_decode(field.value) for field in row.get_fields()]
        if columns and columns[0].name.lower() == "time":
            if len(values) == len(columns) - 1:
                timestamp = row.get_timestamp()
                return [_decode(timestamp)] + values
            if len(values) == len(columns):
                return values
            raise AdapterError(
                f"unexpected row shape: {len(values)} fields for "
                f"{len(columns)} columns including Time"
            )
        if len(values) != len(columns):
            raise AdapterError(
                f"unexpected row shape: {len(values)} fields for {len(columns)} columns"
            )
        return values

    def doctor(self) -> DoctorReport:
        started = time.perf_counter()
        report = DoctorReport(connected=False)
        report.capabilities = CapabilityReport(
            explain_analyze="not_probed",
            notes=[
                (
                    "SQL guard is structural: SQLGlot tokenization plus an AST "
                    "statement-type check when parseable; the token-level check "
                    "remains for verified IoTDB syntax SQLGlot cannot parse. "
                    "It is not a full IoTDB parser."
                ),
                "Table model is not probed by this tree-model doctor.",
                "EXPLAIN ANALYZE is never run on this normal doctor path.",
            ],
        )
        try:
            self.connect()
            report.connected = True
            report.server = ServerInfo(
                model="unknown", client_version=self._client_version
            )

            version_result = self._metadata("SHOW VERSION", preview_rows=1)
            if version_result.status == ExecutionStatus.SUCCESS:
                report.capabilities.show_version = True
                if version_result.rows_preview:
                    row = version_result.rows_preview[0]
                    if len(row) >= 2:
                        report.server.version = str(row[0])
                        report.server.build_info = str(row[1])
                    else:
                        report.warnings.append("SHOW VERSION returned fewer than 2 columns")
                else:
                    report.warnings.append("SHOW VERSION returned no rows")
            else:
                report.warnings.append(
                    f"SHOW VERSION failed: {version_result.error_type}: "
                    f"{version_result.error_message}"
                )

            db_result = self._metadata("SHOW DATABASES", preview_rows=1000)
            if db_result.status == ExecutionStatus.SUCCESS:
                report.capabilities.show_databases = True
                report.capabilities.databases = [
                    str(row[0]) for row in db_result.rows_preview if row
                ]
            else:
                report.warnings.append(
                    f"SHOW DATABASES failed: {db_result.error_type}: "
                    f"{db_result.error_message}"
                )

            series_result = self._metadata(
                "SHOW TIMESERIES root.** LIMIT 1", preview_rows=1
            )
            if series_result.status == ExecutionStatus.SUCCESS:
                report.capabilities.show_timeseries = True
                report.server.model = "tree"
                if series_result.rows_preview and series_result.rows_preview[0]:
                    path = str(series_result.rows_preview[0][0])
                    if "." in path:
                        device, series = path.rsplit(".", 1)
                        self._probe_explain(report, device, series)
                    else:
                        report.warnings.append(
                            f"could not split discovered series path: {path}"
                        )
                else:
                    report.warnings.append(
                        "SHOW TIMESERIES returned no series; EXPLAIN probes skipped"
                    )
            else:
                report.warnings.append(
                    f"SHOW TIMESERIES failed: {series_result.error_type}: "
                    f"{series_result.error_message}"
                )
                report.warnings.append(
                    "tree-model probe failed; database model remains unknown"
                )

            if report.server.version and report.server.version != "2.0.6":
                report.warnings.append(
                    f"server version {report.server.version} differs from certified 2.0.6"
                )
            if self._client_version and self._client_version != "2.0.6":
                report.warnings.append(
                    f"client version {self._client_version} differs from locked 2.0.6"
                )
            if (
                report.server.version
                and self._client_version
                and report.server.version != self._client_version
            ):
                report.warnings.append("server and client versions differ")

            version_certified = (
                report.server.version == "2.0.6"
                and self._client_version == "2.0.6"
            )
            if version_certified:
                report.capabilities.explain_analyze = (
                    "available_but_executes_query_not_run"
                )
            elif report.connected:
                report.capabilities.explain_analyze = (
                    "not_certified_for_observed_version"
                )
                report.warnings.append(
                    "EXPLAIN ANALYZE capability is not certified for the "
                    "observed server/client version; not probed"
                )

            report.ok = (
                report.connected
                and report.server is not None
                and report.server.model == "tree"
                and report.capabilities.show_version
                and report.capabilities.show_databases
                and version_certified
            )
        except Exception as exc:  # noqa: BLE001 - doctor reports, never crashes
            report.ok = False
            report.error_type = type(exc).__name__
            report.error_message = str(exc)[:4000]
        finally:
            self.close()
            report.elapsed_ms = round((time.perf_counter() - started) * 1000, 3)
        return report

    def _probe_explain(self, report: DoctorReport, device: str, series: str) -> None:
        if not _is_safe_tree_path(device) or not _is_safe_tree_path(series):
            report.warnings.append(
                "discovered series path is outside the safe unquoted tree-path "
                "subset; EXPLAIN probes skipped"
            )
            return
        explain = self._metadata(
            f"EXPLAIN SELECT {series} FROM {device} LIMIT 1", preview_rows=1
        )
        if explain.status == ExecutionStatus.SUCCESS and explain.row_count > 0:
            report.capabilities.explain_text = True
        else:
            report.warnings.append(
                "EXPLAIN probe failed: "
                f"{explain.error_type}: {explain.error_message}"
            )

        last_explain = self._metadata(
            f"EXPLAIN SELECT LAST {series} FROM {device}", preview_rows=1
        )
        if last_explain.status == ExecutionStatus.SUCCESS and last_explain.row_count > 0:
            report.capabilities.explain_last_query = True
        else:
            report.warnings.append(
                "EXPLAIN LAST probe failed: "
                f"{last_explain.error_type}: {last_explain.error_message}"
            )

    def _metadata(self, sql: str, preview_rows: int) -> ExecutionResult:
        return self._run_raw(
            sql=sql,
            query_id=f"doctor-{uuid4().hex}",
            execution_id=uuid4().hex,
            started_at=utc_now(),
            preview_rows=preview_rows,
        )
