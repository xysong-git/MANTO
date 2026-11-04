"""Cost-evidence interfaces for Agent 2.

Phase 4 does not train or load a latency model.  The default estimator returns
``UNKNOWN``; the optional historical estimator reports a median only when a
minimum number of real, successful, non-fallback executions exists.
"""

from __future__ import annotations

import logging
import sqlite3
import statistics
from typing import Protocol

from ..sql_guard import sql_sha256
from ..storage import SQLiteStore
from .models import CostEstimate, CostSource

logger = logging.getLogger(__name__)


class CostEstimator(Protocol):
    def estimate(self, *, strategy_id: str, candidate_sql: str) -> CostEstimate:
        """Return cost evidence or an explicit unknown reason."""


class UnknownCostEstimator:
    """Always unknown; safe when no historical evidence is available."""

    def __init__(self, reason: str = "NO_HISTORICAL_STATISTICS"):
        self.reason = reason

    def estimate(self, *, strategy_id: str, candidate_sql: str) -> CostEstimate:
        del strategy_id, candidate_sql
        return CostEstimate(
            source=CostSource.UNKNOWN,
            sample_count=0,
            unknown_reason=self.reason,
        )


class HistoricalCostEstimator:
    """Median of recent successful, non-fallback executions.

    Matching is by exact candidate SQL SHA-256 and strategy ID, so one strategy's
    history cannot be used to justify another.  The result has no confidence
    score; introducing one would require a model and validation evidence that
    Phase 4 does not have.
    """

    def __init__(
        self,
        store: SQLiteStore,
        *,
        min_samples: int = 3,
        window: int = 20,
    ):
        if min_samples < 1:
            raise ValueError("min_samples must be positive")
        if window < min_samples:
            raise ValueError("window must be at least min_samples")
        self.store = store
        self.min_samples = min_samples
        self.window = window

    def estimate(self, *, strategy_id: str, candidate_sql: str) -> CostEstimate:
        digest = sql_sha256(candidate_sql)
        try:
            latencies = self.store.recent_successful_latencies(
                sql_sha256=digest,
                strategy_id=strategy_id,
                limit=self.window,
            )
        except sqlite3.Error as exc:
            logger.warning(
                "historical cost lookup failed",
                extra={
                    "event": "cost_history_error",
                    "strategy_id": strategy_id,
                    "error_type": type(exc).__name__,
                },
            )
            return CostEstimate(
                source=CostSource.UNKNOWN,
                sample_count=0,
                unknown_reason=f"HISTORY_UNAVAILABLE: {type(exc).__name__}",
            )
        usable = [value for value in latencies if value >= 0.0]
        if len(usable) < self.min_samples:
            return CostEstimate(
                source=CostSource.UNKNOWN,
                sample_count=len(usable),
                unknown_reason=(
                    f"INSUFFICIENT_HISTORY: {len(usable)} < {self.min_samples}"
                ),
            )
        return CostEstimate(
            source=CostSource.HISTORICAL_STATISTIC,
            expected_latency_ms=round(statistics.median(usable), 3),
            sample_count=len(usable),
            confidence=None,
        )


__all__ = [
    "CostEstimator",
    "HistoricalCostEstimator",
    "UnknownCostEstimator",
]
