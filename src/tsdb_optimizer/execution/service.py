"""Decision -> persisted audit records -> one executor run.

This is the Phase-4 library pipeline, not the Phase-6 four-agent Orchestrator.
It performs no learning, does not start monitoring threads, and never calls
EXPLAIN automatically.
"""

from __future__ import annotations

import logging
import time
from uuid import uuid4

from ..adapters.base import TSDBAdapter
from ..agents import QueryAnalysisAgent
from ..decision.cost import CostEstimator
from ..decision.engine import DecisionEngine
from ..decision.models import AdaptiveAdvice, DecisionMode
from ..schemas import QueryRequest
from ..schemas.monitoring import SystemSample
from ..sql_guard import sql_sha256, validate_readonly_sql
from ..storage import SQLiteStore
from ..request_memo import request_scope, execution_store_session
from ..strategies import StrategyRegistry, build_default_registry
from .executor import StrategyExecutor
from .models import OptimizationRun

logger = logging.getLogger(__name__)


class DecisionExecutionService:
    """Persist a decision before executing exactly one selected candidate."""

    def __init__(
        self,
        *,
        adapter: TSDBAdapter,
        store: SQLiteStore,
        registry: StrategyRegistry | None = None,
        engine: DecisionEngine | None = None,
        analyzer: QueryAnalysisAgent | None = None,
        cost_estimator: CostEstimator | None = None,
        preview_rows: int = 20,
        server_version: str | None = None,
        database_model: str | None = None,
    ):
        if preview_rows < 1:
            raise ValueError("preview_rows must be positive")
        self.adapter = adapter
        self.store = store
        self.registry = registry or (
            engine.registry if engine is not None else build_default_registry()
        )
        self.analyzer = analyzer or QueryAnalysisAgent()
        self.engine = engine or DecisionEngine(
            self.registry,
            cost_estimator=cost_estimator,
            analyzer=self.analyzer,
        )
        if self.engine.registry is not self.registry:
            raise ValueError("decision engine and service must share one registry")
        self.executor = StrategyExecutor(adapter=adapter, registry=self.registry)
        self.preview_rows = preview_rows
        self.server_version = server_version
        self.database_model = database_model

    @request_scope
    @execution_store_session
    def run(
        self,
        sql: str,
        *,
        mode: DecisionMode | str = DecisionMode.RULE_BASED,
        source: str = "cli:optimize",
        timeout_ms: int | None = None,
        preview_rows: int | None = None,
        system_state: SystemSample | None = None,
        adaptive_advice: AdaptiveAdvice | None = None,
        server_version: str | None = None,
        database_model: str | None = None,
    ) -> OptimizationRun:
        started = time.perf_counter()
        effective_preview = (
            self.preview_rows if preview_rows is None else preview_rows
        )
        if effective_preview < 1:
            raise ValueError("preview_rows must be positive")
        if timeout_ms is not None and timeout_ms < 0:
            raise ValueError("timeout_ms must be non-negative")

        cleaned = validate_readonly_sql(sql)
        request = QueryRequest(
            query_id=uuid4().hex,
            sql=cleaned,
            source=source,
            timeout_ms=timeout_ms,
        )
        # Historical cost lookup reads the SQLite store, so the schema must be
        # ready before decision-making.
        self.store.initialize()
        analysis = self.analyzer.analyze(cleaned, query_id=request.query_id)
        effective_server_version = (
            self.server_version if server_version is None else server_version
        )
        effective_database_model = (
            self.database_model if database_model is None else database_model
        )
        decision = self.engine.decide(
            query_id=request.query_id,
            original_sql=cleaned,
            mode=mode,
            analysis=analysis,
            system_state=system_state,
            server_version=effective_server_version,
            database_model=effective_database_model,
            adaptive_advice=adaptive_advice,
        )
        self.store.record_query(request, sql_sha256(cleaned))
        self.store.record_decision(decision)
        # If the executor raises before returning a report, the decision remains
        # auditable in SQLite.  No execution row is invented.
        report = self.executor.execute(
            decision,
            preview_rows=effective_preview,
            timeout_ms=timeout_ms,
            analysis=analysis,
            system_state=system_state,
            server_version=effective_server_version,
            database_model=effective_database_model,
        )
        for attempt in report.attempts:
            try:
                self.store.record_execution(
                    attempt.execution,
                    decision_id=decision.decision_id,
                    attempt_index=attempt.attempt_index,
                    is_fallback=attempt.is_fallback,
                    requested_strategy_id=attempt.requested_strategy_id,
                )
            except Exception as exc:  # noqa: BLE001 - keep the completed result
                # The database already executed the query.  A local audit-write
                # failure must not be reported as an execution failure, but it
                # must be visible in the returned report and the logs.
                logger.exception(
                    "execution persistence failed",
                    extra={
                        "event": "execution_persistence_error",
                        "query_id": decision.query_id,
                        "decision_id": decision.decision_id,
                        "execution_id": attempt.execution.execution_id,
                        "error_type": type(exc).__name__,
                    },
                )
                report.warnings.append(
                    "EXECUTION_PERSISTENCE_FAILED: "
                    f"attempt {attempt.attempt_index}: "
                    f"{type(exc).__name__}: {exc}"
                )
        end_to_end_ms = round((time.perf_counter() - started) * 1000, 3)
        report = report.model_copy(update={"end_to_end_ms": end_to_end_ms})
        try:
            self.store.record_execution_report(report)
        except Exception as exc:  # noqa: BLE001 - completed execution is authoritative
            # The query already completed.  A local audit-write failure must be
            # visible without changing the execution outcome.
            logger.exception(
                "execution report persistence failed",
                extra={
                    "event": "report_persistence_error",
                    "query_id": decision.query_id,
                    "decision_id": decision.decision_id,
                    "error_type": type(exc).__name__,
                },
            )
            report.warnings.append(
                "REPORT_PERSISTENCE_FAILED: "
                f"{type(exc).__name__}: {exc}"
            )
        logger.info(
            "decision executed",
            extra={
                "event": "decision_execution",
                "query_id": decision.query_id,
                "decision_id": decision.decision_id,
                "execution_id": report.attempts[-1].execution.execution_id,
                "strategy_id": decision.selected_strategy_id,
                "attempt_count": len(report.attempts),
                "fallback_used": report.fallback_used,
                "final_status": report.final_status.value,
                "client_wall_ms": report.attempts[-1].execution.metrics.client_wall_ms,
                "total_wall_ms": report.total_wall_ms,
                "end_to_end_ms": report.end_to_end_ms,
            },
        )
        return OptimizationRun(decision=decision, execution=report)


__all__ = ["DecisionExecutionService"]
