"""Execute exactly one decision candidate, with safe Native fallback.

The executor re-derives the candidate from the registered plugin and validates
it again before dispatching.  A decision therefore cannot smuggle arbitrary SQL
or execution options past the registry.  It never runs EXPLAIN/ANALYZE and never
retries a request whose completion is unknown.
"""

from __future__ import annotations

import time
from dataclasses import dataclass

from ..adapters.base import TSDBAdapter
from ..decision.models import OptimizationDecision
from ..errors import AdapterError, ExecutionError, StrategyRegistryError
from ..schemas import QueryRequest
from ..schemas.common import utc_now
from ..schemas.execution import ExecutionResult, ExecutionStatus
from ..schemas.monitoring import SystemSample
from ..schemas.query_features import QueryAnalysisResult
from ..sql_guard import sql_sha256
from ..strategies import (
    CandidateContext,
    CandidatePlan,
    RegisteredStrategy,
    StrategyRegistry,
    ValidationContext,
    ValidationResult,
)
from .fallback import FallbackPolicy
from .models import (
    ExecutionAttempt,
    ExecutionReport,
    FallbackDecision,
    FallbackTrigger,
)


@dataclass
class _Preflight:
    entry: RegisteredStrategy | None
    candidate: CandidatePlan | None
    validation: ValidationResult | None
    error: str | None


class StrategyExecutor:
    def __init__(
        self,
        *,
        adapter: TSDBAdapter,
        registry: StrategyRegistry,
        fallback_policy: FallbackPolicy | None = None,
    ):
        self.adapter = adapter
        self.registry = registry
        self.fallback_policy = fallback_policy or FallbackPolicy()

    def execute(
        self,
        decision: OptimizationDecision,
        *,
        preview_rows: int = 20,
        timeout_ms: int | None = None,
        analysis: QueryAnalysisResult | None = None,
        system_state: SystemSample | None = None,
        server_version: str | None = None,
        database_model: str | None = None,
        require_fresh_state: bool = False,
    ) -> ExecutionReport:
        if preview_rows < 1:
            raise ValueError("preview_rows must be positive")
        if timeout_ms is not None and timeout_ms < 0:
            raise ValueError("timeout_ms must be non-negative")

        started = time.perf_counter()
        context = CandidateContext(
            query_id=decision.query_id,
            analysis=analysis,
            system_state=system_state,
            server_version=server_version,
            database_model=database_model,
        )
        validation_context = ValidationContext(
            query_id=decision.query_id,
            analysis=analysis,
            server_version=server_version,
            database_model=database_model,
        )
        baseline_id = self.registry.baseline_strategy_id
        preflight = self._preflight_selected(
            decision,
            context=context,
            validation_context=validation_context,
        )

        # Phase-6 admission check AFTER all candidate validation and audit I/O,
        # immediately before dispatch. Legacy explicit no-state callers retain
        # their Phase-4 contract; the Orchestrator always enables this gate.
        if (require_fresh_state and decision.selected_strategy_id != baseline_id
                and preflight.error is None and preflight.candidate is not None):
            state_error = _dispatch_state_error(system_state)
            if state_error is not None:
                preflight.error = state_error

        if preflight.error is not None or preflight.candidate is None:
            reason = preflight.error or "selected candidate preflight failed"
            if decision.selected_strategy_id == baseline_id:
                raise ExecutionError(
                    f"Native baseline candidate failed preflight: {reason}"
                )
            policy = self.fallback_policy.evaluate(
                selected_strategy_id=decision.selected_strategy_id,
                baseline_strategy_id=baseline_id,
                first_result=None,
                preflight_failure_reason=reason,
            )
            if not policy.allowed:
                raise ExecutionError(
                    f"candidate preflight failed and fallback was denied: "
                    f"{policy.reason}"
                )
            fallback = self._fallback_attempt(
                decision,
                context=context,
                validation_context=validation_context,
                preview_rows=preview_rows,
                timeout_ms=timeout_ms,
                attempt_index=1,
                fallback_reason=policy.reason,
            )
            warnings = [
                f"SELECTED_CANDIDATE_PREFLIGHT_FAILED: {reason}",
                f"NATIVE_FALLBACK_USED: {policy.reason}",
            ]
            return ExecutionReport(
                decision_id=decision.decision_id,
                query_id=decision.query_id,
                selected_strategy_id=decision.selected_strategy_id,
                fallback_strategy_id=baseline_id,
                attempts=[fallback],
                fallback_used=True,
                fallback_trigger=policy.trigger,
                fallback_reason=policy.reason,
                final_status=fallback.execution.status,
                final_execution_id=fallback.execution.execution_id,
                total_wall_ms=round((time.perf_counter() - started) * 1000, 3),
                warnings=warnings,
            )

        selected_candidate = preflight.candidate
        first_result = self._execute_candidate(
            decision,
            candidate=selected_candidate,
            preview_rows=preview_rows,
            timeout_ms=timeout_ms,
        )
        first_attempt = ExecutionAttempt(
            attempt_index=1,
            is_fallback=False,
            requested_strategy_id=decision.selected_strategy_id,
            execution=first_result,
        )
        attempts = [first_attempt]
        warnings: list[str] = []

        if first_result.status is ExecutionStatus.SUCCESS:
            return self._report(
                decision,
                attempts=attempts,
                fallback_decision=None,
                fallback_blocked_reason=None,
                total_started=started,
                warnings=warnings,
            )

        policy = self.fallback_policy.evaluate(
            selected_strategy_id=decision.selected_strategy_id,
            baseline_strategy_id=baseline_id,
            first_result=first_result,
            preflight_failure_reason=None,
        )
        if not policy.allowed:
            blocked = (
                policy.reason
                if decision.selected_strategy_id != baseline_id
                else None
            )
            warnings.append(f"NATIVE_FALLBACK_BLOCKED: {policy.reason}")
            return self._report(
                decision,
                attempts=attempts,
                fallback_decision=None,
                fallback_blocked_reason=blocked,
                total_started=started,
                warnings=warnings,
            )

        try:
            fallback = self._fallback_attempt(
                decision,
                context=context,
                validation_context=validation_context,
                preview_rows=preview_rows,
                timeout_ms=timeout_ms,
                attempt_index=2,
                fallback_reason=policy.reason,
            )
        except ExecutionError as exc:
            warnings.append(f"NATIVE_FALLBACK_FAILED_TO_START: {exc}")
            return self._report(
                decision,
                attempts=attempts,
                fallback_decision=None,
                fallback_blocked_reason=str(exc),
                total_started=started,
                warnings=warnings,
            )
        attempts.append(fallback)
        warnings.append(f"NATIVE_FALLBACK_USED: {policy.reason}")
        return self._report(
            decision,
            attempts=attempts,
            fallback_decision=policy,
            fallback_blocked_reason=None,
            total_started=started,
            warnings=warnings,
        )

    def _preflight_selected(
        self,
        decision: OptimizationDecision,
        *,
        context: CandidateContext,
        validation_context: ValidationContext,
    ) -> _Preflight:
        try:
            entry = self.registry.assert_selectable(decision.selected_strategy_id)
        except StrategyRegistryError as exc:
            raise ExecutionError(f"decision strategy is not selectable: {exc}") from exc
        if entry.spec.version != decision.strategy_version:
            return _Preflight(
                entry=entry,
                candidate=None,
                validation=None,
                error=(
                    f"decision version {decision.strategy_version!r} does not "
                    f"match registry version {entry.spec.version!r}"
                ),
            )
        try:
            candidate = entry.plugin.build_candidate(decision.original_sql, context)
        except Exception as exc:  # noqa: BLE001 - untrusted plugin/candidate boundary
            return _Preflight(
                entry=entry,
                candidate=None,
                validation=None,
                error=(
                    f"candidate build failed: {type(exc).__name__}: {exc}"
                ),
            )
        if candidate.strategy_id != decision.selected_strategy_id:
            return _Preflight(
                entry=entry,
                candidate=None,
                validation=None,
                error="rebuilt candidate has a different strategy_id",
            )
        if candidate.strategy_version != decision.strategy_version:
            return _Preflight(
                entry=entry,
                candidate=None,
                validation=None,
                error="rebuilt candidate has a different strategy version",
            )
        if (
            candidate.candidate_sql != decision.candidate_sql
            or candidate.execution_options != decision.execution_options
        ):
            return _Preflight(
                entry=entry,
                candidate=None,
                validation=None,
                error=(
                    "decision candidate does not match the candidate rebuilt by "
                    "the registered strategy"
                ),
            )
        try:
            validation = entry.plugin.validate(
                decision.original_sql,
                candidate,
                validation_context,
            )
        except Exception as exc:  # noqa: BLE001 - validation failure is fail-closed
            return _Preflight(
                entry=entry,
                candidate=candidate,
                validation=None,
                error=f"candidate validation raised {type(exc).__name__}: {exc}",
            )
        try:
            central_validation = self.registry.validate_candidate(
                entry,
                decision.original_sql,
                candidate,
                validation_context,
            )
        except Exception as exc:  # noqa: BLE001 - central validation is fail-closed
            return _Preflight(
                entry=entry,
                candidate=candidate,
                validation=validation,
                error=(
                    f"CENTRAL_VALIDATION_ERROR: {type(exc).__name__}: {exc}"
                ),
            )
        validation = ValidationResult.combine(validation, central_validation)
        if not validation.passed:
            return _Preflight(
                entry=entry,
                candidate=candidate,
                validation=validation,
                error=f"candidate validation failed: {validation.error_summary()}",
            )
        return _Preflight(
            entry=entry,
            candidate=candidate,
            validation=validation,
            error=None,
        )

    def _fallback_attempt(
        self,
        decision: OptimizationDecision,
        *,
        context: CandidateContext,
        validation_context: ValidationContext,
        preview_rows: int,
        timeout_ms: int | None,
        attempt_index: int,
        fallback_reason: str,
    ) -> ExecutionAttempt:
        baseline = self.registry.baseline
        try:
            candidate = baseline.plugin.build_candidate(decision.original_sql, context)
            validation = baseline.plugin.validate(
                decision.original_sql,
                candidate,
                validation_context,
            )
        except Exception as exc:  # noqa: BLE001 - fallback must fail closed
            raise ExecutionError(
                f"Native fallback candidate could not be built/validated: "
                f"{type(exc).__name__}: {exc}"
            ) from exc
        try:
            central_validation = self.registry.validate_candidate(
                baseline,
                decision.original_sql,
                candidate,
                validation_context,
            )
        except Exception as exc:  # noqa: BLE001 - fallback must fail closed
            raise ExecutionError(
                f"Native fallback candidate failed central validation: "
                f"{type(exc).__name__}: {exc}"
            ) from exc
        validation = ValidationResult.combine(validation, central_validation)
        if not validation.passed:
            raise ExecutionError(
                f"Native fallback candidate failed validation: "
                f"{validation.error_summary()}"
            )
        result = self._execute_candidate(
            decision,
            candidate=candidate,
            preview_rows=preview_rows,
            timeout_ms=timeout_ms,
        )
        return ExecutionAttempt(
            attempt_index=attempt_index,
            is_fallback=True,
            requested_strategy_id=baseline.spec.strategy_id,
            fallback_reason=fallback_reason,
            execution=result,
        )

    def _execute_candidate(
        self,
        decision: OptimizationDecision,
        *,
        candidate: CandidatePlan,
        preview_rows: int,
        timeout_ms: int | None,
    ) -> ExecutionResult:
        request = QueryRequest(
            query_id=decision.query_id,
            sql=candidate.candidate_sql,
            source=f"decision:{decision.decision_mode.value}",
            timeout_ms=timeout_ms,
        )
        try:
            result = self.adapter.execute_readonly(
                request,
                preview_rows=preview_rows,
                strategy_id=candidate.strategy_id,
                execution_options=candidate.execution_options,
            )
        except AdapterError as exc:
            raise ExecutionError(f"adapter could not execute the candidate: {exc}") from exc
        except Exception as exc:  # noqa: BLE001 - adapter implementations are untrusted
            # A non-AdapterError escaping the adapter still means the request
            # may have reached the server.  Fail closed: no fallback/retry is
            # attempted, and the cause is preserved for the caller.
            raise ExecutionError(
                "adapter raised an unexpected error while executing the "
                f"candidate: {type(exc).__name__}: {exc}"
            ) from exc
        if result.strategy_id != candidate.strategy_id:
            raise ExecutionError(
                "adapter execution label does not match the selected strategy"
            )
        if result.query_id != decision.query_id:
            raise ExecutionError(
                "adapter execution query_id does not match the decision"
            )
        if result.sql_sha256 != sql_sha256(candidate.candidate_sql):
            raise ExecutionError(
                "adapter execution SQL hash does not match the candidate SQL"
            )
        return result

    def _report(
        self,
        decision: OptimizationDecision,
        *,
        attempts: list[ExecutionAttempt],
        fallback_decision: FallbackDecision | None,
        fallback_blocked_reason: str | None,
        total_started: float,
        warnings: list[str],
    ) -> ExecutionReport:
        last = attempts[-1]
        fallback_used = fallback_decision is not None
        return ExecutionReport(
            decision_id=decision.decision_id,
            query_id=decision.query_id,
            selected_strategy_id=decision.selected_strategy_id,
            fallback_strategy_id=self.registry.baseline_strategy_id,
            attempts=attempts,
            fallback_used=fallback_used,
            fallback_trigger=(
                fallback_decision.trigger
                if fallback_decision is not None
                else FallbackTrigger.NONE
            ),
            fallback_reason=(
                fallback_decision.reason if fallback_decision is not None else None
            ),
            fallback_blocked_reason=fallback_blocked_reason,
            final_status=last.execution.status,
            final_execution_id=last.execution.execution_id,
            total_wall_ms=round((time.perf_counter() - total_started) * 1000, 3),
            warnings=warnings,
        )


__all__ = ["StrategyExecutor"]


def _dispatch_state_error(sample: SystemSample | None) -> str | None:
    """Source-aware admission, not a new sample or a server cancellation claim."""
    now = utc_now()
    if sample is None:
        return "RESOURCE_STATE_UNUSABLE_AT_DISPATCH: client_host missing"
    for name in ('client_host', 'db_host', 'db_metrics'):
        snapshot = getattr(sample, name)
        if name != 'client_host' and (snapshot is None or not any(
                getattr(snapshot, field) is not None for field in (
                    'cpu_percent', 'memory_percent', 'disk_usage_percent',
                    'disk_read_bytes_per_sec', 'disk_write_bytes_per_sec', 'active_queries'))):
            # No DB agent is unavailable evidence, not fabricated low load.
            continue
        if snapshot.source_type.value != name or not snapshot.is_usable(now=now):
            return f"RESOURCE_STATE_UNUSABLE_AT_DISPATCH: {name} invalid, stale or source-mismatched"
    return None
