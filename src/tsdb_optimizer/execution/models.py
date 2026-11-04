"""Execution and fallback audit contracts."""

from __future__ import annotations

from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field, model_validator

from ..decision.models import OptimizationDecision
from ..schemas.execution import ExecutionResult, ExecutionStatus


class FallbackTrigger(StrEnum):
    NONE = "NONE"
    CANDIDATE_VALIDATION_FAILED = "CANDIDATE_VALIDATION_FAILED"
    FIRST_REQUEST_NOT_DISPATCHED = "FIRST_REQUEST_NOT_DISPATCHED"
    FIRST_REQUEST_RETRY_SAFE = "FIRST_REQUEST_RETRY_SAFE"


class FallbackDecision(BaseModel):
    """Outcome of the conservative retry-safety policy."""

    model_config = ConfigDict(extra="forbid")

    allowed: bool
    trigger: FallbackTrigger
    reason: str = Field(min_length=1, max_length=2000)

    @model_validator(mode="after")
    def trigger_matches_allowed(self) -> "FallbackDecision":
        if self.allowed and self.trigger is FallbackTrigger.NONE:
            raise ValueError("an allowed fallback needs a non-NONE trigger")
        if not self.allowed and self.trigger is not FallbackTrigger.NONE:
            raise ValueError("a blocked fallback must use trigger=NONE")
        return self


class ExecutionAttempt(BaseModel):
    """One adapter call, including first-attempt or Native-fallback metadata."""

    model_config = ConfigDict(extra="forbid")

    attempt_index: int = Field(ge=1)
    is_fallback: bool
    requested_strategy_id: str = Field(min_length=1)
    fallback_reason: str | None = Field(default=None, max_length=2000)
    execution: ExecutionResult

    @model_validator(mode="after")
    def attempt_metadata_is_consistent(self) -> "ExecutionAttempt":
        if self.requested_strategy_id != self.execution.strategy_id:
            raise ValueError(
                "requested strategy does not match the adapter execution label"
            )
        if self.is_fallback and not (self.fallback_reason or "").strip():
            raise ValueError("a fallback attempt needs a reason")
        if not self.is_fallback and self.fallback_reason is not None:
            raise ValueError("a first attempt must not carry a fallback reason")
        return self


class ExecutionReport(BaseModel):
    """One decision's complete execution and fallback evidence."""

    model_config = ConfigDict(extra="forbid")

    decision_id: str = Field(min_length=1)
    query_id: str = Field(min_length=1)
    selected_strategy_id: str = Field(min_length=1)
    fallback_strategy_id: str = Field(min_length=1)
    attempts: list[ExecutionAttempt] = Field(min_length=1)
    fallback_used: bool
    fallback_trigger: FallbackTrigger = FallbackTrigger.NONE
    fallback_reason: str | None = Field(default=None, max_length=2000)
    fallback_blocked_reason: str | None = Field(default=None, max_length=2000)
    final_status: ExecutionStatus
    final_execution_id: str = Field(min_length=1)
    # Executor wall time (decision time excluded).
    total_wall_ms: float = Field(ge=0.0)
    # Service wall time from request intake through decision, execution and
    # attempt persistence.  It excludes the execution_reports row write and
    # report serialization.  The raw executor leaves this unknown; the service
    # fills it in so callers cannot mistake total_wall_ms for end-to-end time.
    end_to_end_ms: float | None = Field(default=None, ge=0.0)
    warnings: list[str] = Field(default_factory=list)

    @model_validator(mode="after")
    def execution_sequence_is_consistent(self) -> "ExecutionReport":
        expected_indices = list(range(1, len(self.attempts) + 1))
        actual_indices = [attempt.attempt_index for attempt in self.attempts]
        if actual_indices != expected_indices:
            raise ValueError("attempt_index values must be contiguous from 1")
        if any(attempt.execution.query_id != self.query_id for attempt in self.attempts):
            raise ValueError("all executions must belong to the report query_id")
        fallback_attempts = [attempt for attempt in self.attempts if attempt.is_fallback]
        last = self.attempts[-1]
        if self.final_execution_id != last.execution.execution_id:
            raise ValueError("final_execution_id must identify the last attempt")
        if self.final_status != last.execution.status:
            raise ValueError("final_status must match the last attempt")
        if self.fallback_used:
            if self.selected_strategy_id == self.fallback_strategy_id:
                raise ValueError("the baseline cannot fall back to itself")
            if len(fallback_attempts) != 1 or not last.is_fallback:
                raise ValueError(
                    "fallback_used requires exactly one final fallback attempt"
                )
            if self.fallback_trigger is FallbackTrigger.NONE:
                raise ValueError("a used fallback needs a trigger")
            if not (self.fallback_reason or "").strip():
                raise ValueError("a used fallback needs a reason")
            if self.fallback_blocked_reason is not None:
                raise ValueError("a used fallback cannot be blocked")
        else:
            if fallback_attempts:
                raise ValueError("a fallback attempt requires fallback_used=true")
            if self.fallback_trigger is not FallbackTrigger.NONE:
                raise ValueError("no fallback occurred but a trigger was recorded")
            if self.fallback_reason is not None:
                raise ValueError("no fallback occurred but a reason was recorded")
        return self


class OptimizationRun(BaseModel):
    """Decision plus its execution report, for CLI/library callers."""

    model_config = ConfigDict(extra="forbid")

    decision: OptimizationDecision
    execution: ExecutionReport

    @model_validator(mode="after")
    def decision_and_execution_match(self) -> "OptimizationRun":
        if self.decision.decision_id != self.execution.decision_id:
            raise ValueError("decision and execution IDs do not match")
        if self.decision.query_id != self.execution.query_id:
            raise ValueError("decision and execution query IDs do not match")
        if (
            self.decision.selected_strategy_id
            != self.execution.selected_strategy_id
        ):
            raise ValueError("decision and executed strategy IDs do not match")
        return self


__all__ = [
    "ExecutionAttempt",
    "ExecutionReport",
    "FallbackDecision",
    "FallbackTrigger",
    "OptimizationRun",
]
