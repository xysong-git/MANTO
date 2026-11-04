"""Cost evidence must be real history or explicitly unknown."""

from __future__ import annotations

import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from tsdb_optimizer.decision import (
    CostSource,
    HistoricalCostEstimator,
    UnknownCostEstimator,
)
from tsdb_optimizer.schemas import QueryRequest, RetryUnsafeReason
from tsdb_optimizer.sql_guard import sql_sha256
from tsdb_optimizer.storage import SQLiteStore
from phase4_support import TEST_SQL, execution_result


def _store(tmp_path: Path) -> SQLiteStore:
    store = SQLiteStore(tmp_path / "cost.sqlite3")
    store.initialize()
    return store


def _seed(
    store: SQLiteStore,
    *,
    latencies: list[float],
    strategy_id: str = "S0_NATIVE",
    sql: str = TEST_SQL,
    executed_sql: str | None = None,
    fallback: bool = False,
    status: str = "SUCCESS",
) -> None:
    request = QueryRequest(sql=sql, source="pytest-cost")
    store.record_query(request, sql_sha256(sql))
    effective_executed_sql = sql if executed_sql is None else executed_sql
    base = datetime(2026, 11, 1, tzinfo=timezone.utc)
    for index, latency in enumerate(latencies):
        if status == "SUCCESS":
            result = execution_result(
                query_id=request.query_id,
                strategy_id=strategy_id,
                sql=effective_executed_sql,
                client_wall_ms=latency,
                started_at=base + timedelta(seconds=index),
            )
        else:
            result = execution_result(
                query_id=request.query_id,
                strategy_id=strategy_id,
                sql=effective_executed_sql,
                status="ERROR",
                retry_unsafe_reason=RetryUnsafeReason.REQUEST_COMPLETION_UNCONFIRMED,
                client_wall_ms=latency,
                started_at=base + timedelta(seconds=index),
            )
        store.record_execution(
            result,
            attempt_index=1,
            is_fallback=fallback,
        )


def test_unknown_estimator_is_explicit() -> None:
    estimate = UnknownCostEstimator().estimate(
        strategy_id="S0_NATIVE",
        candidate_sql=TEST_SQL,
    )
    assert estimate.source is CostSource.UNKNOWN
    assert estimate.expected_latency_ms is None
    assert estimate.confidence is None
    assert estimate.sample_count == 0
    assert estimate.unknown_reason == "NO_HISTORICAL_STATISTICS"


def test_insufficient_history_is_unknown(tmp_path: Path) -> None:
    store = _store(tmp_path)
    _seed(store, latencies=[10.0, 20.0])
    estimate = HistoricalCostEstimator(store, min_samples=3).estimate(
        strategy_id="S0_NATIVE",
        candidate_sql=TEST_SQL,
    )
    assert estimate.source is CostSource.UNKNOWN
    assert estimate.sample_count == 2
    assert estimate.expected_latency_ms is None
    assert "INSUFFICIENT_HISTORY" in (estimate.unknown_reason or "")


def test_median_is_reported_after_minimum_real_samples(tmp_path: Path) -> None:
    store = _store(tmp_path)
    _seed(store, latencies=[10.0, 30.0, 20.0])
    estimate = HistoricalCostEstimator(store, min_samples=3).estimate(
        strategy_id="S0_NATIVE",
        candidate_sql=TEST_SQL,
    )
    assert estimate.source is CostSource.HISTORICAL_STATISTIC
    assert estimate.expected_latency_ms == 20.0
    assert estimate.sample_count == 3
    assert estimate.confidence is None
    assert estimate.unknown_reason is None


def test_history_matching_is_scoped_to_sql_and_strategy(tmp_path: Path) -> None:
    store = _store(tmp_path)
    _seed(store, latencies=[10.0, 20.0, 30.0])
    estimator = HistoricalCostEstimator(store, min_samples=3)

    other_strategy = estimator.estimate(
        strategy_id="TEST_ONLY_FAST",
        candidate_sql=TEST_SQL,
    )
    assert other_strategy.source is CostSource.UNKNOWN
    assert other_strategy.sample_count == 0

    other_sql = estimator.estimate(
        strategy_id="S0_NATIVE",
        candidate_sql=f"{TEST_SQL} LIMIT 5",
    )
    assert other_sql.source is CostSource.UNKNOWN
    assert other_sql.sample_count == 0


def test_history_matching_uses_executed_candidate_sql(tmp_path: Path) -> None:
    store = _store(tmp_path)
    candidate_sql = f"{TEST_SQL} LIMIT 5"
    _seed(store, latencies=[10.0, 20.0, 30.0], executed_sql=candidate_sql)
    estimator = HistoricalCostEstimator(store, min_samples=3)

    candidate = estimator.estimate(
        strategy_id="S0_NATIVE",
        candidate_sql=candidate_sql,
    )
    assert candidate.source is CostSource.HISTORICAL_STATISTIC
    assert candidate.expected_latency_ms == 20.0

    original = estimator.estimate(
        strategy_id="S0_NATIVE",
        candidate_sql=TEST_SQL,
    )
    assert original.source is CostSource.UNKNOWN
    assert original.sample_count == 0


def test_failed_and_fallback_executions_do_not_supply_cost(tmp_path: Path) -> None:
    store = _store(tmp_path)
    _seed(store, latencies=[999.0], fallback=True)
    _seed(store, latencies=[888.0], status="ERROR")
    estimate = HistoricalCostEstimator(store, min_samples=1).estimate(
        strategy_id="S0_NATIVE",
        candidate_sql=TEST_SQL,
    )
    assert estimate.source is CostSource.UNKNOWN
    assert estimate.sample_count == 0


def test_history_lookup_failure_is_unknown_with_reason(
    tmp_path: Path, monkeypatch
) -> None:
    store = _store(tmp_path)

    def boom(**kwargs):
        del kwargs
        raise sqlite3.OperationalError("history table unavailable")

    monkeypatch.setattr(store, "recent_successful_latencies", boom)
    estimate = HistoricalCostEstimator(store, min_samples=1).estimate(
        strategy_id="S0_NATIVE",
        candidate_sql=TEST_SQL,
    )
    assert estimate.source is CostSource.UNKNOWN
    assert estimate.unknown_reason == "HISTORY_UNAVAILABLE: OperationalError"


def test_historical_estimator_rejects_invalid_parameters(tmp_path: Path) -> None:
    store = _store(tmp_path)
    with pytest.raises(ValueError, match="min_samples"):
        HistoricalCostEstimator(store, min_samples=0)
    with pytest.raises(ValueError, match="window"):
        HistoricalCostEstimator(store, min_samples=5, window=4)
