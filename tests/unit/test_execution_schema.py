"""ExecutionResult invariants: UTC evidence and contradictory success states."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest
from pydantic import ValidationError

from tsdb_optimizer.schemas import ExecutionResult, QueryRequest
from tsdb_optimizer.schemas.execution import (
    ExecutionStatus,
    FetchMode,
    RetryUnsafeReason,
)


def _result(**updates: object) -> ExecutionResult:
    started = datetime(2026, 11, 1, 12, 0, tzinfo=timezone.utc)
    values: dict[str, object] = {
        "query_id": "q-execution-schema",
        "execution_id": "e-execution-schema",
        "status": ExecutionStatus.SUCCESS,
        "sql_sha256": "a" * 64,
        "started_at": started,
        "finished_at": started + timedelta(milliseconds=3),
        "request_dispatched": True,
        "request_completed": True,
    }
    values.update(updates)
    return ExecutionResult(**values)


def test_success_requires_dispatch_and_completion() -> None:
    with pytest.raises(ValidationError, match="must have been dispatched"):
        _result(request_dispatched=False, request_completed=False)
    with pytest.raises(ValidationError, match="must have completed"):
        _result(request_completed=False)


def test_success_cannot_be_timed_out() -> None:
    with pytest.raises(ValidationError, match="cannot be timed out"):
        _result(
            timed_out=True,
            retry_unsafe_reason=RetryUnsafeReason.TIMEOUT_OR_UNKNOWN_SERVER_STATE,
        )


def test_error_may_record_a_timeout_without_claiming_success() -> None:
    result = _result(
        status=ExecutionStatus.ERROR,
        request_completed=False,
        timed_out=True,
        retry_unsafe_reason=RetryUnsafeReason.TIMEOUT_OR_UNKNOWN_SERVER_STATE,
    )
    assert result.status is ExecutionStatus.ERROR
    assert result.timed_out is True


def test_timestamps_reject_naive_values() -> None:
    with pytest.raises(ValidationError, match="timezone-aware UTC"):
        _result(started_at=datetime(2026, 11, 1, 12, 0))


def test_timestamps_are_normalized_to_utc() -> None:
    plus_two = timezone(timedelta(hours=2))
    started = datetime(2026, 11, 1, 14, 0, tzinfo=plus_two)
    result = _result(
        started_at=started,
        finished_at=started + timedelta(milliseconds=1),
    )
    assert result.started_at == datetime(2026, 11, 1, 12, 0, tzinfo=timezone.utc)
    assert result.started_at.tzinfo is timezone.utc


def test_finished_at_must_not_precede_started_at() -> None:
    started = datetime(2026, 11, 1, 12, 0, tzinfo=timezone.utc)
    with pytest.raises(ValidationError, match="finished_at must not precede"):
        _result(
            started_at=started,
            finished_at=started - timedelta(milliseconds=1),
        )


def test_execution_record_carries_a_protocol_version() -> None:
    assert _result().schema_version == 1


def test_sql_hash_must_be_lowercase_sha256_hex() -> None:
    with pytest.raises(ValidationError, match="String should match pattern"):
        _result(sql_sha256="A" * 64)
    with pytest.raises(ValidationError, match="String should match pattern"):
        _result(sql_sha256="a" * 63)


def test_success_cannot_carry_error_fields() -> None:
    with pytest.raises(ValidationError, match="cannot carry an error"):
        _result(error_type="RuntimeError")
    with pytest.raises(ValidationError, match="cannot carry an error"):
        _result(error_message="failed")


def test_fetch_mode_is_explicit_and_defaults_to_full() -> None:
    assert _result().fetch_mode is FetchMode.FULL
    assert _result(fetch_mode=FetchMode.LIMITED).fetch_mode is FetchMode.LIMITED
    with pytest.raises(ValidationError, match="Input should be"):
        _result(fetch_mode="PARTIAL")


def test_cache_hit_requires_no_dispatch_and_names_its_source() -> None:
    hit = _result(
        served_from_cache=True,
        request_dispatched=False,
        request_completed=False,
        cache_source_execution_id="source-execution",
        cache_epoch="epoch-2026-11-01",
        row_count=1,
        rows_preview=[[1]],
        preview_truncated=False,
    )
    assert hit.served_from_cache is True
    assert hit.request_dispatched is False
    assert hit.request_completed is False


@pytest.mark.parametrize(
    "updates",
    [
        {
            "served_from_cache": True,
            "cache_epoch": "epoch-2026-11-01",
            "request_dispatched": False,
            "request_completed": False,
            "row_count": 1,
            "rows_preview": [[1]],
        },
        {
            "served_from_cache": True,
            "cache_source_execution_id": "source-execution",
            "request_dispatched": False,
            "request_completed": False,
            "row_count": 1,
            "rows_preview": [[1]],
        },
        {
            "served_from_cache": True,
            "cache_source_execution_id": "source-execution",
            "cache_epoch": "   ",
            "request_dispatched": False,
            "request_completed": False,
            "row_count": 1,
            "rows_preview": [[1]],
        },
        {
            "served_from_cache": True,
            "cache_source_execution_id": "source-execution",
            "cache_epoch": "epoch-2026-11-01",
            "row_count": 1,
            "rows_preview": [[1]],
            "request_dispatched": True,
            "request_completed": True,
        },
        {
            "served_from_cache": True,
            "cache_source_execution_id": "e-execution-schema",
            "cache_epoch": "epoch-2026-11-01",
            "row_count": 1,
            "rows_preview": [[1]],
            "preview_truncated": False,
            "request_dispatched": False,
            "request_completed": False,
        },
        {
            "served_from_cache": True,
            "cache_source_execution_id": "source-execution",
            "cache_epoch": "epoch-2026-11-01",
            "row_count": 2,
            "rows_preview": [[1]],
            "preview_truncated": False,
            "request_dispatched": False,
            "request_completed": False,
        },
    ],
)
def test_invalid_cache_hit_states_are_rejected(updates: dict[str, object]) -> None:
    with pytest.raises(ValidationError):
        _result(**updates)


def test_non_cache_execution_must_not_name_a_cache_source() -> None:
    with pytest.raises(ValidationError, match="only a cache hit"):
        _result(cache_source_execution_id="source-execution")


def test_cache_miss_may_record_its_epoch_without_a_source() -> None:
    miss = _result(cache_epoch="epoch-2026-11-01")
    assert miss.served_from_cache is False
    assert miss.cache_source_execution_id is None
    assert miss.cache_epoch == "epoch-2026-11-01"


def test_query_request_rejects_naive_timestamps_and_normalizes_offsets() -> None:
    with pytest.raises(ValidationError, match="timezone-aware UTC"):
        QueryRequest(sql="SELECT 1", requested_at=datetime(2026, 11, 1, 12, 0))

    plus_two = timezone(timedelta(hours=2))
    request = QueryRequest(
        sql="SELECT 1",
        requested_at=datetime(2026, 11, 1, 14, 0, tzinfo=plus_two),
    )
    assert request.requested_at == datetime(
        2026, 11, 1, 12, 0, tzinfo=timezone.utc
    )
    assert request.schema_version == 1
