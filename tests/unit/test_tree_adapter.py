from __future__ import annotations

import logging
import math
from typing import Any

import pytest

from tsdb_optimizer.adapters.iotdb.result_cache import ResultCache
from tsdb_optimizer.adapters.iotdb.tree_adapter import IoTDBTreeAdapter
from tsdb_optimizer.config import Settings
from tsdb_optimizer.errors import AdapterError, UnsafeSQLError
from tsdb_optimizer.schemas import QueryRequest


class FakeField:
    def __init__(self, value: Any):
        self.value = value


class FakeRow:
    def __init__(self, timestamp: int, values: list[Any]):
        self.timestamp = timestamp
        self.values = values

    def get_timestamp(self) -> int:
        return self.timestamp

    def get_fields(self) -> list[FakeField]:
        return [FakeField(value) for value in self.values]


class FakeDataset:
    def __init__(
        self,
        columns: list[str],
        types: list[str],
        rows: list[FakeRow],
        *,
        fail_on_fetch: bool = False,
        close_error: Exception | None = None,
        types_error: Exception | None = None,
    ):
        self.columns = columns
        self.types = types
        self._rows = list(rows)
        self._iterator = iter(self._rows)
        self._current: FakeRow | None = None
        self.fail_on_fetch = fail_on_fetch
        self.close_error = close_error
        self.types_error = types_error
        self.closed = False

    def get_column_names(self) -> list[str]:
        return list(self.columns)

    def get_column_types(self) -> list[str]:
        if self.types_error is not None:
            raise self.types_error
        return list(self.types)

    def has_next(self) -> bool:
        if self.fail_on_fetch:
            raise RuntimeError("fetch failed")
        try:
            self._current = next(self._iterator)
        except StopIteration:
            return False
        return True

    def next(self) -> FakeRow:
        assert self._current is not None
        return self._current

    def close_operation_handle(self) -> None:
        self.closed = True
        if self.close_error is not None:
            raise self.close_error


class FakeSession:
    def __init__(
        self,
        responses: dict[str, Any],
        *,
        open_error: Exception | None = None,
        execute_error: Exception | None = None,
    ):
        self.responses = responses
        self.open_error = open_error
        self.execute_error = execute_error
        self.open_called = False
        self.close_called = False
        self.executed: list[tuple[str, int]] = []
        self.last_timeout: int | None = None

    def open(self, flag: bool = False) -> None:
        self.open_called = True
        if self.open_error is not None:
            raise self.open_error

    def close(self) -> None:
        self.close_called = True

    def reconnect(self) -> bool:
        # The adapter replaces this on the instance so the SDK cannot silently
        # resend a request.  If it is ever called, the test has leaked through
        # the at-most-once guard.
        raise AssertionError("reconnect must be disabled by the adapter")

    def execute_query_statement(self, sql: str, timeout: int = 0) -> FakeDataset:
        self.executed.append((sql, timeout))
        self.last_timeout = timeout
        if self.execute_error is not None:
            raise self.execute_error
        response = self.responses[sql]
        return response() if callable(response) else response


def _settings() -> Settings:
    return Settings(iotdb_password="test-password")


def _dataset(
    columns: list[str],
    rows: list[list[Any]],
    types: list[str] | None = None,
    **kwargs: Any,
) -> FakeDataset:
    return FakeDataset(
        columns,
        types or ["5"] * len(columns),
        [FakeRow(0, row) for row in rows],
        **kwargs,
    )


def _query() -> QueryRequest:
    return QueryRequest(sql="SELECT s_130 FROM root.test.g_0.d_0")


def test_execute_readonly_consumes_rows_and_closes(caplog) -> None:
    caplog.set_level(logging.INFO)
    dataset = _dataset(
        ["Time", "root.test.g_0.d_0.s_130"],
        [[123, b"1.5"]],
        types=["2", "5"],
    )
    session = FakeSession(
        {"SELECT s_130 FROM root.test.g_0.d_0": dataset}
    )
    adapter = IoTDBTreeAdapter(_settings(), session_factory=lambda _: session)

    result = adapter.execute_readonly(_query(), preview_rows=5)

    assert result.status.value == "SUCCESS"
    assert result.row_count == 1
    assert result.rows_preview == [[123, "1.5"]]
    assert [column.name for column in result.columns] == [
        "Time",
        "root.test.g_0.d_0.s_130",
    ]
    assert result.preview_truncated is False
    assert result.metrics.execute_call_ms >= 0
    assert result.metrics.result_fetch_ms >= 0
    assert result.metrics.client_wall_ms >= 0
    assert dataset.closed is True
    assert session.open_called is True

    query_logs = [
        record
        for record in caplog.records
        if getattr(record, "event", None) == "query"
    ]
    assert query_logs
    assert query_logs[-1].query_id == result.query_id
    assert query_logs[-1].execution_id == result.execution_id
    assert query_logs[-1].status == "SUCCESS"
    assert query_logs[-1].row_count == 1

    adapter.close()
    assert session.close_called is True


def test_preview_is_truncated_but_all_rows_consumed() -> None:
    dataset = _dataset(["Value"], [[1], [2], [3], [4], [5]])
    session = FakeSession({"SELECT s_130 FROM root.test.g_0.d_0": dataset})
    adapter = IoTDBTreeAdapter(_settings(), session_factory=lambda _: session)

    result = adapter.execute_readonly(_query(), preview_rows=2)

    assert result.status.value == "SUCCESS"
    assert result.row_count == 5
    assert result.rows_preview == [[1], [2]]
    assert result.preview_truncated is True
    assert dataset.closed is True


def test_fetch_error_is_structured_and_dataset_closed() -> None:
    dataset = _dataset(["Value"], [[1]], fail_on_fetch=True)
    session = FakeSession({"SELECT s_130 FROM root.test.g_0.d_0": dataset})
    adapter = IoTDBTreeAdapter(_settings(), session_factory=lambda _: session)

    result = adapter.execute_readonly(_query(), preview_rows=5)

    assert result.status.value == "ERROR"
    assert result.error_type == "RuntimeError"
    assert "fetch failed" in (result.error_message or "")
    assert dataset.closed is True


def test_execute_error_is_structured() -> None:
    session = FakeSession(
        {"SELECT s_130 FROM root.test.g_0.d_0": _dataset(["Value"], [[1]])},
        execute_error=RuntimeError("execute failed"),
    )
    adapter = IoTDBTreeAdapter(_settings(), session_factory=lambda _: session)

    result = adapter.execute_readonly(_query(), preview_rows=5)

    assert result.status.value == "ERROR"
    assert result.error_type == "RuntimeError"
    assert "execute failed" in (result.error_message or "")


def test_missing_type_metadata_is_a_warning_not_silent() -> None:
    dataset = _dataset(
        ["Value"], [[1]], types_error=RuntimeError("types unavailable")
    )
    session = FakeSession({"SELECT s_130 FROM root.test.g_0.d_0": dataset})
    adapter = IoTDBTreeAdapter(_settings(), session_factory=lambda _: session)

    result = adapter.execute_readonly(_query(), preview_rows=5)

    assert result.status.value == "SUCCESS"
    assert result.columns[0].data_type is None
    assert any(
        "column type metadata unavailable" in warning
        and "RuntimeError" in warning
        for warning in result.warnings
    )


def test_close_failure_is_a_warning() -> None:
    dataset = _dataset(
        ["Value"], [[1]], close_error=RuntimeError("close failed")
    )
    session = FakeSession({"SELECT s_130 FROM root.test.g_0.d_0": dataset})
    adapter = IoTDBTreeAdapter(_settings(), session_factory=lambda _: session)

    result = adapter.execute_readonly(_query(), preview_rows=5)

    assert result.status.value == "SUCCESS"
    assert any("result set close failed" in warning for warning in result.warnings)


def test_row_shape_mismatch_is_an_error() -> None:
    dataset = _dataset(["A", "B"], [[1]])
    session = FakeSession({"SELECT s_130 FROM root.test.g_0.d_0": dataset})
    adapter = IoTDBTreeAdapter(_settings(), session_factory=lambda _: session)

    result = adapter.execute_readonly(_query(), preview_rows=5)

    assert result.status.value == "ERROR"
    assert "unexpected row shape" in (result.error_message or "")


def test_unsafe_sql_is_rejected_before_connecting() -> None:
    created: list[FakeSession] = []

    def factory(_: Settings) -> FakeSession:
        session = FakeSession({})
        created.append(session)
        return session

    adapter = IoTDBTreeAdapter(_settings(), session_factory=factory)
    with pytest.raises(UnsafeSQLError):
        adapter.execute_readonly(QueryRequest(sql="DELETE FROM root.test.g_0.d_0"))
    assert created == []


class ReadOnlyReconnectSession(FakeSession):
    """Session whose reconnect method cannot be shadowed."""

    @property
    def reconnect(self):
        return lambda: True

    @reconnect.setter
    def reconnect(self, value: Any) -> None:
        raise AttributeError("reconnect is read-only")


def test_connect_disables_the_sdk_reconnect_and_resend_path() -> None:
    session = FakeSession({})
    adapter = IoTDBTreeAdapter(_settings(), session_factory=lambda _: session)

    adapter.connect()

    assert adapter._sdk_reconnect_disabled is True
    assert session.reconnect() is False
    adapter.close()
    assert session.close_called is True


def test_connect_fails_closed_if_reconnect_cannot_be_disabled() -> None:
    session = ReadOnlyReconnectSession({})
    adapter = IoTDBTreeAdapter(_settings(), session_factory=lambda _: session)

    with pytest.raises(AdapterError, match="at-most-once"):
        adapter.connect()

    assert adapter.connected is False
    assert session.close_called is True


def _doctor_session() -> FakeSession:
    responses: dict[str, Any] = {
        "SHOW VERSION": lambda: _dataset(
            ["Version", "BuildInfo"], [[b"2.0.6", b"c6ecbde"]]
        ),
        "SHOW DATABASES": lambda: _dataset(
            ["Database"], [[b"root.test"]]
        ),
        "SHOW TIMESERIES root.** LIMIT 1": lambda: _dataset(
            ["Timeseries"], [[b"root.test.g_0.d_0.s_130"]]
        ),
        "EXPLAIN SELECT s_130 FROM root.test.g_0.d_0 LIMIT 1": lambda: _dataset(
            ["Plan"], [["AlignedSeriesScan"]]
        ),
        "EXPLAIN SELECT LAST s_130 FROM root.test.g_0.d_0": lambda: _dataset(
            ["Plan"], [["LastQuery"]]
        ),
    }
    return FakeSession(responses)


def test_doctor_reports_probed_capabilities() -> None:
    session = _doctor_session()
    adapter = IoTDBTreeAdapter(_settings(), session_factory=lambda _: session)
    adapter._client_version = "2.0.6"

    report = adapter.doctor()

    assert report.ok is True
    assert report.connected is True
    assert report.server is not None
    assert report.server.model == "tree"
    assert report.server.version == "2.0.6"
    assert report.server.build_info == "c6ecbde"
    assert report.capabilities.databases == ["root.test"]
    assert report.capabilities.show_version is True
    assert report.capabilities.show_databases is True
    assert report.capabilities.show_timeseries is True
    assert report.capabilities.explain_text is True
    assert report.capabilities.explain_last_query is True
    assert report.capabilities.explain_analyze == (
        "available_but_executes_query_not_run"
    )
    assert session.close_called is True


@pytest.mark.parametrize(
    "series_path",
    [
        "root.test.g_0.d_0.s_130 LIMIT 1; DROP DATABASE root.test",
        "root.test.`g 0`.d_0.s_130",
    ],
)
def test_doctor_skips_explain_for_unsafe_discovered_paths(series_path: str) -> None:
    responses = dict(_doctor_session().responses)
    responses["SHOW TIMESERIES root.** LIMIT 1"] = lambda: _dataset(
        ["Timeseries"], [[series_path]]
    )
    session = FakeSession(responses)
    adapter = IoTDBTreeAdapter(_settings(), session_factory=lambda _: session)
    adapter._client_version = "2.0.6"

    report = adapter.doctor()

    assert report.capabilities.show_timeseries is True
    assert report.capabilities.explain_text is False
    assert report.capabilities.explain_last_query is False
    assert any(
        "safe unquoted tree-path subset" in warning for warning in report.warnings
    )
    assert not any(sql.startswith("EXPLAIN") for sql, _ in session.executed)


def test_doctor_connection_failure_is_reported() -> None:
    session = FakeSession({}, open_error=RuntimeError("connection refused"))
    adapter = IoTDBTreeAdapter(_settings(), session_factory=lambda _: session)

    report = adapter.doctor()

    assert report.ok is False
    assert report.connected is False
    assert report.capabilities.explain_analyze == "not_probed"
    assert report.error_type == "AdapterError"
    assert "connection refused" in (report.error_message or "")
    assert session.close_called is True


def test_doctor_refuses_certification_for_a_non_2_0_6_client() -> None:
    session = _doctor_session()
    adapter = IoTDBTreeAdapter(_settings(), session_factory=lambda _: session)
    adapter._client_version = "2.0.10"

    report = adapter.doctor()

    assert report.ok is False
    assert report.connected is True
    assert report.server is not None
    assert report.server.version == "2.0.6"
    assert report.capabilities.explain_analyze == (
        "not_certified_for_observed_version"
    )
    assert any("client version 2.0.10" in warning for warning in report.warnings)
    assert any("EXPLAIN ANALYZE capability" in warning for warning in report.warnings)


def test_doctor_refuses_certification_for_a_non_2_0_6_server() -> None:
    responses = dict(_doctor_session().responses)
    responses["SHOW VERSION"] = lambda: _dataset(
        ["Version", "BuildInfo"], [[b"2.0.7", b"other"]]
    )
    adapter = IoTDBTreeAdapter(
        _settings(),
        session_factory=lambda _: FakeSession(responses),
    )
    adapter._client_version = "2.0.6"

    report = adapter.doctor()

    assert report.ok is False
    assert report.capabilities.explain_analyze == (
        "not_certified_for_observed_version"
    )
    assert any("server version 2.0.7" in warning for warning in report.warnings)


def test_two_executions_close_each_dataset() -> None:
    datasets: list[FakeDataset] = []

    def make_dataset() -> FakeDataset:
        dataset = _dataset(["Value"], [[1]])
        datasets.append(dataset)
        return dataset

    session = FakeSession(
        {"SELECT s_130 FROM root.test.g_0.d_0": make_dataset}
    )
    adapter = IoTDBTreeAdapter(_settings(), session_factory=lambda _: session)

    first = adapter.execute_readonly(_query(), preview_rows=5)
    second = adapter.execute_readonly(_query(), preview_rows=5)

    assert first.status.value == "SUCCESS"
    assert second.status.value == "SUCCESS"
    assert len(datasets) == 2
    assert all(dataset.closed for dataset in datasets)
    adapter.close()
    assert session.close_called is True


def test_close_is_idempotent() -> None:
    session = FakeSession({})
    adapter = IoTDBTreeAdapter(_settings(), session_factory=lambda _: session)
    adapter.connect()
    adapter.close()
    adapter.close()
    assert session.close_called is True


def test_omitted_timeout_sends_sdk_default_zero() -> None:
    dataset = _dataset(["Value"], [[1]])
    session = FakeSession({"SELECT s_130 FROM root.test.g_0.d_0": dataset})
    adapter = IoTDBTreeAdapter(_settings(), session_factory=lambda _: session)

    result = adapter.execute_readonly(_query(), preview_rows=5)

    # The installed client receives 0 when no timeout was requested.  This
    # test proves forwarding only; the server-side meaning of 0 is UNKNOWN.
    assert session.last_timeout == 0
    assert result.timeout_ms is None


def test_timeout_ms_is_passed_to_the_iotdb_client() -> None:
    dataset = _dataset(["Value"], [[1]])
    session = FakeSession({"SELECT s_130 FROM root.test.g_0.d_0": dataset})
    adapter = IoTDBTreeAdapter(_settings(), session_factory=lambda _: session)

    result = adapter.execute_readonly(
        QueryRequest(
            sql="SELECT s_130 FROM root.test.g_0.d_0",
            timeout_ms=1234,
        ),
        preview_rows=5,
    )

    assert session.last_timeout == 1234
    assert result.timeout_ms == 1234
    assert result.timed_out is False


def test_timeout_error_is_marked_but_still_structured() -> None:
    session = FakeSession(
        {},
        execute_error=TimeoutError("request timed out"),
    )
    adapter = IoTDBTreeAdapter(_settings(), session_factory=lambda _: session)

    result = adapter.execute_readonly(
        QueryRequest(
            sql="SELECT s_130 FROM root.test.g_0.d_0",
            timeout_ms=50,
        ),
        preview_rows=5,
    )

    assert result.status.value == "ERROR"
    assert result.timeout_ms == 50
    assert result.timed_out is True
    assert result.error_type == "TimeoutError"


def test_execute_explain_prefixes_explain_and_never_analyze() -> None:
    dataset = _dataset(
        ["distribution plan"],
        [["┌───────────────┐"], ["│AlignedSeriesScan-1│"], ["└───────────────┘"]],
    )
    session = FakeSession(
        {"EXPLAIN SELECT s_130 FROM root.test.g_0.d_0": dataset}
    )
    adapter = IoTDBTreeAdapter(_settings(), session_factory=lambda _: session)

    result = adapter.execute_explain(
        "SELECT s_130 FROM root.test.g_0.d_0",
        query_id="query-1",
        preview_rows=10,
    )

    assert result.status.value == "SUCCESS"
    assert result.query_id == "query-1"
    assert result.row_count == 3
    assert session.executed[-1][0] == "EXPLAIN SELECT s_130 FROM root.test.g_0.d_0"
    assert "ANALYZE" not in session.executed[-1][0].upper()
    assert session.executed[-1][1] == 0
    assert result.timeout_ms is None
    assert result.timed_out is False


def test_execute_explain_rejects_unsafe_sql_before_connecting() -> None:
    created: list[FakeSession] = []

    def factory(_: Settings) -> FakeSession:
        session = FakeSession({})
        created.append(session)
        return session

    adapter = IoTDBTreeAdapter(_settings(), session_factory=factory)
    with pytest.raises(UnsafeSQLError):
        adapter.execute_explain("DELETE FROM root.test.g_0.d_0")
    assert created == []


EPOCH = "epoch-2026-11-01"


def _cache_adapter(
    session: FakeSession,
    *,
    cache: ResultCache | None = None,
    factory: Any | None = None,
) -> IoTDBTreeAdapter:
    return IoTDBTreeAdapter(
        _settings(),
        session_factory=factory or (lambda _: session),
        result_cache=cache or ResultCache(),
    )


def test_cache_miss_stores_and_hit_serves_without_dispatch() -> None:
    datasets: list[FakeDataset] = []

    def make_dataset() -> FakeDataset:
        dataset = _dataset(["Value"], [[1], [2]])
        datasets.append(dataset)
        return dataset

    session = FakeSession(
        {"SELECT s_130 FROM root.test.g_0.d_0": make_dataset}
    )
    cache = ResultCache()
    adapter = _cache_adapter(session, cache=cache)

    first = adapter.execute_readonly(
        _query(),
        preview_rows=10,
        strategy_id="S1_REPEAT_CACHE",
        execution_options={"cache_epoch": EPOCH},
    )

    assert first.status.value == "SUCCESS"
    assert first.served_from_cache is False
    assert first.request_dispatched is True
    assert first.request_completed is True
    assert first.cache_epoch == EPOCH
    assert first.row_count == 2
    assert any("CACHE_MISS_STORED" in warning for warning in first.warnings)
    assert len(session.executed) == 1
    assert cache.entries == 1

    second = adapter.execute_readonly(
        _query(),
        preview_rows=10,
        strategy_id="S1_REPEAT_CACHE",
        execution_options={"cache_epoch": EPOCH},
    )

    assert second.status.value == "SUCCESS"
    assert second.served_from_cache is True
    assert second.request_dispatched is False
    assert second.request_completed is False
    assert second.retry_safe is False
    assert second.cache_source_execution_id == first.execution_id
    assert second.cache_epoch == EPOCH
    assert second.row_count == 2
    assert second.rows_preview == first.rows_preview
    assert [column.name for column in second.columns] == [
        column.name for column in first.columns
    ]
    assert second.metrics.execute_call_ms == 0.0
    assert second.metrics.result_fetch_ms == 0.0
    assert "cache lookup" in " ".join(second.warnings)
    assert any("CACHE_HIT" in warning for warning in second.warnings)
    # Column metadata is frozen, so a caller cannot mutate the cached entry
    # through the miss result.
    with pytest.raises(Exception):
        first.columns[0].name = "MUTATED"
    assert second.columns[0].name == first.columns[0].name
    # The second call dispatched no request and consumed no new dataset.
    assert len(session.executed) == 1
    assert len(datasets) == 1


def _assert_rows_equal_with_nan(left: list[list[Any]], right: list[list[Any]]) -> None:
    assert len(left) == len(right)
    for left_row, right_row in zip(left, right):
        assert len(left_row) == len(right_row)
        for left_value, right_value in zip(left_row, right_row):
            if (
                isinstance(left_value, float)
                and isinstance(right_value, float)
                and math.isnan(left_value)
                and math.isnan(right_value)
            ):
                continue
            assert left_value == right_value


def test_cache_hit_preserves_boundary_values_and_duplicate_timestamps() -> None:
    timestamp_min = -(2**63)
    timestamp_max = 2**63 - 1
    rows = [
        [timestamp_min, None, float("nan")],
        [timestamp_min, None, float("inf")],
        [timestamp_max, 1, 0.0],
    ]
    session = FakeSession(
        {
            "SELECT s_130 FROM root.test.g_0.d_0": _dataset(
                ["Time", "a", "b"], rows, types=["2", "5", "6"]
            )
        }
    )
    adapter = _cache_adapter(session)

    first = adapter.execute_readonly(
        _query(),
        preview_rows=10,
        strategy_id="S1_REPEAT_CACHE",
        execution_options={"cache_epoch": EPOCH},
    )
    second = adapter.execute_readonly(
        _query(),
        preview_rows=10,
        strategy_id="S1_REPEAT_CACHE",
        execution_options={"cache_epoch": EPOCH},
    )

    assert first.status.value == "SUCCESS"
    assert second.served_from_cache is True
    assert first.row_count == 3
    assert second.row_count == 3
    _assert_rows_equal_with_nan(first.rows_preview, rows)
    _assert_rows_equal_with_nan(second.rows_preview, first.rows_preview)
    assert [row[0] for row in second.rows_preview] == [
        timestamp_min,
        timestamp_min,
        timestamp_max,
    ]
    assert second.rows_preview[0][1] is None
    assert math.isnan(second.rows_preview[0][2])
    assert len(session.executed) == 1


def test_cache_hit_preserves_empty_result() -> None:
    session = FakeSession(
        {"SELECT s_130 FROM root.test.g_0.d_0": _dataset(["Value"], [])}
    )
    adapter = _cache_adapter(session)

    first = adapter.execute_readonly(
        _query(),
        preview_rows=10,
        strategy_id="S1_REPEAT_CACHE",
        execution_options={"cache_epoch": EPOCH},
    )
    second = adapter.execute_readonly(
        _query(),
        preview_rows=10,
        strategy_id="S1_REPEAT_CACHE",
        execution_options={"cache_epoch": EPOCH},
    )

    assert first.row_count == 0
    assert second.row_count == 0
    assert first.rows_preview == []
    assert second.rows_preview == []
    assert second.preview_truncated is False
    assert [column.name for column in second.columns] == [
        column.name for column in first.columns
    ]
    assert len(session.executed) == 1


def test_cache_hit_log_uses_the_hit_execution_id_and_names_the_source(
    caplog,
) -> None:
    caplog.set_level(logging.INFO)
    session = FakeSession(
        {"SELECT s_130 FROM root.test.g_0.d_0": lambda: _dataset(["Value"], [[1]])}
    )
    adapter = _cache_adapter(session)

    first = adapter.execute_readonly(
        _query(),
        preview_rows=10,
        strategy_id="S1_REPEAT_CACHE",
        execution_options={"cache_epoch": EPOCH},
    )
    hit = adapter.execute_readonly(
        _query(),
        preview_rows=10,
        strategy_id="S1_REPEAT_CACHE",
        execution_options={"cache_epoch": EPOCH},
    )

    hit_logs = [
        record
        for record in caplog.records
        if getattr(record, "event", None) == "cache_hit"
    ]
    assert len(hit_logs) == 1
    hit_log = hit_logs[0]
    assert hit_log.execution_id == hit.execution_id
    assert hit_log.cache_source_execution_id == first.execution_id
    assert hit_log.execution_id != hit_log.cache_source_execution_id
    assert hit_log.cache_lookup_ms >= 0.0
    assert hit.cache_source_execution_id == first.execution_id


def test_cache_hit_needs_no_connection_at_all() -> None:
    session = FakeSession(
        {
            "SELECT s_130 FROM root.test.g_0.d_0": lambda: _dataset(
                ["Value"], [[7]]
            )
        }
    )
    cache = ResultCache()
    first_adapter = _cache_adapter(session, cache=cache)
    first = first_adapter.execute_readonly(
        _query(),
        preview_rows=10,
        strategy_id="S1_REPEAT_CACHE",
        execution_options={"cache_epoch": EPOCH},
    )
    assert first.served_from_cache is False
    first_adapter.close()

    created: list[FakeSession] = []

    def factory(_: Settings) -> FakeSession:
        session_double = FakeSession({})
        created.append(session_double)
        return session_double

    second_adapter = _cache_adapter(session, cache=cache, factory=factory)
    hit = second_adapter.execute_readonly(
        _query(),
        preview_rows=10,
        strategy_id="S1_REPEAT_CACHE",
        execution_options={"cache_epoch": EPOCH},
    )
    assert hit.served_from_cache is True
    assert hit.cache_source_execution_id == first.execution_id
    assert created == []
    assert second_adapter.connected is False


def test_cache_hit_preview_is_bounded_but_row_count_is_complete() -> None:
    session = FakeSession(
        {
            "SELECT s_130 FROM root.test.g_0.d_0": lambda: _dataset(
                ["Value"], [[1], [2], [3]]
            )
        }
    )
    adapter = _cache_adapter(session)
    first = adapter.execute_readonly(
        _query(),
        preview_rows=10,
        strategy_id="S1_REPEAT_CACHE",
        execution_options={"cache_epoch": EPOCH},
    )
    assert first.row_count == 3

    hit = adapter.execute_readonly(
        _query(),
        preview_rows=1,
        strategy_id="S1_REPEAT_CACHE",
        execution_options={"cache_epoch": EPOCH},
    )
    assert hit.served_from_cache is True
    assert hit.row_count == 3
    assert hit.rows_preview == [[1]]
    assert hit.preview_truncated is True
    assert len(session.executed) == 1


def test_a_different_epoch_is_a_miss_not_a_false_hit() -> None:
    session = FakeSession(
        {
            "SELECT s_130 FROM root.test.g_0.d_0": lambda: _dataset(
                ["Value"], [[1]]
            )
        }
    )
    cache = ResultCache()
    adapter = _cache_adapter(session, cache=cache)
    first = adapter.execute_readonly(
        _query(),
        preview_rows=10,
        strategy_id="S1_REPEAT_CACHE",
        execution_options={"cache_epoch": "epoch-A"},
    )
    second = adapter.execute_readonly(
        _query(),
        preview_rows=10,
        strategy_id="S1_REPEAT_CACHE",
        execution_options={"cache_epoch": "epoch-B"},
    )
    assert first.served_from_cache is False
    assert second.served_from_cache is False
    assert len(session.executed) == 2
    assert cache.entries == 2


def test_without_epoch_the_adapter_never_caches() -> None:
    session = FakeSession(
        {
            "SELECT s_130 FROM root.test.g_0.d_0": lambda: _dataset(
                ["Value"], [[1]]
            )
        }
    )
    cache = ResultCache()
    adapter = _cache_adapter(session, cache=cache)
    for _ in range(2):
        result = adapter.execute_readonly(_query(), preview_rows=10)
        assert result.served_from_cache is False
        assert result.request_dispatched is True
    assert len(session.executed) == 2
    assert cache.entries == 0


class FailingLookupCache(ResultCache):
    def get(self, cache_key: str) -> None:
        del cache_key
        raise RuntimeError("cache lookup exploded")


class FailingStoreCache(ResultCache):
    def put(self, **kwargs: Any) -> Any:
        del kwargs
        raise RuntimeError("cache store exploded")


def test_lookup_failure_is_a_pre_dispatch_error_and_never_connects() -> None:
    created: list[FakeSession] = []

    def factory(_: Settings) -> FakeSession:
        session = FakeSession({})
        created.append(session)
        return session

    adapter = _cache_adapter(
        FakeSession({}),
        cache=FailingLookupCache(),
        factory=factory,
    )
    result = adapter.execute_readonly(
        _query(),
        preview_rows=10,
        strategy_id="S1_REPEAT_CACHE",
        execution_options={"cache_epoch": EPOCH},
    )

    assert result.status.value == "ERROR"
    assert result.error_type == "RuntimeError"
    assert "cache lookup exploded" in (result.error_message or "")
    assert result.request_dispatched is False
    assert result.request_completed is False
    assert result.served_from_cache is False
    assert result.metrics.execute_call_ms == 0.0
    assert result.metrics.result_fetch_ms == 0.0
    assert any("CACHE_LOOKUP_FAILED" in warning for warning in result.warnings)
    assert created == []


def test_store_failure_keeps_the_real_success() -> None:
    session = FakeSession(
        {
            "SELECT s_130 FROM root.test.g_0.d_0": lambda: _dataset(
                ["Value"], [[1], [2]]
            )
        }
    )
    adapter = _cache_adapter(session, cache=FailingStoreCache())
    result = adapter.execute_readonly(
        _query(),
        preview_rows=10,
        strategy_id="S1_REPEAT_CACHE",
        execution_options={"cache_epoch": EPOCH},
    )
    assert result.status.value == "SUCCESS"
    assert result.row_count == 2
    assert result.request_dispatched is True
    assert result.request_completed is True
    assert any("CACHE_STORE_FAILED" in warning for warning in result.warnings)


def test_result_over_the_entry_limit_is_not_partially_cached() -> None:
    datasets: list[FakeDataset] = []

    def make_dataset() -> FakeDataset:
        dataset = _dataset(["Value"], [[1], [2]])
        datasets.append(dataset)
        return dataset

    session = FakeSession(
        {"SELECT s_130 FROM root.test.g_0.d_0": make_dataset}
    )
    cache = ResultCache(max_entry_rows=1)
    adapter = _cache_adapter(session, cache=cache)
    first = adapter.execute_readonly(
        _query(),
        preview_rows=10,
        strategy_id="S1_REPEAT_CACHE",
        execution_options={"cache_epoch": EPOCH},
    )
    assert first.status.value == "SUCCESS"
    assert first.row_count == 2
    assert any("CACHE_NOT_STORED" in warning for warning in first.warnings)
    assert cache.entries == 0

    second = adapter.execute_readonly(
        _query(),
        preview_rows=10,
        strategy_id="S1_REPEAT_CACHE",
        execution_options={"cache_epoch": EPOCH},
    )
    assert second.served_from_cache is False
    assert len(session.executed) == 2
    assert len(datasets) == 2


def test_invalid_epoch_is_rejected_before_connecting() -> None:
    created: list[FakeSession] = []

    def factory(_: Settings) -> FakeSession:
        session = FakeSession({})
        created.append(session)
        return session

    adapter = _cache_adapter(FakeSession({}), factory=factory)
    result = adapter.execute_readonly(
        _query(),
        preview_rows=10,
        strategy_id="S1_REPEAT_CACHE",
        execution_options={"cache_epoch": "   "},
    )
    assert result.status.value == "ERROR"
    assert result.error_type == "InvalidCacheEpoch"
    assert result.request_dispatched is False
    assert any("CACHE_OPTION_INVALID" in warning for warning in result.warnings)
    assert created == []


def test_unsupported_execution_option_is_rejected_before_connecting() -> None:
    created: list[FakeSession] = []

    def factory(_: Settings) -> FakeSession:
        session = FakeSession({})
        created.append(session)
        return session

    adapter = _cache_adapter(FakeSession({}), factory=factory)
    with pytest.raises(ValueError, match="unsupported execution options"):
        adapter.execute_readonly(
            _query(),
            preview_rows=10,
            strategy_id="S1_REPEAT_CACHE",
            execution_options={"other_option": 1},
        )
    assert created == []
