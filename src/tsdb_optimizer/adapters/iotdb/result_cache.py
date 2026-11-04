"""Process-local exact-SQL result cache for the S1_REPEAT_CACHE strategy.

This cache is deliberately small in scope:

* it is process-local and disappears when the process exits;
* the key is the normalized caller-declared dataset epoch, the cleaned SQL
  SHA-256 and the strategy id;
* it stores the complete result only when the whole result fits explicit row
  and byte limits;
* it has no server-side invalidation protocol.  A caller that cannot declare an
  immutable dataset epoch must not enable it.

The adapter is the only writer.  A hit is served without opening or using an
IoTDB session, which is why a cache entry records the real execution id that
originally populated it.
"""

from __future__ import annotations

import hashlib
from collections import OrderedDict
from dataclasses import dataclass
from datetime import datetime
from threading import RLock
from typing import Any, Sequence

from ...schemas.common import ColumnMeta, ensure_utc

CACHE_KEY_SCHEMA = "repeat-cache-v1"


def result_cache_key(
    *,
    cache_epoch: str,
    sql_sha256: str,
    strategy_id: str,
) -> str:
    """Return the opaque cache key for one exact execution identity."""

    if not isinstance(cache_epoch, str) or not cache_epoch.strip():
        raise ValueError("cache_epoch must be a non-blank string")
    if (
        not isinstance(sql_sha256, str)
        or len(sql_sha256) != 64
        or any(character not in "0123456789abcdef" for character in sql_sha256)
    ):
        raise ValueError("sql_sha256 must be a lowercase SHA-256 hex digest")
    if not isinstance(strategy_id, str) or not strategy_id.strip():
        raise ValueError("strategy_id must be a non-blank string")
    material = "\x1f".join(
        (CACHE_KEY_SCHEMA, cache_epoch, strategy_id, sql_sha256)
    )
    return hashlib.sha256(material.encode("utf-8")).hexdigest()


def estimate_row_bytes(row: Sequence[Any]) -> int:
    """Return a deterministic estimate used only for cache admission."""

    total = 0
    for value in row:
        if value is None or isinstance(value, bool):
            total += 8
        elif isinstance(value, (int, float)):
            total += 16
        elif isinstance(value, str):
            total += 24 + len(value.encode("utf-8"))
        elif isinstance(value, (bytes, bytearray)):
            total += 24 + len(value)
        else:
            total += 24 + len(repr(value).encode("utf-8", errors="replace"))
    return total


@dataclass(frozen=True)
class CachedResult:
    """One complete, immutable result set stored by the adapter."""

    cache_key: str
    cache_epoch: str
    sql_sha256: str
    strategy_id: str
    columns: tuple[ColumnMeta, ...]
    rows: tuple[tuple[Any, ...], ...]
    source_execution_id: str
    stored_at: datetime
    size_bytes: int

    @property
    def row_count(self) -> int:
        return len(self.rows)


@dataclass(frozen=True)
class CachePutResult:
    stored: bool
    reason: str
    row_count: int = 0
    size_bytes: int = 0


class ResultCache:
    """A bounded LRU cache of complete query results.

    ``get`` returns an immutable :class:`CachedResult`.  Rows are stored as
    tuples and ``ColumnMeta`` is frozen, so callers cannot mutate an entry
    through an alias.  The cache holds at most ``max_entries`` entries and
    explicit aggregate row/byte
    limits.  An entry larger than ``max_entry_rows`` or ``max_entry_bytes`` is
    rejected rather than truncated: a partial result would be semantically
    wrong.
    """

    def __init__(
        self,
        *,
        max_entries: int = 128,
        max_total_rows: int = 100_000,
        max_total_bytes: int = 64 * 1024 * 1024,
        max_entry_rows: int = 20_000,
        max_entry_bytes: int = 16 * 1024 * 1024,
    ):
        for name, value in (
            ("max_entries", max_entries),
            ("max_total_rows", max_total_rows),
            ("max_total_bytes", max_total_bytes),
            ("max_entry_rows", max_entry_rows),
            ("max_entry_bytes", max_entry_bytes),
        ):
            if not isinstance(value, int) or value < 1:
                raise ValueError(f"{name} must be a positive integer")
        if max_entry_rows > max_total_rows:
            raise ValueError("max_entry_rows must not exceed max_total_rows")
        if max_entry_bytes > max_total_bytes:
            raise ValueError("max_entry_bytes must not exceed max_total_bytes")
        self.max_entries = max_entries
        self.max_total_rows = max_total_rows
        self.max_total_bytes = max_total_bytes
        self.max_entry_rows = max_entry_rows
        self.max_entry_bytes = max_entry_bytes
        self._entries: OrderedDict[str, CachedResult] = OrderedDict()
        self._total_rows = 0
        self._total_bytes = 0
        self._hits = 0
        self._misses = 0
        self._evictions = 0
        self._lock = RLock()

    def get(self, cache_key: str) -> CachedResult | None:
        if not isinstance(cache_key, str) or not cache_key:
            raise ValueError("cache_key must be a non-blank string")
        with self._lock:
            entry = self._entries.pop(cache_key, None)
            if entry is None:
                self._misses += 1
                return None
            self._entries[cache_key] = entry
            self._hits += 1
            return entry

    def put(
        self,
        *,
        cache_epoch: str,
        sql_sha256: str,
        strategy_id: str,
        columns: Sequence[ColumnMeta],
        rows: Sequence[Sequence[Any]],
        source_execution_id: str,
        stored_at: datetime,
    ) -> CachePutResult:
        """Store a complete result, or reject it without truncation."""

        if not isinstance(source_execution_id, str) or not source_execution_id:
            raise ValueError("source_execution_id must be a non-blank string")
        stored_at = ensure_utc(stored_at)
        assert stored_at is not None
        # Copy the metadata even though ColumnMeta is frozen.  This keeps the
        # cache independent of any future caller-side container mutation and
        # makes the immutability contract explicit at the cache boundary.
        column_tuple = tuple(
            ColumnMeta(name=column.name, data_type=column.data_type)
            for column in columns
        )
        row_tuple = tuple(tuple(row) for row in rows)
        if len(row_tuple) > self.max_entry_rows:
            return CachePutResult(
                stored=False,
                reason=(
                    f"result has {len(row_tuple)} rows, exceeding the "
                    f"max_entry_rows limit of {self.max_entry_rows}"
                ),
                row_count=len(row_tuple),
            )
        data_bytes = sum(estimate_row_bytes(row) for row in row_tuple)
        column_bytes = sum(
            len(column.name.encode("utf-8"))
            + len((column.data_type or "").encode("utf-8"))
            + 32
            for column in column_tuple
        )
        # Hash/epoch/strategy/source ids and Python object overhead are small,
        # but include a fixed per-entry allowance so the byte limit is not
        # misleadingly tight.
        size_bytes = data_bytes + column_bytes + 256
        if size_bytes > self.max_entry_bytes:
            return CachePutResult(
                stored=False,
                reason=(
                    f"estimated result size {size_bytes} bytes exceeds the "
                    f"max_entry_bytes limit of {self.max_entry_bytes}"
                ),
                row_count=len(row_tuple),
                size_bytes=size_bytes,
            )
        key = result_cache_key(
            cache_epoch=cache_epoch,
            sql_sha256=sql_sha256,
            strategy_id=strategy_id,
        )
        entry = CachedResult(
            cache_key=key,
            cache_epoch=cache_epoch,
            sql_sha256=sql_sha256,
            strategy_id=strategy_id,
            columns=column_tuple,
            rows=row_tuple,
            source_execution_id=source_execution_id,
            stored_at=stored_at,
            size_bytes=size_bytes,
        )
        with self._lock:
            previous = self._entries.pop(key, None)
            if previous is not None:
                self._total_rows -= previous.row_count
                self._total_bytes -= previous.size_bytes
            self._entries[key] = entry
            self._total_rows += entry.row_count
            self._total_bytes += entry.size_bytes
            while self._entries and (
                len(self._entries) > self.max_entries
                or self._total_rows > self.max_total_rows
                or self._total_bytes > self.max_total_bytes
            ):
                evicted_key, evicted = self._entries.popitem(last=False)
                del evicted_key
                self._total_rows -= evicted.row_count
                self._total_bytes -= evicted.size_bytes
                self._evictions += 1
            if key not in self._entries:
                return CachePutResult(
                    stored=False,
                    reason=(
                        "the new entry was evicted immediately because the "
                        "aggregate cache limits are smaller than one entry"
                    ),
                    row_count=len(row_tuple),
                    size_bytes=size_bytes,
                )
        return CachePutResult(
            stored=True,
            reason="complete result stored under the declared dataset epoch",
            row_count=len(row_tuple),
            size_bytes=size_bytes,
        )

    def clear(self) -> None:
        """Drop all entries; counters remain available for observation."""

        with self._lock:
            self._entries.clear()
            self._total_rows = 0
            self._total_bytes = 0

    @property
    def hits(self) -> int:
        with self._lock:
            return self._hits

    @property
    def misses(self) -> int:
        with self._lock:
            return self._misses

    @property
    def evictions(self) -> int:
        with self._lock:
            return self._evictions

    @property
    def entries(self) -> int:
        with self._lock:
            return len(self._entries)

    @property
    def total_rows(self) -> int:
        with self._lock:
            return self._total_rows

    @property
    def total_bytes(self) -> int:
        with self._lock:
            return self._total_bytes


__all__ = [
    "CACHE_KEY_SCHEMA",
    "CachePutResult",
    "CachedResult",
    "ResultCache",
    "estimate_row_bytes",
    "result_cache_key",
]
