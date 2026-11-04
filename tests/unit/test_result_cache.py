"""Bounded process-local repeat-result cache: identity, limits and LRU."""

from __future__ import annotations

import math
from datetime import datetime, timezone

import pytest
from pydantic import ValidationError

from tsdb_optimizer.adapters.iotdb.result_cache import (
    CachePutResult,
    ResultCache,
    estimate_row_bytes,
    result_cache_key,
)
from tsdb_optimizer.schemas.common import ColumnMeta

EPOCH = "epoch-2026-11-01"
SQL_HASH = "a" * 64
STRATEGY = "S1_REPEAT_CACHE"


def _columns() -> list[ColumnMeta]:
    return [ColumnMeta(name="Time", data_type="INT64")]


def _stored_at() -> datetime:
    return datetime(2026, 11, 1, 12, 0, tzinfo=timezone.utc)


def _put(
    cache: ResultCache,
    *,
    rows: list[list[object]] | None = None,
    epoch: str = EPOCH,
    sql_sha256: str = SQL_HASH,
    strategy_id: str = STRATEGY,
    source_execution_id: str = "source-execution",
) -> CachePutResult:
    return cache.put(
        cache_epoch=epoch,
        sql_sha256=sql_sha256,
        strategy_id=strategy_id,
        columns=_columns(),
        rows=rows if rows is not None else [[1], [2]],
        source_execution_id=source_execution_id,
        stored_at=_stored_at(),
    )


def test_cache_key_is_deterministic_and_identity_scoped() -> None:
    base = result_cache_key(
        cache_epoch=EPOCH, sql_sha256=SQL_HASH, strategy_id=STRATEGY
    )
    assert base == result_cache_key(
        cache_epoch=EPOCH, sql_sha256=SQL_HASH, strategy_id=STRATEGY
    )
    assert base != result_cache_key(
        cache_epoch="epoch-other", sql_sha256=SQL_HASH, strategy_id=STRATEGY
    )
    assert base != result_cache_key(
        cache_epoch=EPOCH, sql_sha256="b" * 64, strategy_id=STRATEGY
    )
    assert base != result_cache_key(
        cache_epoch=EPOCH, sql_sha256=SQL_HASH, strategy_id="S9_OTHER"
    )
    assert len(base) == 64


def test_cache_key_rejects_invalid_identity() -> None:
    with pytest.raises(ValueError, match="non-blank"):
        result_cache_key(cache_epoch="  ", sql_sha256=SQL_HASH, strategy_id=STRATEGY)
    with pytest.raises(ValueError, match="lowercase SHA-256"):
        result_cache_key(cache_epoch=EPOCH, sql_sha256="A" * 64, strategy_id=STRATEGY)
    with pytest.raises(ValueError, match="lowercase SHA-256"):
        result_cache_key(cache_epoch=EPOCH, sql_sha256="a" * 63, strategy_id=STRATEGY)
    with pytest.raises(ValueError, match="non-blank"):
        result_cache_key(cache_epoch=EPOCH, sql_sha256=SQL_HASH, strategy_id="")


def test_put_and_get_store_the_complete_result_once() -> None:
    cache = ResultCache()
    put_result = _put(cache)
    assert put_result.stored is True
    assert put_result.row_count == 2
    assert put_result.size_bytes > 0

    key = result_cache_key(
        cache_epoch=EPOCH, sql_sha256=SQL_HASH, strategy_id=STRATEGY
    )
    entry = cache.get(key)
    assert entry is not None
    assert entry.rows == ((1,), (2,))
    assert entry.row_count == 2
    assert entry.cache_epoch == EPOCH
    assert entry.sql_sha256 == SQL_HASH
    assert entry.strategy_id == STRATEGY
    assert entry.source_execution_id == "source-execution"
    assert entry.columns == tuple(_columns())
    assert cache.hits == 1
    assert cache.misses == 0
    assert cache.entries == 1
    assert cache.total_rows == 2


def test_put_copies_columns_and_frozen_metadata_cannot_be_mutated() -> None:
    columns = _columns()
    cache = ResultCache()
    assert (
        cache.put(
            cache_epoch=EPOCH,
            sql_sha256=SQL_HASH,
            strategy_id=STRATEGY,
            columns=columns,
            rows=[[1]],
            source_execution_id="source-execution",
            stored_at=_stored_at(),
        ).stored
        is True
    )
    key = result_cache_key(
        cache_epoch=EPOCH, sql_sha256=SQL_HASH, strategy_id=STRATEGY
    )

    with pytest.raises(ValidationError):
        columns[0].name = "MUTATED"  # type: ignore[misc]

    # Bypass Pydantic's frozen guard to simulate a hostile caller that still
    # holds the original object.  The cache must have copied it at put time.
    object.__setattr__(columns[0], "name", "MUTATED")
    entry = cache.get(key)
    assert entry is not None
    assert tuple(entry.columns) == tuple(_columns())

    with pytest.raises(ValidationError):
        entry.columns[0].name = "MUTATED"  # type: ignore[misc]


def test_cache_preserves_empty_result() -> None:
    cache = ResultCache()
    assert _put(cache, rows=[]).stored is True
    entry = cache.get(
        result_cache_key(
            cache_epoch=EPOCH, sql_sha256=SQL_HASH, strategy_id=STRATEGY
        )
    )
    assert entry is not None
    assert entry.rows == ()
    assert entry.row_count == 0


def test_cache_preserves_boundary_values_duplicate_timestamps_and_order() -> None:
    timestamp_min = -(2**63)
    timestamp_max = 2**63 - 1
    rows = [
        [timestamp_min, None, float("nan")],
        [timestamp_min, None, float("inf")],
        [timestamp_max, 1, 0.0],
    ]
    cache = ResultCache()
    assert _put(cache, rows=rows).stored is True
    entry = cache.get(
        result_cache_key(
            cache_epoch=EPOCH, sql_sha256=SQL_HASH, strategy_id=STRATEGY
        )
    )
    assert entry is not None
    assert entry.rows[0][0] == timestamp_min
    assert entry.rows[1][0] == timestamp_min
    assert entry.rows[0][1] is None
    assert entry.rows[1][1] is None
    assert math.isnan(entry.rows[0][2])
    assert entry.rows[1][2] == float("inf")
    assert entry.rows[2][0] == timestamp_max
    assert entry.rows[2][2] == 0.0
    assert [row[0] for row in entry.rows] == [
        timestamp_min,
        timestamp_min,
        timestamp_max,
    ]


def test_put_rejects_naive_stored_at() -> None:
    cache = ResultCache()
    with pytest.raises(ValueError, match="timezone-aware UTC"):
        cache.put(
            cache_epoch=EPOCH,
            sql_sha256=SQL_HASH,
            strategy_id=STRATEGY,
            columns=_columns(),
            rows=[[1]],
            source_execution_id="source-execution",
            stored_at=datetime(2026, 11, 1, 12, 0),
        )


def test_get_copies_nothing_from_the_caller_and_is_immutable() -> None:
    rows = [[1], [2]]
    cache = ResultCache()
    assert _put(cache, rows=rows).stored is True
    rows[0][0] = 999  # caller mutation after put must not change the cache
    assert cache.get(
        result_cache_key(
            cache_epoch=EPOCH, sql_sha256=SQL_HASH, strategy_id=STRATEGY
        )
    ).rows == ((1,), (2,))
    entry = cache.get(
        result_cache_key(
            cache_epoch=EPOCH, sql_sha256=SQL_HASH, strategy_id=STRATEGY
        )
    )
    assert entry is not None
    with pytest.raises(Exception):
        entry.rows[0] = (3,)  # frozen dataclass/tuple: cannot rewrite an entry


def test_miss_is_counted_and_returns_none() -> None:
    cache = ResultCache()
    key = result_cache_key(
        cache_epoch=EPOCH, sql_sha256=SQL_HASH, strategy_id=STRATEGY
    )
    assert cache.get(key) is None
    assert cache.misses == 1
    assert cache.hits == 0
    assert cache.entries == 0


def test_lru_eviction_respects_max_entries_and_get_refreshes_order() -> None:
    cache = ResultCache(max_entries=2)
    for index in range(2):
        assert _put(
            cache,
            sql_sha256=str(index) * 64,
            source_execution_id=f"source-{index}",
        ).stored
    first_key = result_cache_key(
        cache_epoch=EPOCH, sql_sha256="0" * 64, strategy_id=STRATEGY
    )
    second_key = result_cache_key(
        cache_epoch=EPOCH, sql_sha256="1" * 64, strategy_id=STRATEGY
    )
    # Touch the oldest entry so the second becomes the LRU victim.
    assert cache.get(first_key) is not None
    assert _put(
        cache,
        sql_sha256="2" * 64,
        source_execution_id="source-2",
    ).stored

    assert cache.entries == 2
    assert cache.get(second_key) is None
    assert cache.get(first_key) is not None
    assert cache.evictions == 1


def test_result_larger_than_entry_row_limit_is_rejected_not_truncated() -> None:
    cache = ResultCache(max_entry_rows=1)
    result = _put(cache, rows=[[1], [2]])
    assert result.stored is False
    assert "max_entry_rows" in result.reason
    assert cache.entries == 0
    assert cache.total_rows == 0


def test_result_larger_than_entry_byte_limit_is_rejected() -> None:
    cache = ResultCache(max_entry_bytes=300, max_total_bytes=1000)
    result = _put(cache, rows=[["x" * 1000]])
    assert result.stored is False
    assert "max_entry_bytes" in result.reason
    assert cache.entries == 0


def test_aggregate_row_limit_evicts_the_oldest_entry() -> None:
    cache = ResultCache(max_total_rows=3, max_entry_rows=2)
    assert _put(cache, sql_sha256="0" * 64, rows=[[1], [2]]).stored
    assert _put(cache, sql_sha256="1" * 64, rows=[[3], [4]]).stored

    assert cache.entries == 1
    assert cache.total_rows == 2
    assert cache.evictions == 1
    assert cache.get(
        result_cache_key(
            cache_epoch=EPOCH, sql_sha256="0" * 64, strategy_id=STRATEGY
        )
    ) is None
    assert cache.get(
        result_cache_key(
            cache_epoch=EPOCH, sql_sha256="1" * 64, strategy_id=STRATEGY
        )
    ) is not None


def test_replacing_the_same_key_updates_the_accounting() -> None:
    cache = ResultCache()
    assert _put(cache, rows=[[1]]).stored
    assert _put(cache, rows=[[1], [2], [3]]).stored
    assert cache.entries == 1
    assert cache.total_rows == 3
    key = result_cache_key(
        cache_epoch=EPOCH, sql_sha256=SQL_HASH, strategy_id=STRATEGY
    )
    assert cache.get(key).rows == ((1,), (2,), (3,))


def test_epoch_and_strategy_are_part_of_the_identity() -> None:
    cache = ResultCache()
    assert _put(cache).stored
    other_epoch_key = result_cache_key(
        cache_epoch="epoch-other", sql_sha256=SQL_HASH, strategy_id=STRATEGY
    )
    other_strategy_key = result_cache_key(
        cache_epoch=EPOCH, sql_sha256=SQL_HASH, strategy_id="S9_OTHER"
    )
    assert cache.get(other_epoch_key) is None
    assert cache.get(other_strategy_key) is None
    assert cache.entries == 1


def test_clear_drops_entries_but_keeps_counters() -> None:
    cache = ResultCache()
    assert _put(cache).stored
    key = result_cache_key(
        cache_epoch=EPOCH, sql_sha256=SQL_HASH, strategy_id=STRATEGY
    )
    assert cache.get(key) is not None
    cache.clear()
    assert cache.entries == 0
    assert cache.total_rows == 0
    assert cache.total_bytes == 0
    assert cache.hits == 1
    assert cache.get(key) is None


def test_invalid_limits_are_rejected() -> None:
    with pytest.raises(ValueError, match="positive integer"):
        ResultCache(max_entries=0)
    with pytest.raises(ValueError, match="max_entry_rows"):
        ResultCache(max_total_rows=1, max_entry_rows=2)
    with pytest.raises(ValueError, match="max_entry_bytes"):
        ResultCache(max_total_bytes=100, max_entry_bytes=101)


def test_put_requires_a_source_execution() -> None:
    cache = ResultCache()
    with pytest.raises(ValueError, match="source_execution_id"):
        cache.put(
            cache_epoch=EPOCH,
            sql_sha256=SQL_HASH,
            strategy_id=STRATEGY,
            columns=_columns(),
            rows=[[1]],
            source_execution_id="",
            stored_at=_stored_at(),
        )


def test_estimate_row_bytes_is_deterministic_and_type_sensitive() -> None:
    assert estimate_row_bytes([None]) == 8
    assert estimate_row_bytes([True]) == 8
    assert estimate_row_bytes([1]) == 16
    assert estimate_row_bytes([1.5]) == 16
    assert estimate_row_bytes(["abc"]) == 27
    assert estimate_row_bytes([b"abc"]) == 27
    assert estimate_row_bytes(["abcdef"]) == 30
