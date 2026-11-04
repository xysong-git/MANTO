"""Agent 3: query/snapshot association and non-executing plan verification."""

from __future__ import annotations

import logging
import math
import time
from collections import deque
from typing import Literal, Sequence

from ...adapters.iotdb.tree_adapter import IoTDBTreeAdapter
from ...errors import AdapterError, StrategyRegistryError
from ...monitoring.plans import PlanParser, compare_plan_hashes, compare_plan_results
from ...monitoring.system import (
    SystemMonitor,
    SystemSample,
    SystemSnapshot,
    sample_snapshot_ids,
    sample_snapshots,
)
from ...schemas.common import utc_now
from ...schemas.execution import ExecutionResult, ExecutionStatus
from ...schemas.monitoring import (
    ExecutionRecord,
    PlanComparison,
    PlanKind,
    PlanParseResult,
    PlanParseStatus,
    PlanUnavailableReason,
    RecordUnavailableReason,
)
from ...strategies import StrategyRegistry, StrategyStatus, build_default_registry

logger = logging.getLogger(__name__)

DatabaseModel = Literal["tree", "table"]


class PerformanceMonitoringAgent:
    """Agent 3 with in-memory records and explicit missing-field markers.

    It can collect snapshots and parse EXPLAIN text supplied by an adapter.  It
    never calls ``EXPLAIN ANALYZE`` and does not execute queries by itself.
    """

    def __init__(
        self,
        *,
        adapter: IoTDBTreeAdapter | None = None,
        monitor: SystemMonitor | None = None,
        plan_parser: PlanParser | None = None,
        registry: StrategyRegistry | None = None,
        database_model: DatabaseModel = "tree",
        monitor_interval_s: float = 1.0,
        history_size: int = 200,
        snapshot_mode: str = "sample",
        latest_max_age_ms: int | None = None,
        latest_wait_timeout_s: float = 0.0,
    ):
        if history_size < 1:
            raise ValueError("history_size must be positive")
        if database_model not in {"tree", "table"}:
            raise ValueError("database_model must be 'tree' or 'table'")
        if snapshot_mode not in {"sample", "latest"}:
            raise ValueError("snapshot_mode must be sample or latest")
        if latest_max_age_ms is not None and (snapshot_mode != "latest" or latest_max_age_ms <= 0):
            raise ValueError("positive age budget requires latest snapshot mode")
        if not math.isfinite(latest_wait_timeout_s) or not 0 <= latest_wait_timeout_s <= 1:
            raise ValueError("snapshot wait must be bounded to at most one second")
        if latest_wait_timeout_s and latest_max_age_ms is None:
            raise ValueError("snapshot wait requires explicit age budget")
        self.latest_max_age_ms = latest_max_age_ms
        self.latest_wait_timeout_s = latest_wait_timeout_s
        self.snapshot_mode = snapshot_mode
        self.adapter = adapter
        self.monitor = monitor or SystemMonitor(interval_s=monitor_interval_s)
        self.plan_parser = plan_parser or PlanParser()
        self.registry = registry or build_default_registry()
        self.database_model: DatabaseModel = database_model
        self._records: deque[ExecutionRecord] = deque(maxlen=history_size)

    @property
    def is_monitoring(self) -> bool:
        return self.monitor.is_running

    def start(self) -> None:
        self.monitor.start()

    def stop(self, timeout: float = 2.0) -> None:
        self.monitor.stop(timeout=timeout)

    def sample_once(self) -> SystemSample:
        if self.snapshot_mode == "sample":
            return self.monitor.sample_once()
        deadline = time.monotonic() + self.latest_wait_timeout_s
        while True:
            if not self.monitor.is_running:
                raise RuntimeError("background monitor is not running")
            sample = self.monitor.latest_sample()
            if sample is not None:
                at = utc_now()
                if self.latest_max_age_ms is None or all(
                    item.is_usable(now=at) and item.is_fresh(now=at,max_age_ms=self.latest_max_age_ms)
                    for item in (sample.client_host,sample.db_host)
                ):
                    # Original timestamps/identity retained, never resample or
                    # relabel old data. Optional waiting is inside request E2E.
                    return sample.model_copy(deep=True)
            remaining = deadline-time.monotonic()
            if remaining <= 0:
                raise RuntimeError("background snapshot unavailable within age/deadline budget")
            time.sleep(min(.01,remaining))

    def latest_sample(self) -> SystemSample | None:
        return self.monitor.latest_sample()

    def history(self) -> tuple[SystemSample, ...]:
        return self.monitor.history()

    def records(self) -> tuple[ExecutionRecord, ...]:
        return tuple(self._records)

    def latest_record(
        self, *, query_id: str | None = None
    ) -> ExecutionRecord | None:
        if query_id is None:
            return self._records[-1] if self._records else None
        for record in reversed(self._records):
            if record.query_id == query_id:
                return record
        return None

    def plan_from_execution(self, result: ExecutionResult) -> PlanParseResult:
        """Parse the EXPLAIN text in an execution result; never execute again."""

        if result.status is ExecutionStatus.ERROR:
            return PlanParseResult(
                status=PlanParseStatus.ERROR,
                model=self.database_model,
                plan_kind=PlanKind.EXPLAIN,
                raw_line_count=result.row_count,
                error_type=result.error_type or "ExplainExecutionError",
                error_message=(result.error_message or "EXPLAIN execution failed")[
                    :4000
                ],
                warnings=list(result.warnings),
            )
        if result.preview_truncated:
            return PlanParseResult(
                status=PlanParseStatus.UNAVAILABLE,
                model=self.database_model,
                plan_kind=PlanKind.EXPLAIN,
                raw_line_count=result.row_count,
                unavailable_reason=PlanUnavailableReason.PLAN_PREVIEW_TRUNCATED,
                warnings=list(result.warnings)
                + [
                    "EXPLAIN preview was truncated; no partial plan hash was produced"
                ],
            )
        if result.row_count == 0:
            return PlanParseResult(
                status=PlanParseStatus.EMPTY,
                model=self.database_model,
                plan_kind=PlanKind.EXPLAIN,
                raw_line_count=0,
                warnings=list(result.warnings),
            )
        parsed = self.plan_parser.parse_rows(
            result.rows_preview,
            model=self.database_model,
            kind=PlanKind.EXPLAIN,
        )
        if result.warnings:
            parsed = parsed.model_copy(
                update={"warnings": _unique([*result.warnings, *parsed.warnings])}
            )
        return parsed

    def explain(
        self,
        sql: str,
        *,
        query_id: str | None = None,
        preview_rows: int = 1000,
    ) -> PlanParseResult:
        """Run only ``EXPLAIN`` through the adapter, then parse its text.

        The adapter validates the inner statement as a read-only SELECT and
        adds the ``EXPLAIN`` prefix.  This method has no analyze switch.
        """

        if self.adapter is None:
            raise AdapterError("no IoTDB adapter is configured for EXPLAIN")
        if preview_rows < 1:
            raise ValueError("preview_rows must be positive")
        result = self.adapter.execute_explain(
            sql,
            query_id=query_id,
            preview_rows=preview_rows,
        )
        return self.plan_from_execution(result)

    def compare(
        self,
        expected: PlanParseResult,
        actual: PlanParseResult,
    ) -> PlanComparison:
        return compare_plan_results(expected, actual)

    def compare_hashes(
        self,
        expected: PlanParseResult | str | None,
        actual: PlanParseResult | str | None,
    ) -> PlanComparison:
        return compare_plan_hashes(expected, actual)

    def record_execution(
        self,
        result: ExecutionResult,
        *,
        decision_id: str | None = None,
        plan_result: PlanParseResult | None = None,
        snapshots: SystemSample | Sequence[SystemSnapshot] | None = None,
        timeout_ms: int | None = None,
    ) -> ExecutionRecord:
        """Create one record linked by query/execution/snapshot IDs."""

        effective_timeout = self._effective_timeout(result, timeout_ms)
        attached, association_warnings = self._resolve_snapshots(
            snapshots, result
        )
        plan_status, plan_hash, plan_reason, plan_warnings = self._plan_fields(
            plan_result
        )

        warnings = _unique(
            [
                *result.warnings,
                *association_warnings,
                *plan_warnings,
            ]
        )
        if result.status is ExecutionStatus.SUCCESS and result.timed_out:
            warnings.append(
                "adapter reported a successful execution together with timed_out=true"
            )

        unavailable: dict[str, RecordUnavailableReason] = {}
        if decision_id is None:
            unavailable["decision_id"] = (
                RecordUnavailableReason.NO_DECISION_IN_PHASE_3
            )
        if effective_timeout is None:
            unavailable["timeout_ms"] = (
                RecordUnavailableReason.TIMEOUT_NOT_CONFIGURED
            )
        if plan_hash is None and plan_reason is not None:
            unavailable["actual_plan_hash"] = plan_reason
        snapshot_ids = _unique([snapshot.snapshot_id for snapshot in attached])
        if not snapshot_ids:
            unavailable["snapshot_ids"] = (
                RecordUnavailableReason.NO_SNAPSHOT_ATTACHED
            )

        record = ExecutionRecord(
            query_id=result.query_id,
            decision_id=decision_id,
            execution_id=result.execution_id,
            strategy_id=result.strategy_id,
            started_at=result.started_at,
            finished_at=result.finished_at,
            execute_call_ms=result.metrics.execute_call_ms,
            result_fetch_ms=result.metrics.result_fetch_ms,
            client_wall_ms=result.metrics.client_wall_ms,
            rows_returned=result.row_count,
            success=result.status is ExecutionStatus.SUCCESS,
            timed_out=result.timed_out,
            timeout_ms=effective_timeout,
            error_type=result.error_type,
            error_message=result.error_message,
            plan_status=plan_status,
            actual_plan_hash=plan_hash,
            strategy_certification_status=self._certification_status(
                result.strategy_id
            ),
            runtime_result_check="NOT_CHECKED",
            snapshot_ids=snapshot_ids,
            unavailable_fields=unavailable,
            warnings=warnings,
        )
        self._records.append(record)
        logger.info(
            "execution record created",
            extra={
                "event": "execution_record",
                "query_id": record.query_id,
                "decision_id": record.decision_id,
                "execution_id": record.execution_id,
                "plan_status": (
                    record.plan_status.value if record.plan_status is not None else None
                ),
                "snapshot_count": len(record.snapshot_ids),
            },
        )
        return record

    def _certification_status(
        self, strategy_id: str
    ) -> Literal["PASSED", "FAILED", "NOT_CERTIFIED"]:
        """Derive certification from the registry, never from execution success.

        Only a registered, enabled, ``VERIFIED`` strategy is ``PASSED``.
        Unregistered, disabled, or unverified strategies are ``NOT_CERTIFIED``;
        Phase 4 has no audit status that maps to ``FAILED``.
        """

        try:
            entry = self.registry.get_enabled(strategy_id)
        except StrategyRegistryError:
            return "NOT_CERTIFIED"
        if entry.spec.verification_status is StrategyStatus.VERIFIED:
            return "PASSED"
        return "NOT_CERTIFIED"

    @staticmethod
    def _effective_timeout(
        result: ExecutionResult, timeout_ms: int | None
    ) -> int | None:
        if (
            timeout_ms is not None
            and result.timeout_ms is not None
            and timeout_ms != result.timeout_ms
        ):
            raise ValueError(
                "explicit timeout_ms conflicts with the execution result timeout_ms"
            )
        return timeout_ms if timeout_ms is not None else result.timeout_ms

    def _resolve_snapshots(
        self,
        snapshots: SystemSample | Sequence[SystemSnapshot] | None,
        result: ExecutionResult,
    ) -> tuple[list[SystemSnapshot], list[str]]:
        warnings: list[str] = []
        if isinstance(snapshots, SystemSample):
            return sample_snapshots(snapshots), warnings
        if snapshots is not None:
            attached = list(snapshots)
            for snapshot in attached:
                if not isinstance(snapshot, SystemSnapshot):
                    raise TypeError("snapshots must contain SystemSnapshot values")
            if not attached:
                warnings.append("no system snapshot was attached to this execution")
            return attached, warnings

        sample = self.monitor.latest_sample()
        if sample is None:
            sample = self.monitor.sample_once()
        reference = result.finished_at or utc_now()
        if not sample.client_host.is_fresh(now=reference):
            warnings.append(
                "the latest client-host system snapshot was stale at "
                "execution completion"
            )
        if sample.db_host.valid and not sample.db_host.is_fresh(now=reference):
            warnings.append(
                "the latest DB-host system snapshot was stale at execution "
                "completion"
            )
        if (
            sample.db_metrics is not None
            and sample.db_metrics.valid
            and not sample.db_metrics.is_fresh(now=reference)
        ):
            warnings.append(
                "the latest DB-metrics snapshot was stale at execution completion"
            )
        return sample_snapshots(sample), warnings

    def _plan_fields(
        self, plan_result: PlanParseResult | None
    ) -> tuple[
        PlanParseStatus,
        str | None,
        RecordUnavailableReason | None,
        list[str],
    ]:
        if plan_result is None:
            return (
                PlanParseStatus.UNAVAILABLE,
                None,
                RecordUnavailableReason.PLAN_UNAVAILABLE,
                ["no EXPLAIN plan was supplied for this execution"],
            )
        if not isinstance(plan_result, PlanParseResult):
            raise TypeError("plan_result must be a PlanParseResult")

        if plan_result.status is PlanParseStatus.AVAILABLE:
            if plan_result.signature_hash is None:
                raise ValueError("available plan has no stable hash")
            return (
                PlanParseStatus.AVAILABLE,
                plan_result.signature_hash,
                None,
                list(plan_result.warnings),
            )
        reason = (
            RecordUnavailableReason.PLAN_EMPTY
            if plan_result.status is PlanParseStatus.EMPTY
            else RecordUnavailableReason.PLAN_UNAVAILABLE
        )
        warnings = list(plan_result.warnings)
        if plan_result.error_message:
            warnings.append(
                f"plan parse error {plan_result.error_type}: "
                f"{plan_result.error_message}"
            )
        return plan_result.status, None, reason, warnings


def _unique(values: Sequence[str]) -> list[str]:
    seen: set[str] = set()
    result: list[str] = []
    for value in values:
        if value not in seen:
            seen.add(value)
            result.append(value)
    return result
