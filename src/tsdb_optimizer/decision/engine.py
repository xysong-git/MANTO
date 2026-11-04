"""Deterministic, LLM-free Agent 2 decision engine."""

from __future__ import annotations

import logging
import time
from collections import OrderedDict
from contextlib import contextmanager
from dataclasses import dataclass
from threading import Lock
from typing import Iterator

from ..agents import QueryAnalysisAgent
from ..errors import DecisionError, UnsafeSQLError
from ..schemas.common import utc_now
from ..schemas.monitoring import (
    MetricUnavailableReason,
    SystemSample,
    SystemSnapshot,
    SystemSourceType,
)
from ..schemas.query_features import ParseStatus, QueryAnalysisResult
from ..sql_guard import sql_sha256, validate_readonly_sql
from ..strategies import (
    CandidateContext,
    CandidatePlan,
    RegisteredStrategy,
    StrategyRegistry,
    ValidationContext,
    ValidationResult,
)
from ..strategies.models import ApplicabilityResult, SelectionPolicy
from .cost import CostEstimator, UnknownCostEstimator
from .models import (
    AdaptiveAdvice,
    CandidateEvaluation,
    CostEstimate,
    CostSource,
    DecisionMode,
    OptimizationDecision,
)
from .resource_policy import (
    ResourcePolicy,
    ResourcePressure,
    assess_resource_pressure,
    load_default_resource_policy,
)

logger = logging.getLogger(__name__)

# Bounded process-local occurrence tracking.  Evicting an old exact SQL merely
# resets its next occurrence to 1 (Native), which is always safe.
MAX_TRACKED_SQL_OCCURRENCES = 100_000


@dataclass
class _Draft:
    entry: RegisteredStrategy
    applicability: ApplicabilityResult
    candidate: CandidatePlan | None
    validation: ValidationResult | None
    cost: CostEstimate
    rejection: str | None

    @property
    def valid(self) -> bool:
        return (
            self.rejection is None
            and self.applicability.applicable
            and self.candidate is not None
            and self.validation is not None
            and self.validation.passed
        )


@dataclass(frozen=True)
class _Selection:
    draft: _Draft
    reason: str
    warnings: tuple[str, ...] = ()


class DecisionEngine:
    """Select exactly one registered, applicable, validated candidate.

    All modes are deterministic and LLM-free.  ``adaptive`` accepts only a
    caller-provided strategy recommendation; Phase 4 contains no learner.
    """

    def __init__(
        self,
        registry: StrategyRegistry,
        *,
        cost_estimator: CostEstimator | None = None,
        analyzer: QueryAnalysisAgent | None = None,
        resource_policy: ResourcePolicy | None = None,
    ):
        self.registry = registry
        self.cost_estimator = cost_estimator or UnknownCostEstimator()
        self.analyzer = analyzer or QueryAnalysisAgent()
        self.resource_policy = (
            load_default_resource_policy()
            if resource_policy is None
            else resource_policy
        )
        self._occurrences: OrderedDict[str, int] = OrderedDict()
        self._occurrence_lock = Lock()

    @contextmanager
    def _occurrence_scope(self, cleaned_sql: str) -> Iterator[int]:
        """Reserve an occurrence that is committed only if the decision succeeds.

        A failed decision must not consume an occurrence: otherwise a later
        first successful decision can be mislabeled as a repeat and the
        repeat-cache rule can select S1 before any execution has populated the
        cache.  The lock is held until the decision is fully constructed, not
        just until the reservation is made, so no later reservation can
        interleave.  Rollback therefore always restores the exact previous
        count, including when several threads decide the same SQL.  The critical
        section contains only local selection/validation-result assembly and
        logging; strategy applicability, candidate generation, validation and
        cost estimation run before it, so database/network work is not
        serialized here.
        """

        key = sql_sha256(cleaned_sql)
        with self._occurrence_lock:
            previous = self._occurrences.get(key, 0)
            count = previous + 1
            self._occurrences[key] = count
            self._occurrences.move_to_end(key)
            while len(self._occurrences) > MAX_TRACKED_SQL_OCCURRENCES:
                self._occurrences.popitem(last=False)
            try:
                yield count
            except BaseException:
                if previous:
                    self._occurrences[key] = previous
                    self._occurrences.move_to_end(key)
                else:
                    self._occurrences.pop(key, None)
                raise

    def reset_occurrence_tracking(self) -> None:
        """Clear process-local repeat evidence without changing the registry."""

        with self._occurrence_lock:
            self._occurrences.clear()

    def decide(
        self,
        *,
        query_id: str,
        original_sql: str,
        mode: DecisionMode | str = DecisionMode.RULE_BASED,
        analysis: QueryAnalysisResult | None = None,
        system_state: SystemSample | None = None,
        server_version: str | None = None,
        database_model: str | None = None,
        adaptive_advice: AdaptiveAdvice | None = None,
    ) -> OptimizationDecision:
        started = time.perf_counter()
        try:
            decision_mode = DecisionMode(mode)
        except ValueError as exc:
            allowed = [item.value for item in DecisionMode]
            raise DecisionError(
                f"unsupported decision mode {mode!r}; expected one of {allowed}"
            ) from exc

        cleaned = validate_readonly_sql(original_sql)
        if analysis is None:
            analysis = self.analyzer.analyze(cleaned, query_id=query_id)
        self._validate_analysis(query_id=query_id, cleaned=cleaned, analysis=analysis)

        effective_state, state_warnings = _effective_system_state(system_state)
        resource_pressure = assess_resource_pressure(
            effective_state,
            self.resource_policy,
        )
        candidate_context = CandidateContext(
            query_id=query_id,
            analysis=analysis,
            system_state=effective_state,
            server_version=server_version,
            database_model=database_model,
        )
        validation_context = ValidationContext(
            query_id=query_id,
            analysis=analysis,
            server_version=server_version,
            database_model=database_model,
        )
        drafts = [
            self._evaluate(
                entry,
                analysis=analysis,
                system_state=effective_state,
                candidate_context=candidate_context,
                validation_context=validation_context,
            )
            for entry in self.registry.enabled_entries()
        ]
        if not drafts:
            raise DecisionError("registry has no enabled strategy")

        baseline_id = self.registry.baseline_strategy_id
        baseline = next(
            (draft for draft in drafts if draft.entry.spec.strategy_id == baseline_id),
            None,
        )
        if baseline is None:
            raise DecisionError(f"baseline {baseline_id} is not enabled")
        if not baseline.valid:
            raise DecisionError(
                f"baseline {baseline_id} failed validation: "
                f"{baseline.rejection or baseline.validation}"
            )

        # Occurrence is reserved only after every cheap fail-closed check has
        # passed, and is rolled back if selection or decision construction
        # fails.  The occurrence lock is held until the decision is complete,
        # so a concurrent decision cannot turn a failed reservation into a
        # repeat for a later first success.
        with self._occurrence_scope(cleaned) as occurrence:
            mode_warnings: list[str] = list(state_warnings)
            if resource_pressure.warning is not None:
                mode_warnings.append(resource_pressure.warning)
            if decision_mode is DecisionMode.BASELINE:
                baseline_reason = (
                    f"baseline: selected {baseline_id}; "
                    f"{len(drafts)} enabled strategy considered; no rewrite"
                )
                if resource_pressure.breached:
                    baseline_reason += (
                        f"; resource policy {resource_pressure.policy_version} "
                        f"exceeded ({resource_pressure.details}); the baseline "
                        "is already the conservative choice"
                    )
                selection = _Selection(baseline, baseline_reason)
            elif decision_mode is DecisionMode.RULE_BASED:
                selection = self._select_rule_based(
                    drafts,
                    baseline,
                    occurrence=occurrence,
                    resource_pressure=resource_pressure,
                )
            else:
                selection = self._select_adaptive(
                    drafts,
                    baseline,
                    adaptive_advice=adaptive_advice,
                    occurrence=occurrence,
                    warnings=mode_warnings,
                    resource_pressure=resource_pressure,
                )

            selected_id = selection.draft.entry.spec.strategy_id
            considered = [
                self._to_evaluation(
                    draft,
                    selected=draft is selection.draft,
                    mode=decision_mode,
                    resource_pressure=resource_pressure,
                )
                for draft in drafts
            ]
            selected_cost = selection.draft.cost

            warnings = self._collect_warnings(
                analysis=analysis,
                selection=selection,
                mode_warnings=mode_warnings,
                enabled_ids=[draft.entry.spec.strategy_id for draft in drafts],
                baseline_strategy_id=baseline_id,
            )
            model_version = (
                adaptive_advice.model_version
                if decision_mode is DecisionMode.ADAPTIVE
                and adaptive_advice is not None
                and selected_id == adaptive_advice.strategy_id
                else None
            )
            overhead_ms = round((time.perf_counter() - started) * 1000, 3)
            decision = OptimizationDecision(
                query_id=query_id,
                selected_strategy_id=selected_id,
                strategy_version=selection.draft.entry.spec.version,
                original_sql=cleaned,
                candidate_sql=selection.draft.candidate.candidate_sql,  # type: ignore[union-attr]
                execution_options=selection.draft.candidate.execution_options,  # type: ignore[union-attr]
                expected_latency_ms=selected_cost.expected_latency_ms,
                confidence=selected_cost.confidence,
                decision_mode=decision_mode,
                query_occurrence=occurrence,
                model_version=model_version,
                fallback_strategy_id=baseline_id,
                reason=selection.reason,
                cost_estimate=selected_cost,
                considered=considered,
                decision_overhead_ms=overhead_ms,
                warnings=warnings,
            )
            logger.info(
                "optimization decision",
                extra={
                    "event": "decision",
                    "query_id": query_id,
                    "decision_id": decision.decision_id,
                    "strategy_id": selected_id,
                    "strategy_version": decision.strategy_version,
                    "decision_mode": decision_mode.value,
                    "query_occurrence": occurrence,
                    "expected_latency_ms": decision.expected_latency_ms,
                    "decision_overhead_ms": overhead_ms,
                    "considered_count": len(considered),
                    "cost_source": selected_cost.source.value,
                    "sample_count": selected_cost.sample_count,
                },
            )
        return decision

    def _evaluate(
        self,
        entry: RegisteredStrategy,
        *,
        analysis: QueryAnalysisResult,
        system_state: SystemSample | None,
        candidate_context: CandidateContext,
        validation_context: ValidationContext,
    ) -> _Draft:
        strategy_id = entry.spec.strategy_id
        try:
            applicability = entry.plugin.is_applicable(analysis, system_state)
        except Exception as exc:  # noqa: BLE001 - a broken plugin must not be selected
            logger.exception(
                "strategy applicability failed",
                extra={"event": "strategy_error", "strategy_id": strategy_id},
            )
            reason = f"STRATEGY_APPLICABILITY_ERROR: {type(exc).__name__}: {exc}"
            return _Draft(
                entry=entry,
                applicability=ApplicabilityResult(
                    strategy_id=strategy_id,
                    applicable=False,
                    reasons=[reason],
                ),
                candidate=None,
                validation=None,
                cost=self._unknown_cost(reason),
                rejection=reason,
            )
        if not applicability.applicable:
            reason = "; ".join(applicability.reasons)
            return _Draft(
                entry=entry,
                applicability=applicability,
                candidate=None,
                validation=None,
                cost=self._unknown_cost(f"NOT_APPLICABLE: {reason}"),
                rejection=f"not applicable: {reason}",
            )
        try:
            candidate = entry.plugin.build_candidate(
                analysis.parse.sql,
                candidate_context,
            )
        except Exception as exc:  # noqa: BLE001 - candidate generation is untrusted
            logger.exception(
                "strategy candidate generation failed",
                extra={"event": "strategy_error", "strategy_id": strategy_id},
            )
            reason = f"CANDIDATE_BUILD_ERROR: {type(exc).__name__}: {exc}"
            return _Draft(
                entry=entry,
                applicability=applicability,
                candidate=None,
                validation=None,
                cost=self._unknown_cost(reason),
                rejection=reason,
            )
        try:
            validation = entry.plugin.validate(
                analysis.parse.sql,
                candidate,
                validation_context,
            )
        except Exception as exc:  # noqa: BLE001 - validation failures fail closed
            logger.exception(
                "strategy validation failed",
                extra={"event": "strategy_error", "strategy_id": strategy_id},
            )
            reason = f"VALIDATION_ERROR: {type(exc).__name__}: {exc}"
            return _Draft(
                entry=entry,
                applicability=applicability,
                candidate=candidate,
                validation=None,
                cost=self._unknown_cost(reason),
                rejection=reason,
            )
        try:
            central_validation = self.registry.validate_candidate(
                entry,
                analysis.parse.sql,
                candidate,
                validation_context,
            )
        except Exception as exc:  # noqa: BLE001 - central validation fails closed
            logger.exception(
                "registry central validation failed",
                extra={"event": "strategy_error", "strategy_id": strategy_id},
            )
            reason = f"CENTRAL_VALIDATION_ERROR: {type(exc).__name__}: {exc}"
            return _Draft(
                entry=entry,
                applicability=applicability,
                candidate=candidate,
                validation=validation,
                cost=self._unknown_cost(reason),
                rejection=reason,
            )
        validation = ValidationResult.combine(validation, central_validation)
        if not validation.passed:
            reason = validation.error_summary()
            return _Draft(
                entry=entry,
                applicability=applicability,
                candidate=candidate,
                validation=validation,
                cost=self._unknown_cost("VALIDATION_FAILED"),
                rejection=reason,
            )
        try:
            cost = self.cost_estimator.estimate(
                strategy_id=strategy_id,
                candidate_sql=candidate.candidate_sql,
            )
        except Exception as exc:  # noqa: BLE001 - cost is optional evidence
            logger.exception(
                "cost estimation failed",
                extra={"event": "cost_error", "strategy_id": strategy_id},
            )
            cost = self._unknown_cost(
                f"COST_ESTIMATOR_ERROR: {type(exc).__name__}: {exc}"
            )
        return _Draft(
            entry=entry,
            applicability=applicability,
            candidate=candidate,
            validation=validation,
            cost=cost,
            rejection=None,
        )

    def _select_rule_based(
        self,
        drafts: list[_Draft],
        baseline: _Draft,
        *,
        occurrence: int,
        resource_pressure: ResourcePressure,
    ) -> _Selection:
        baseline_id = baseline.entry.spec.strategy_id
        if resource_pressure.breached:
            return _Selection(
                baseline,
                (
                    f"rule_based: selected {baseline_id} because resource policy "
                    f"{resource_pressure.policy_version} was exceeded "
                    f"({resource_pressure.details}); resource pressure vetoes "
                    "every non-Native strategy and the baseline is the "
                    "conservative fallback"
                ),
            )
        valid = [draft for draft in drafts if draft.valid]
        non_baseline = [
            draft
            for draft in valid
            if draft.entry.spec.strategy_id != baseline_id
        ]
        if not non_baseline:
            return _Selection(
                baseline,
                (
                    f"rule_based: selected {baseline_id} because no enabled "
                    "non-Native strategy passed applicability and validation "
                    "for this query"
                ),
            )
        repeat_candidates = [
            draft
            for draft in non_baseline
            if draft.entry.spec.selection_policy is SelectionPolicy.REPEAT_CACHE
        ]
        cost_candidates = [
            draft
            for draft in non_baseline
            if draft.entry.spec.selection_policy is not SelectionPolicy.REPEAT_CACHE
        ]
        if occurrence >= 2 and repeat_candidates:
            # Deterministic priority: an exact repeat under a caller-declared
            # epoch is served by the cache rule, not by an estimated latency.
            # This occurrence may be a miss or a hit depending on whether an
            # earlier successful execution populated the cache.
            selected = min(
                repeat_candidates,
                key=lambda draft: draft.entry.spec.strategy_id,
            )
            return _Selection(
                selected,
                (
                    f"rule_based: selected {selected.entry.spec.strategy_id} "
                    f"because this exact SQL has occurrence {occurrence} in this "
                    "process and the caller declared a dataset epoch; repeat-cache "
                    "strategies are eligible only from occurrence 2 onward, a "
                    "miss executes once, and a hit dispatches no server request"
                ),
                ("REPEAT_CACHE_RULE_SELECTED",),
            )
        if repeat_candidates and occurrence < 2 and not cost_candidates:
            return _Selection(
                baseline,
                (
                    f"rule_based: selected {baseline_id} because the only valid "
                    "non-Native strategy is a repeat cache and this is "
                    f"occurrence {occurrence} < 2; this occurrence stays Native"
                ),
                ("REPEAT_CACHE_REQUIRES_REPEAT",),
            )
        if baseline.cost.source is CostSource.UNKNOWN:
            return _Selection(
                baseline,
                (
                    f"rule_based: selected {baseline_id} because baseline cost is "
                    "unknown; refusing an ungrounded switch to known non-Native "
                    "candidates"
                ),
                ("BASELINE_COST_UNKNOWN",),
            )
        candidates = [
            draft
            for draft in cost_candidates
            if draft.cost.source is not CostSource.UNKNOWN
            and draft.cost.expected_latency_ms is not None
        ]
        if not candidates:
            return _Selection(
                baseline,
                (
                    f"rule_based: selected {baseline_id} because no valid "
                    "non-Native candidate has reliable historical cost evidence"
                ),
                ("NON_NATIVE_COST_UNKNOWN",),
            )
        best = min(
            candidates,
            key=lambda draft: (
                draft.cost.expected_latency_ms or float("inf"),
                draft.entry.spec.strategy_id,
            ),
        )
        baseline_latency = baseline.cost.expected_latency_ms
        best_latency = best.cost.expected_latency_ms
        if (
            baseline_latency is not None
            and best_latency is not None
            and best_latency < baseline_latency
        ):
            return _Selection(
                best,
                (
                    f"rule_based: selected {best.entry.spec.strategy_id} because "
                    f"its historical median {best_latency:.3f} ms is lower than "
                    f"baseline {baseline_latency:.3f} ms"
                ),
            )
        return _Selection(
            baseline,
            (
                f"rule_based: selected {baseline_id} because no valid non-Native "
                "candidate has a strictly lower historical median"
            ),
        )

    def _select_adaptive(
        self,
        drafts: list[_Draft],
        baseline: _Draft,
        *,
        adaptive_advice: AdaptiveAdvice | None,
        occurrence: int,
        warnings: list[str],
        resource_pressure: ResourcePressure,
    ) -> _Selection:
        baseline_id = baseline.entry.spec.strategy_id
        if resource_pressure.breached:
            return _Selection(
                baseline,
                (
                    f"adaptive: selected {baseline_id} because resource policy "
                    f"{resource_pressure.policy_version} was exceeded "
                    f"({resource_pressure.details}); caller advice cannot "
                    "bypass the conservative resource gate"
                ),
            )
        if adaptive_advice is None:
            warnings.append("ADAPTIVE_UNAVAILABLE_NO_ADVICE")
            return _Selection(
                baseline,
                (
                    f"adaptive: no AdaptiveAdvice was supplied (Phase 4 has no "
                    f"learner); safely selected {baseline_id} baseline"
                ),
            )
        valid_by_id = {
            draft.entry.spec.strategy_id: draft for draft in drafts if draft.valid
        }
        candidate = valid_by_id.get(adaptive_advice.strategy_id)
        if candidate is None:
            reason = (
                f"ADAPTIVE_ADVICE_REJECTED: {adaptive_advice.strategy_id!r} is not "
                "an enabled, applicable, validated candidate"
            )
            warnings.append(reason)
            return _Selection(
                baseline,
                (
                    f"adaptive: {reason}; safely selected {baseline_id} baseline"
                ),
            )
        # Adaptive advice is advisory, not an authorization to bypass the
        # repeat-cache contract.  Repeat-cache strategies are eligible only
        # from occurrence 2 onward, regardless of what selected occurrence 1.
        if (
            candidate.entry.spec.selection_policy is SelectionPolicy.REPEAT_CACHE
            and occurrence < 2
        ):
            reason = (
                "REPEAT_CACHE_REQUIRES_REPEAT: adaptive advice selected "
                f"{candidate.entry.spec.strategy_id} at occurrence "
                f"{occurrence} < 2; repeat-cache strategies are eligible only "
                "from occurrence 2 onward"
            )
            warnings.append(reason)
            return _Selection(
                baseline,
                (
                    f"adaptive: {reason}; safely selected {baseline_id} baseline"
                ),
            )
        return _Selection(
            candidate,
            (
                f"adaptive: selected {adaptive_advice.strategy_id} from "
                f"caller-provided advice (model_version="
                f"{adaptive_advice.model_version}); no Phase-5 learner was invoked"
            ),
        )

    def _to_evaluation(
        self,
        draft: _Draft,
        *,
        selected: bool,
        mode: DecisionMode,
        resource_pressure: ResourcePressure | None = None,
    ) -> CandidateEvaluation:
        if selected:
            rejection = None
        elif draft.rejection is not None:
            rejection = draft.rejection
        elif (
            resource_pressure is not None
            and resource_pressure.breached
            and draft.entry.spec.strategy_id
            != self.registry.baseline_strategy_id
        ):
            rejection = (
                f"vetoed by resource policy "
                f"{resource_pressure.policy_version}: "
                f"{resource_pressure.details}"
            )
        else:
            rejection = f"not selected by {mode.value} mode"
        return CandidateEvaluation(
            strategy_id=draft.entry.spec.strategy_id,
            strategy_version=draft.entry.spec.version,
            applicable=draft.applicability.applicable,
            applicability=draft.applicability,
            candidate=draft.candidate,
            validation=draft.validation,
            cost=draft.cost,
            selected=selected,
            rejection_reason=rejection,
        )

    @staticmethod
    def _unknown_cost(reason: str) -> CostEstimate:
        return CostEstimate(
            source=CostSource.UNKNOWN,
            sample_count=0,
            unknown_reason=reason[:2000],
        )

    @staticmethod
    def _validate_analysis(
        *,
        query_id: str,
        cleaned: str,
        analysis: QueryAnalysisResult,
    ) -> None:
        if analysis.query_id != query_id:
            raise DecisionError(
                "Agent-1 analysis query_id does not match the decision query_id"
            )
        try:
            analyzed_sql = validate_readonly_sql(analysis.parse.sql)
        except UnsafeSQLError as exc:
            raise DecisionError(
                f"Agent-1 analysis contains invalid SQL: {exc}"
            ) from exc
        if analyzed_sql != cleaned:
            raise DecisionError(
                "Agent-1 analysis describes different SQL than the original query"
            )

    @staticmethod
    def _collect_warnings(
        *,
        analysis: QueryAnalysisResult,
        selection: _Selection,
        mode_warnings: list[str],
        enabled_ids: list[str],
        baseline_strategy_id: str,
    ) -> list[str]:
        warnings: list[str] = []
        if analysis.parse_status is not ParseStatus.SUCCESS:
            warnings.append(
                "Agent-1 parser returned UNSUPPORTED for the original SQL; "
                "Native passthrough is allowed and no rewrite is attempted"
            )
        if not any(
            strategy_id != baseline_strategy_id for strategy_id in enabled_ids
        ):
            warnings.append("NO_VERIFIED_NON_NATIVE_STRATEGY")
        warnings.extend(selection.draft.applicability.warnings)
        if selection.draft.validation is not None:
            warnings.extend(
                f"{check.name}: {check.detail}"
                for check in selection.draft.validation.warning_checks
            )
        warnings.extend(selection.warnings)
        warnings.extend(mode_warnings)
        if selection.draft.cost.source is CostSource.UNKNOWN:
            warnings.append(
                "COST_UNKNOWN: "
                + (selection.draft.cost.unknown_reason or "no reason recorded")
            )
        seen: set[str] = set()
        unique: list[str] = []
        for warning in warnings:
            cleaned = warning.strip()
            if cleaned and cleaned not in seen:
                seen.add(cleaned)
                unique.append(cleaned)
        return unique


def _has_metric_values(snapshot: SystemSnapshot) -> bool:
    return any(
        getattr(snapshot, field_name) is not None
        for field_name in (
            "cpu_percent",
            "memory_percent",
            "disk_usage_percent",
            "disk_read_bytes_per_sec",
            "disk_write_bytes_per_sec",
            "active_queries",
        )
    )


def _effective_system_state(
    system_state: SystemSample | None,
) -> tuple[SystemSample | None, list[str]]:
    """Return state safe to expose to strategies, plus degradation warnings.

    Each source is evaluated independently.  Unusable snapshots have their
    values withheld without discarding usable evidence from other sources.
    Already-unavailable snapshots with no values are left unchanged.
    """

    warnings: list[str] = []
    if system_state is None:
        return None, warnings
    now = utc_now()

    def withhold_if_unusable(
        snapshot: SystemSnapshot,
        *,
        expected_source: SystemSourceType,
        label: str,
    ) -> tuple[SystemSnapshot | None, str | None]:
        if not _has_metric_values(snapshot):
            # The normal no-DB-agent sample already has all values withheld.
            # Do not emit a degradation warning merely because no agent exists.
            return None, None
        if snapshot.source_type is not expected_source:
            detail = (
                f"{label} has source_type={snapshot.source_type.value}, expected "
                f"{expected_source.value}"
            )
            return (
                snapshot.unavailable_copy(
                    MetricUnavailableReason.INVALID_VALUE,
                    f"{label} source mismatch withheld at decision time",
                ),
                f"{label}_SOURCE_MISMATCH_DEGRADED: {detail}; "
                "its values were withheld from strategies",
            )
        if not snapshot.valid:
            return (
                snapshot.unavailable_copy(
                    MetricUnavailableReason.INVALID_VALUE,
                    f"invalid {label} snapshot withheld at decision time",
                ),
                f"{label}_INVALID_DEGRADED: {label} was invalid at decision "
                "time; its values were withheld from strategies",
            )
        if snapshot.captured_at > now:
            return (
                snapshot.unavailable_copy(
                    MetricUnavailableReason.FUTURE_SNAPSHOT,
                    f"future {label} snapshot withheld at decision time",
                ),
                f"{label}_FUTURE_DEGRADED: captured_at is after decision time; "
                "its values were withheld from strategies",
            )
        if not snapshot.is_fresh(now=now):
            return (
                snapshot.unavailable_copy(
                    MetricUnavailableReason.STALE_SNAPSHOT,
                    f"stale {label} snapshot withheld at decision time",
                ),
                f"{label}_STALE_DEGRADED: {label} was stale at decision time; "
                "its values were withheld from strategies",
            )
        return None, None

    updates: dict[str, object] = {}
    client_update, client_warning = withhold_if_unusable(
        system_state.client_host,
        expected_source=SystemSourceType.CLIENT_HOST,
        label="SYSTEM_STATE",
    )
    if client_update is not None:
        updates["client_host"] = client_update
    if client_warning is not None:
        warnings.append(client_warning)
    db_host_update, db_host_warning = withhold_if_unusable(
        system_state.db_host,
        expected_source=SystemSourceType.DB_HOST,
        label="DB_HOST_STATE",
    )
    if db_host_update is not None:
        updates["db_host"] = db_host_update
    if db_host_warning is not None:
        warnings.append(db_host_warning)
    if system_state.db_metrics is not None:
        db_metrics_update, db_metrics_warning = withhold_if_unusable(
            system_state.db_metrics,
            expected_source=SystemSourceType.DB_METRICS,
            label="DB_METRICS_STATE",
        )
        if db_metrics_update is not None:
            updates["db_metrics"] = db_metrics_update
        if db_metrics_warning is not None:
            warnings.append(db_metrics_warning)
    if updates:
        return system_state.model_copy(update=updates), warnings
    return system_state, warnings


__all__ = ["DecisionEngine"]
