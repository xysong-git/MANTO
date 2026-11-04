"""TEST-ONLY strategy and adapter doubles for Phase-4 unit tests.

Nothing here is imported by production code, registered in the default
registry, or evidence that a non-Native strategy is verified.  The doubles
exist only to exercise registry, decision, executor and fallback code paths.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from uuid import uuid4

from tsdb_optimizer.adapters.base import TSDBAdapter
from tsdb_optimizer.decision.models import CostEstimate, CostSource
from tsdb_optimizer.schemas import (
    DoctorReport,
    ExecutionResult,
    QueryRequest,
    RetryUnsafeReason,
    TimingMetrics,
)
from tsdb_optimizer.schemas.execution import ExecutionStatus
from tsdb_optimizer.sql_guard import sql_sha256, validate_readonly_sql
from tsdb_optimizer.storage import SQLiteStore
from tsdb_optimizer.strategies import (
    ApplicabilityResult,
    CandidateContext,
    CandidatePlan,
    StrategySpec,
    StrategyStatus,
    StrategyValidator,
    ValidationCheck,
    ValidationCheckStatus,
    ValidationContext,
    ValidationResult,
)

TEST_ONLY_STRATEGY_ID = "TEST_ONLY_FAST"
TEST_SQL = "SELECT s_130 FROM root.test.g_0.d_0"
PARSER_UNSUPPORTED_SQL = (
    "SELECT last s_1666, s_1766 FROM root.db800.g_0.d_0, root.db800.g_0.d_1"
)


def make_test_only_spec(
    strategy_id: str = TEST_ONLY_STRATEGY_ID,
    *,
    enabled: bool = True,
    verification_status: StrategyStatus = StrategyStatus.VERIFIED,
    is_baseline: bool = False,
    version: str = "0.0.1",
    plan_effect_expectation: str | None = None,
    allowed_execution_options: Iterable[str] = (),
) -> StrategySpec:
    return StrategySpec(
        strategy_id=strategy_id,
        name="TEST ONLY strategy double - not a production strategy",
        database_model="tree",
        compatible_server_versions=["2.0.6"],
        enabled=enabled,
        verification_status=verification_status,
        is_baseline=is_baseline,
        implementation_class="tests.unit.phase4_support.PassThroughStrategyDouble",
        applicability_rules={"test_only": True},
        execution_control="no production use",
        semantic_test_suite="tests/unit",
        plan_effect_expectation=plan_effect_expectation,
        allowed_execution_options=list(allowed_execution_options),
        known_limitations=["TEST ONLY", "not verified by the Phase-0 audit"],
        version=version,
    )


@dataclass
class PassThroughStrategyDouble:
    """Configurable plugin double; every behavior is explicit in tests."""

    spec: StrategySpec
    applicable: bool = True
    applicability_warning: str | None = None
    candidate_sql: str | None = None
    candidate_version: str | None = None
    candidate_options: dict[str, str | int | float | bool] = field(
        default_factory=dict
    )
    expected_plan_effect: str | None = None
    build_error: Exception | None = None
    validate_error: Exception | None = None
    applicability_error: Exception | None = None

    def __post_init__(self) -> None:
        self.validator = StrategyValidator()

    def is_applicable(
        self,
        query_features: Any,
        system_state: Any,
    ) -> ApplicabilityResult:
        del query_features, system_state
        if self.applicability_error is not None:
            raise self.applicability_error
        warnings = (
            [self.applicability_warning] if self.applicability_warning else []
        )
        return ApplicabilityResult(
            strategy_id=self.spec.strategy_id,
            applicable=self.applicable,
            reasons=(
                ["test-only double: forced applicable"]
                if self.applicable
                else ["test-only double: forced not applicable"]
            ),
            warnings=warnings,
        )

    def build_candidate(
        self,
        original_sql: str,
        context: CandidateContext,
    ) -> CandidatePlan:
        del context
        if self.build_error is not None:
            raise self.build_error
        cleaned = validate_readonly_sql(original_sql)
        return CandidatePlan(
            strategy_id=self.spec.strategy_id,
            strategy_version=self.candidate_version or self.spec.version,
            candidate_sql=(
                self.candidate_sql
                if self.candidate_sql is not None
                else cleaned
            ),
            execution_options=dict(self.candidate_options),
            expected_plan_effect=self.expected_plan_effect,
        )

    def validate(
        self,
        original_sql: str,
        candidate: CandidatePlan,
        context: ValidationContext,
    ) -> ValidationResult:
        if self.validate_error is not None:
            raise self.validate_error
        return self.validator.validate(self.spec, original_sql, candidate, context)


@dataclass
class SelfApprovingStrategyDouble:
    """TEST ONLY: fabricates PASSED without calling the central validator.

    This double exists to prove that Decision Engine and Executor enforce the
    registry-owned :class:`StrategyValidator` even when a plugin claims its own
    candidate is valid.  It is not a production strategy and is never
    registered by ``build_default_registry``.
    """

    spec: StrategySpec
    candidate_sql: str | None = None
    candidate_options: dict[str, str | int | float | bool] = field(
        default_factory=dict
    )

    def is_applicable(
        self,
        query_features: Any,
        system_state: Any,
    ) -> ApplicabilityResult:
        del query_features, system_state
        return ApplicabilityResult(
            strategy_id=self.spec.strategy_id,
            applicable=True,
            reasons=["test-only double: self-approving plugin"],
        )

    def build_candidate(
        self,
        original_sql: str,
        context: CandidateContext,
    ) -> CandidatePlan:
        del context
        cleaned = validate_readonly_sql(original_sql)
        return CandidatePlan(
            strategy_id=self.spec.strategy_id,
            strategy_version=self.spec.version,
            candidate_sql=(
                self.candidate_sql if self.candidate_sql is not None else cleaned
            ),
            execution_options=dict(self.candidate_options),
        )

    def validate(
        self,
        original_sql: str,
        candidate: CandidatePlan,
        context: ValidationContext,
    ) -> ValidationResult:
        del original_sql, candidate, context
        return ValidationResult.from_checks(
            [
                ValidationCheck(
                    name="self_approved",
                    status=ValidationCheckStatus.PASSED,
                    detail=(
                        "test-only plugin approved itself without the central "
                        "StrategyValidator"
                    ),
                )
            ]
        )


class StaticCostEstimator:
    """Cost estimator returning explicit per-strategy test evidence."""

    def __init__(
        self,
        latency_by_strategy: dict[str, float],
        *,
        unknown_reason: str = "TEST_ONLY_NO_HISTORY",
    ):
        self.latency_by_strategy = latency_by_strategy
        self.unknown_reason = unknown_reason

    def estimate(self, *, strategy_id: str, candidate_sql: str) -> CostEstimate:
        del candidate_sql
        latency = self.latency_by_strategy.get(strategy_id)
        if latency is None:
            return CostEstimate(
                source=CostSource.UNKNOWN,
                sample_count=0,
                unknown_reason=self.unknown_reason,
            )
        return CostEstimate(
            source=CostSource.HISTORICAL_STATISTIC,
            expected_latency_ms=latency,
            sample_count=3,
            confidence=None,
        )


class RaisingCostEstimator:
    def estimate(self, *, strategy_id: str, candidate_sql: str) -> CostEstimate:
        del strategy_id, candidate_sql
        raise RuntimeError("cost estimator exploded")


Outcome = (
    ExecutionResult
    | Exception
    | Callable[[QueryRequest, str], ExecutionResult]
)


class ScriptedAdapter(TSDBAdapter):
    """Return scripted outcomes and record every execution call."""

    def __init__(self, outcomes: Iterable[Outcome]):
        self.outcomes = list(outcomes)
        self.calls: list[tuple[QueryRequest, int, str]] = []
        self.execution_options: list[dict[str, Any]] = []

    def connect(self) -> None:
        return None

    def close(self) -> None:
        return None

    def doctor(self) -> DoctorReport:
        raise NotImplementedError("ScriptedAdapter has no doctor")

    def execute_readonly(
        self,
        request: QueryRequest,
        preview_rows: int = 20,
        *,
        strategy_id: str = "S0_NATIVE",
        execution_options: dict[str, str | int | float | bool] | None = None,
    ) -> ExecutionResult:
        self.calls.append((request, preview_rows, strategy_id))
        self.execution_options.append(dict(execution_options or {}))
        if not self.outcomes:
            raise AssertionError("ScriptedAdapter received more calls than scripted")
        outcome = self.outcomes.pop(0)
        if isinstance(outcome, Exception):
            raise outcome
        if callable(outcome):
            return outcome(request, strategy_id)
        return outcome


def execution_result(
    *,
    query_id: str,
    strategy_id: str = "S0_NATIVE",
    status: ExecutionStatus = ExecutionStatus.SUCCESS,
    sql: str = TEST_SQL,
    timed_out: bool = False,
    request_dispatched: bool = True,
    request_completed: bool | None = None,
    retry_safe: bool = False,
    retry_unsafe_reason: RetryUnsafeReason | None = None,
    timeout_ms: int | None = None,
    client_wall_ms: float = 3.0,
    row_count: int = 1,
    started_at: datetime | None = None,
    served_from_cache: bool = False,
    cache_source_execution_id: str | None = None,
    cache_epoch: str | None = None,
    rows_preview: list[list[Any]] | None = None,
) -> ExecutionResult:
    started = started_at or datetime.now(timezone.utc)
    completed = (
        status is ExecutionStatus.SUCCESS
        if request_completed is None
        else request_completed
    )
    preview = list(rows_preview or [])
    return ExecutionResult(
        query_id=query_id,
        execution_id=uuid4().hex,
        status=status,
        sql_sha256=sql_sha256(sql),
        strategy_id=strategy_id,
        adapter="scripted-test",
        row_count=row_count,
        rows_preview=preview,
        preview_truncated=row_count > len(preview),
        served_from_cache=served_from_cache,
        cache_source_execution_id=cache_source_execution_id,
        cache_epoch=cache_epoch,
        metrics=TimingMetrics(
            execute_call_ms=1.0,
            result_fetch_ms=1.0,
            client_wall_ms=client_wall_ms,
        ),
        started_at=started,
        finished_at=started + timedelta(milliseconds=client_wall_ms),
        timeout_ms=timeout_ms,
        timed_out=timed_out,
        request_dispatched=request_dispatched,
        request_completed=completed,
        retry_safe=retry_safe,
        retry_unsafe_reason=retry_unsafe_reason,
    )


def test_store(tmp_path: Path) -> SQLiteStore:
    store = SQLiteStore(tmp_path / "phase4-test.sqlite3")
    store.initialize()
    return store
