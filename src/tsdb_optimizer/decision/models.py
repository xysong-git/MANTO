"""Agent 2 decision contracts and cost evidence."""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from ..schemas.common import RECORD_SCHEMA_VERSION, ensure_utc, new_id, utc_now
from ..strategies.models import (
    ApplicabilityResult,
    CandidatePlan,
    ValidationResult,
)


class DecisionMode(StrEnum):
    BASELINE = "baseline"
    RULE_BASED = "rule_based"
    ADAPTIVE = "adaptive"


class CostSource(StrEnum):
    HISTORICAL_STATISTIC = "HISTORICAL_STATISTIC"
    RULE = "RULE"
    UNKNOWN = "UNKNOWN"


class CostEstimate(BaseModel):
    """Latency evidence, or an explicit statement that it is unknown.

    Historical estimates are medians of successful, non-fallback executions
    with the same candidate SQL hash and strategy ID.  They are descriptive,
    not a confidence-bearing performance model.
    """

    model_config = ConfigDict(extra="forbid")

    source: CostSource
    expected_latency_ms: float | None = Field(default=None, ge=0.0)
    sample_count: int = Field(default=0, ge=0)
    confidence: float | None = Field(default=None, ge=0.0, le=1.0)
    unknown_reason: str | None = None

    @model_validator(mode="after")
    def source_matches_payload(self) -> "CostEstimate":
        if self.source is CostSource.UNKNOWN:
            if self.expected_latency_ms is not None:
                raise ValueError("unknown cost must not have a latency value")
            if self.confidence is not None:
                raise ValueError("unknown cost must not have a confidence value")
            if not (self.unknown_reason or "").strip():
                raise ValueError("unknown cost needs an explicit reason")
            return self
        if self.expected_latency_ms is None:
            raise ValueError("a known cost source needs expected_latency_ms")
        if self.unknown_reason is not None:
            raise ValueError("a known cost source must not have an unknown reason")
        if self.source is CostSource.HISTORICAL_STATISTIC and self.sample_count < 1:
            raise ValueError("historical cost needs at least one sample")
        return self


class AdaptiveAdvice(BaseModel):
    """Optional caller-provided adaptive recommendation.

    Phase 4 has no learner and no model producer for this object.  The interface
    exists so a later learning phase can recommend only a strategy ID that the
    registry and validator have already accepted.  It cannot supply SQL.
    """

    model_config = ConfigDict(extra="forbid")

    strategy_id: str = Field(min_length=1)
    model_version: str = Field(min_length=1)
    sample_count: int = Field(default=0, ge=0)
    reason: str = Field(min_length=1, max_length=2000)


class CandidateEvaluation(BaseModel):
    """Auditable result for every enabled strategy considered by Agent 2."""

    model_config = ConfigDict(extra="forbid")

    strategy_id: str = Field(min_length=1)
    strategy_version: str = Field(min_length=1)
    applicable: bool
    applicability: ApplicabilityResult
    candidate: CandidatePlan | None = None
    validation: ValidationResult | None = None
    cost: CostEstimate
    selected: bool = False
    rejection_reason: str | None = None

    @model_validator(mode="after")
    def selected_candidate_is_fully_valid(self) -> "CandidateEvaluation":
        if self.strategy_id != self.applicability.strategy_id:
            raise ValueError("candidate evaluation strategy IDs do not match")
        if self.applicable != self.applicability.applicable:
            raise ValueError("applicability flag does not match its evidence")
        if self.selected:
            if not self.applicable:
                raise ValueError("a non-applicable strategy cannot be selected")
            if self.candidate is None:
                raise ValueError("a selected strategy needs a candidate")
            if self.validation is None or not self.validation.passed:
                raise ValueError("a selected strategy needs passed validation")
            if self.rejection_reason is not None:
                raise ValueError("a selected strategy cannot have a rejection reason")
        elif not (self.rejection_reason or "").strip():
            raise ValueError("a non-selected strategy needs a rejection reason")
        if self.candidate is None and self.validation is not None:
            raise ValueError("validation evidence requires a candidate")
        return self


class OptimizationDecision(BaseModel):
    """Agent 2 output; every field needed to audit execution is included."""

    model_config = ConfigDict(extra="forbid")

    schema_version: int = Field(default=RECORD_SCHEMA_VERSION, ge=1)
    decision_id: str = Field(default_factory=new_id)
    query_id: str = Field(min_length=1)
    selected_strategy_id: str = Field(min_length=1)
    strategy_version: str = Field(min_length=1)
    original_sql: str = Field(min_length=1)
    candidate_sql: str = Field(min_length=1)
    execution_options: dict[str, str | int | float | bool] = Field(
        default_factory=dict
    )
    expected_latency_ms: float | None = Field(default=None, ge=0.0)
    confidence: float | None = Field(default=None, ge=0.0, le=1.0)
    decision_mode: DecisionMode
    # 1 for the first exact cleaned SQL that produced a successful decision in
    # this process-local engine.  Failed decisions do not consume an occurrence.
    # The repeat-cache rule uses this value and it is persisted inside
    # decision_json.
    query_occurrence: int = Field(default=1, ge=1)
    model_version: str | None = None
    fallback_strategy_id: str = Field(default="S0_NATIVE", min_length=1)
    reason: str = Field(min_length=1, max_length=4000)
    cost_estimate: CostEstimate
    considered: list[CandidateEvaluation] = Field(min_length=1)
    decision_overhead_ms: float = Field(default=0.0, ge=0.0)
    warnings: list[str] = Field(default_factory=list)
    created_at: datetime = Field(default_factory=utc_now)

    @field_validator("created_at")
    @classmethod
    def created_at_must_be_utc(cls, value: datetime) -> datetime:
        normalized = ensure_utc(value)
        assert normalized is not None
        return normalized

    @model_validator(mode="after")
    def selected_entry_matches_decision(self) -> "OptimizationDecision":
        selected = [entry for entry in self.considered if entry.selected]
        if len(selected) != 1:
            raise ValueError("a decision must mark exactly one candidate selected")
        chosen = selected[0]
        if chosen.strategy_id != self.selected_strategy_id:
            raise ValueError("selected_strategy_id does not match the selected entry")
        if chosen.strategy_version != self.strategy_version:
            raise ValueError("strategy_version does not match the selected entry")
        if chosen.candidate is None:
            raise ValueError("the selected candidate is missing")
        if chosen.candidate.candidate_sql != self.candidate_sql:
            raise ValueError("candidate_sql does not match the selected entry")
        if chosen.candidate.execution_options != self.execution_options:
            raise ValueError("execution_options do not match the selected entry")
        if chosen.cost != self.cost_estimate:
            raise ValueError("cost_estimate does not match the selected entry")
        if self.expected_latency_ms != self.cost_estimate.expected_latency_ms:
            raise ValueError("expected_latency_ms does not match cost evidence")
        if self.confidence != self.cost_estimate.confidence:
            raise ValueError("confidence does not match cost evidence")
        if self.decision_mode is DecisionMode.BASELINE:
            if self.selected_strategy_id != self.fallback_strategy_id:
                raise ValueError("baseline mode must select the fallback baseline")
        if self.decision_mode is not DecisionMode.ADAPTIVE and self.model_version is not None:
            raise ValueError("only adaptive mode may carry a model_version")
        return self


__all__ = [
    "AdaptiveAdvice",
    "CandidateEvaluation",
    "CostEstimate",
    "CostSource",
    "DecisionMode",
    "OptimizationDecision",
]
