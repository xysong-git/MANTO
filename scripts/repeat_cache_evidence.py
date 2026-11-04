#!/usr/bin/env python
"""Read-only evidence for S1_REPEAT_CACHE against the real IoTDB instance.

This script never writes to IoTDB.  It prints JSON only; the password is never
printed.  Usage:

    python scripts/repeat_cache_evidence.py

Requirements: a reachable Apache IoTDB 2.0.6 tree-model server and a valid
``.env`` (or process environment) with IOTDB_PASSWORD.
"""

from __future__ import annotations

import json
import statistics
from pathlib import Path
from typing import Any

from tsdb_optimizer.adapters.iotdb.tree_adapter import IoTDBTreeAdapter
from tsdb_optimizer.config import load_settings
from tsdb_optimizer.decision import DecisionMode
from tsdb_optimizer.execution import DecisionExecutionService
from tsdb_optimizer.schemas import QueryRequest
from tsdb_optimizer.schemas.execution import ExecutionStatus
from tsdb_optimizer.sql_guard import validate_readonly_sql
from tsdb_optimizer.storage import SQLiteStore
from tsdb_optimizer.strategies import build_default_registry

EPOCH = "evidence-epoch-2026-11-01"
PROJECT_ROOT = Path(__file__).resolve().parents[1]
NATIVE_RUNS = 30
CACHE_RUNS = 30
SERVICE_RUNS = 50
PREVIEW_ROWS = 1000


def _first_workload_sql(relative: str) -> str:
    path = PROJECT_ROOT / "query" / relative
    for line in path.read_text(encoding="utf-8").splitlines():
        candidate = line.strip()
        if candidate and not candidate.startswith("--"):
            if candidate.upper().startswith("SELECT"):
                return validate_readonly_sql(candidate)
    raise RuntimeError(f"no SELECT statement found in {path}")


def _median(values: list[float]) -> float:
    return round(statistics.median(values), 3)


def _percentile_sorted(ordered: list[float], fraction: float) -> float:
    """Linear-interpolated percentile over an already sorted sample."""

    if not ordered:
        raise ValueError("percentile requires at least one value")
    if len(ordered) == 1:
        return ordered[0]
    position = (len(ordered) - 1) * fraction
    lower = int(position)
    upper = min(lower + 1, len(ordered) - 1)
    if lower == upper:
        return ordered[lower]
    weight = position - lower
    return ordered[lower] * (1.0 - weight) + ordered[upper] * weight


def _stats(values: list[float]) -> dict[str, Any]:
    """Return the §8.2 summary: mean, quantiles, variance and extremes."""

    if not values:
        return {
            "n": 0,
            "mean": None,
            "median": None,
            "p50": None,
            "p95": None,
            "p99": None,
            "stdev": None,
            "variance": None,
            "min": None,
            "max": None,
        }
    ordered = sorted(values)
    count = len(ordered)
    return {
        "n": count,
        "mean": round(statistics.mean(ordered), 3),
        "median": round(statistics.median(ordered), 3),
        "p50": round(_percentile_sorted(ordered, 0.50), 3),
        "p95": round(_percentile_sorted(ordered, 0.95), 3),
        "p99": round(_percentile_sorted(ordered, 0.99), 3),
        "stdev": round(statistics.stdev(ordered), 3) if count > 1 else None,
        "variance": round(statistics.variance(ordered), 3) if count > 1 else None,
        "min": round(ordered[0], 3),
        "max": round(ordered[-1], 3),
    }


def _timings(result: Any) -> dict[str, float]:
    return {
        "execute_call_ms": result.metrics.execute_call_ms,
        "result_fetch_ms": result.metrics.result_fetch_ms,
        "client_wall_ms": result.metrics.client_wall_ms,
    }


def _direct_evidence(adapter: IoTDBTreeAdapter, sql: str) -> dict[str, Any]:
    native_results = [
        adapter.execute_readonly(
            QueryRequest(sql=sql, source="evidence-native"),
            preview_rows=PREVIEW_ROWS,
        )
        for _ in range(NATIVE_RUNS)
    ]
    cache_results = [
        adapter.execute_readonly(
            QueryRequest(sql=sql, source="evidence-cache"),
            preview_rows=PREVIEW_ROWS,
            strategy_id="S1_REPEAT_CACHE",
            execution_options={"cache_epoch": EPOCH},
        )
        for _ in range(CACHE_RUNS)
    ]
    for result in native_results + cache_results:
        if result.status is not ExecutionStatus.SUCCESS:
            raise RuntimeError(
                f"real execution failed: {result.error_type}: {result.error_message}"
            )
    reference = native_results[0]
    for result in cache_results:
        if (
            result.row_count != reference.row_count
            or result.rows_preview != reference.rows_preview
            or [(c.name, c.data_type) for c in result.columns]
            != [(c.name, c.data_type) for c in reference.columns]
        ):
            raise RuntimeError("cache/real result mismatch; refusing to report benefit")

    native_wall = [r.metrics.client_wall_ms for r in native_results]
    miss_wall = [cache_results[0].metrics.client_wall_ms]
    hit_wall = [r.metrics.client_wall_ms for r in cache_results[1:]]
    return {
        "sql_sha256": reference.sql_sha256,
        "columns": len(reference.columns),
        "row_count": reference.row_count,
        "native": {
            "runs": len(native_results),
            "server_dispatches": sum(1 for r in native_results if r.request_dispatched),
            "client_wall_ms": native_wall,
            "client_wall_stats": _stats(native_wall),
            "median_client_wall_ms": _median(native_wall),
            "first_timings": _timings(native_results[0]),
        },
        "cache": {
            "runs": len(cache_results),
            "hits": sum(1 for r in cache_results if r.served_from_cache),
            "server_dispatches": sum(
                1 for r in cache_results if r.request_dispatched
            ),
            "miss_client_wall_ms": miss_wall,
            "hit_client_wall_ms": hit_wall,
            "miss_client_wall_stats": _stats(miss_wall),
            "hit_client_wall_stats": _stats(hit_wall),
            "median_hit_client_wall_ms": _median(hit_wall),
            "hit_execute_call_ms": sorted(
                {r.metrics.execute_call_ms for r in cache_results[1:]}
            ),
            "first_miss_timings": _timings(cache_results[0]),
            "hit_timings": _timings(cache_results[1]),
            "result_equal_to_native": True,
        },
        "speedup_on_hit_vs_native_median": round(
            _median(native_wall) / _median(hit_wall), 2
        )
        if _median(hit_wall) > 0
        else None,
    }


def _service_evidence(
    store_path: Path, sql: str, *, enable_cache: bool
) -> dict[str, Any]:
    settings = load_settings()
    adapter = IoTDBTreeAdapter(settings)
    store = SQLiteStore(store_path)
    try:
        report = adapter.doctor()
        if not report.ok or report.server is None:
            raise RuntimeError(
                f"doctor failed: {report.error_type}: {report.error_message}"
            )
        if report.server.version != "2.0.6" or report.server.model != "tree":
            raise RuntimeError(
                f"expected certified 2.0.6 tree server, got "
                f"{report.server.version!r}/{report.server.model!r}"
            )
        registry = build_default_registry(cache_epoch=EPOCH if enable_cache else None)
        service = DecisionExecutionService(
            adapter=adapter,
            store=store,
            registry=registry,
            preview_rows=PREVIEW_ROWS,
            server_version=report.server.version,
            database_model=report.server.model,
        )
        runs = []
        for _ in range(SERVICE_RUNS):
            run = service.run(
                sql,
                mode=DecisionMode.RULE_BASED,
                source="evidence-service",
                timeout_ms=30_000,
                preview_rows=PREVIEW_ROWS,
            )
            attempt = run.execution.attempts[0]
            runs.append(
                {
                    "selected_strategy_id": run.decision.selected_strategy_id,
                    "query_occurrence": run.decision.query_occurrence,
                    "decision_overhead_ms": run.decision.decision_overhead_ms,
                    "end_to_end_ms": run.execution.end_to_end_ms,
                    # Derived, not a separately measured timer: everything in
                    # end-to-end that is not the adapter's client wall time
                    # (decision, validation, persistence, logging and harness).
                    "non_client_overhead_ms": round(
                        run.execution.end_to_end_ms
                        - attempt.execution.metrics.client_wall_ms,
                        3,
                    ),
                    "fallback_used": run.execution.fallback_used,
                    "final_status": run.execution.final_status.value,
                    "served_from_cache": attempt.execution.served_from_cache,
                    "request_dispatched": attempt.execution.request_dispatched,
                    "cache_source_execution_id": (
                        attempt.execution.cache_source_execution_id
                    ),
                    "row_count": attempt.execution.row_count,
                    "client_wall_ms": attempt.execution.metrics.client_wall_ms,
                }
            )
        hit_runs = [run for run in runs if run["served_from_cache"]]
        dispatches = sum(1 for run in runs if run["request_dispatched"])
        expected_dispatches = 2 if enable_cache else SERVICE_RUNS
        if dispatches != expected_dispatches:
            raise RuntimeError(
                f"unexpected real dispatch count: {dispatches} != "
                f"{expected_dispatches}"
            )
        if enable_cache and not hit_runs:
            raise RuntimeError("the real service produced no cache hit")
        if not enable_cache and hit_runs:
            raise RuntimeError("the no-epoch service unexpectedly produced a hit")
        return {
            "server_version": report.server.version,
            "database_model": report.server.model,
            "cache_enabled": enable_cache,
            "runs": runs,
            "summary": {
                "runs": len(runs),
                "server_dispatches": dispatches,
                "cache_hits": len(hit_runs),
                "first_run_end_to_end_ms": runs[0]["end_to_end_ms"],
                "second_run_end_to_end_ms": runs[1]["end_to_end_ms"],
                "end_to_end_stats": _stats(
                    [run["end_to_end_ms"] for run in runs]
                ),
                "hit_end_to_end_stats": _stats(
                    [run["end_to_end_ms"] for run in hit_runs]
                ),
                "decision_overhead_stats": _stats(
                    [run["decision_overhead_ms"] for run in runs]
                ),
                "non_client_overhead_stats": _stats(
                    [run["non_client_overhead_ms"] for run in runs]
                ),
                "hit_non_client_overhead_stats": _stats(
                    [run["non_client_overhead_ms"] for run in hit_runs]
                ),
                "client_wall_stats": _stats(
                    [run["client_wall_ms"] for run in runs]
                ),
                "hit_client_wall_stats": _stats(
                    [run["client_wall_ms"] for run in hit_runs]
                ),
                "median_end_to_end_ms": _median(
                    [run["end_to_end_ms"] for run in runs]
                ),
                "median_hit_end_to_end_ms": _median(
                    [run["end_to_end_ms"] for run in hit_runs]
                )
                if hit_runs
                else None,
                "median_decision_overhead_ms": _median(
                    [run["decision_overhead_ms"] for run in runs]
                ),
                "median_non_client_overhead_ms": _median(
                    [run["non_client_overhead_ms"] for run in runs]
                ),
                "median_hit_non_client_overhead_ms": _median(
                    [run["non_client_overhead_ms"] for run in hit_runs]
                )
                if hit_runs
                else None,
                "median_hit_client_wall_ms": _median(
                    [run["client_wall_ms"] for run in hit_runs]
                )
                if hit_runs
                else None,
                "median_client_wall_ms": _median(
                    [run["client_wall_ms"] for run in runs]
                ),
            },
            "sqlite": {
                "queries": store.count("queries"),
                "decisions": store.count("decisions"),
                "executions": store.count("executions"),
            },
        }
    finally:
        adapter.close()


def main() -> int:
    settings = load_settings()
    adapter = IoTDBTreeAdapter(settings)
    try:
        report = adapter.doctor()
        if not report.ok or report.server is None:
            raise RuntimeError(
                f"doctor failed: {report.error_type}: {report.error_message}"
            )
        if report.server.version != "2.0.6" or report.server.model != "tree":
            raise RuntimeError(
                f"expected certified 2.0.6 tree server, got "
                f"{report.server.version!r}/{report.server.model!r}"
            )
        evidence: dict[str, Any] = {
            "server_version": report.server.version,
            "database_model": report.server.model,
            "cache_epoch": EPOCH,
            "workloads": {},
        }
        for name, relative in (
            ("q8", "queries_dbs/q8.sql"),
            ("q7", "queries_dbs/q7.sql"),
            ("q11", "queries_dbs/q11.sql"),
        ):
            sql = _first_workload_sql(relative)
            evidence["workloads"][name] = _direct_evidence(adapter, sql)
    finally:
        adapter.close()

    import tempfile

    with tempfile.TemporaryDirectory() as directory:
        q8_sql = _first_workload_sql("queries_dbs/q8.sql")
        evidence["service_q8_with_cache"] = _service_evidence(
            Path(directory) / "service-cache.sqlite3",
            q8_sql,
            enable_cache=True,
        )
        evidence["service_q8_native_only"] = _service_evidence(
            Path(directory) / "service-native.sqlite3",
            q8_sql,
            enable_cache=False,
        )
    print(json.dumps(evidence, indent=2, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
