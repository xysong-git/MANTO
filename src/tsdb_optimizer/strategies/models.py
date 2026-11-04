"""Phase-4 strategy contracts.

A strategy is executable only when its :class:`StrategySpec` is registered and
its verification status is ``VERIFIED``.  These models deliberately separate
the strategy declaration, applicability decision, candidate plan and validation
evidence so every decision can be audited without executing SQL.
"""

from __future__ import annotations

from collections.abc import Mapping
from enum import StrEnum
from types import MappingProxyType
from typing import Any, Literal

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    field_serializer,
    field_validator,
    model_validator,
)

from ..schemas.monitoring import SystemSample
from ..schemas.query_features import QueryAnalysisResult


class StrategyStatus(StrEnum):
    """Certification status from the Phase-0 strategy audit."""

    VERIFIED = "VERIFIED"
    NOT_VERIFIED = "NOT_VERIFIED"
    UNSUPPORTED = "UNSUPPORTED"


class SelectionPolicy(StrEnum):
    """How Agent 2 is allowed to consider a verified strategy.

    ``LOWEST_COST`` strategies participate in the normal historical-cost
    comparison.  ``REPEAT_CACHE`` strategies are selected by their deterministic
    repeat rule and are never compared by cost: their first use is a cache miss
    and only an exact repeat can be a zero-request hit.
    """

    LOWEST_COST = "lowest_cost"
    REPEAT_CACHE = "repeat_cache"


class ValidationCheckStatus(StrEnum):
    PASSED = "PASSED"
    WARNING = "WARNING"
    FAILED = "FAILED"


class ValidationStatus(StrEnum):
    PASSED = "PASSED"
    FAILED = "FAILED"


class StrategySpec(BaseModel):
    """Registry metadata for one strategy.

    ``verification_status`` is an extension to the guide's contract.  It is
    required here so an audit status cannot be omitted or inferred from a
    display name.  An enabled strategy must be ``VERIFIED``.

    Specs are frozen: a registered strategy cannot change its enabled,
    verification or version fields after registration.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    strategy_id: str = Field(min_length=1, max_length=100, pattern=r"^[A-Z][A-Z0-9_]*$")
    name: str = Field(min_length=1, max_length=200)
    database_model: Literal["tree", "table", "unknown"]
    # Tuples, not lists: a frozen Pydantic model is only shallow-frozen, so a
    # mutable list would let a caller change version compatibility after
    # registration.  Pydantic still accepts YAML/JSON lists as input.
    compatible_server_versions: tuple[str, ...] = Field(min_length=1)
    enabled: bool
    verification_status: StrategyStatus
    # Exactly one enabled strategy must be the Native baseline.  The registry
    # enforces this; the flag lets validation distinguish safe baseline
    # passthrough from an unverified optimization.
    is_baseline: bool = False
    # The baseline always uses the ordinary path; a repeat-cache strategy is
    # selected by the occurrence rule and is excluded from cost comparison.
    selection_policy: SelectionPolicy = SelectionPolicy.LOWEST_COST
    implementation_class: str = Field(min_length=1)
    # A frozen Pydantic model is shallow-frozen.  The validator below stores a
    # read-only top-level mapping so callers cannot add/remove/change rules
    # after registration.  Nested values are documentation metadata and are
    # not used for selection; they should be kept to immutable scalars.
    applicability_rules: Mapping[str, Any] = Field(default_factory=dict)
    execution_control: str = Field(min_length=1)
    semantic_test_suite: str = Field(min_length=1)
    plan_effect_expectation: str | None = None
    allowed_execution_options: tuple[str, ...] = ()
    known_limitations: tuple[str, ...] = ()
    version: str = Field(min_length=1, max_length=100)

    @field_validator("applicability_rules")
    @classmethod
    def rules_must_be_read_only(
        cls, value: Mapping[str, Any]
    ) -> Mapping[str, Any]:
        return MappingProxyType(dict(value))

    @field_serializer("applicability_rules")
    def serialize_applicability_rules(
        self, value: Mapping[str, Any]
    ) -> dict[str, Any]:
        return dict(value)

    @field_validator("compatible_server_versions")
    @classmethod
    def versions_must_be_non_blank(cls, values: tuple[str, ...]) -> tuple[str, ...]:
        cleaned = tuple(value.strip() for value in values)
        if any(not value for value in cleaned):
            raise ValueError("compatible server versions must be non-blank")
        return cleaned

    @field_validator("allowed_execution_options")
    @classmethod
    def option_names_must_be_unique(cls, values: tuple[str, ...]) -> tuple[str, ...]:
        cleaned = tuple(value.strip() for value in values)
        if any(not value for value in cleaned):
            raise ValueError("execution option names must be non-blank")
        if len(set(cleaned)) != len(cleaned):
            raise ValueError("execution option names must be unique")
        return cleaned

    @model_validator(mode="after")
    def enabled_requires_verification(self) -> "StrategySpec":
        if self.enabled and self.verification_status is not StrategyStatus.VERIFIED:
            raise ValueError("an enabled strategy must have verification_status=VERIFIED")
        if self.verification_status is StrategyStatus.VERIFIED and not self.enabled:
            # A verified strategy may be deliberately disabled; this is not an
            # error, but callers can see it only through explicit registry APIs.
            pass
        return self


class ApplicabilityResult(BaseModel):
    """Why a strategy is, or is not, applicable to the current query/state."""

    model_config = ConfigDict(extra="forbid")

    strategy_id: str = Field(min_length=1)
    applicable: bool
    reasons: list[str] = Field(min_length=1)
    missing_features: list[str] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)

    @model_validator(mode="after")
    def reasons_are_explainable(self) -> "ApplicabilityResult":
        if any(not reason.strip() for reason in self.reasons):
            raise ValueError("applicability reasons must be non-blank")
        if not self.applicable and not any(
            reason.strip() for reason in self.reasons
        ):
            raise ValueError("a non-applicable strategy needs a blocking reason")
        return self


class CandidateContext(BaseModel):
    """Context available when a strategy builds a candidate.

    The context is data only.  A strategy must not execute the candidate while
    building it.
    """

    model_config = ConfigDict(extra="forbid", arbitrary_types_allowed=True)

    query_id: str = Field(min_length=1)
    analysis: QueryAnalysisResult | None = None
    system_state: SystemSample | None = None
    server_version: str | None = None
    database_model: str | None = None


class CandidatePlan(BaseModel):
    """A concrete candidate submitted to the validator.

    ``candidate_sql`` is executable SQL text.  A strategy that only changes
    execution options may keep it identical to the original SQL.
    """

    model_config = ConfigDict(extra="forbid")

    strategy_id: str = Field(min_length=1)
    strategy_version: str = Field(min_length=1)
    candidate_sql: str = Field(min_length=1)
    execution_options: dict[str, str | int | float | bool] = Field(
        default_factory=dict
    )
    expected_plan_effect: str | None = None
    warnings: list[str] = Field(default_factory=list)

    @field_validator("candidate_sql")
    @classmethod
    def candidate_sql_must_not_be_blank(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("candidate_sql must not be blank")
        return value


class ValidationContext(BaseModel):
    """Read-only capability context for candidate validation.

    Validation never executes SQL.  Unknown server version/model information is
    explicit; it is never replaced with a guessed version.
    """

    model_config = ConfigDict(extra="forbid", arbitrary_types_allowed=True)

    query_id: str = Field(min_length=1)
    analysis: QueryAnalysisResult | None = None
    server_version: str | None = None
    database_model: str | None = None


class ValidationCheck(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str = Field(min_length=1, max_length=100)
    status: ValidationCheckStatus
    detail: str = Field(min_length=1, max_length=2000)


class ValidationResult(BaseModel):
    """Fail-closed validation result for one candidate plan."""

    model_config = ConfigDict(extra="forbid")

    status: ValidationStatus
    checks: list[ValidationCheck] = Field(min_length=1)

    @model_validator(mode="after")
    def status_matches_checks(self) -> "ValidationResult":
        failed = [
            check
            for check in self.checks
            if check.status is ValidationCheckStatus.FAILED
        ]
        if self.status is ValidationStatus.PASSED and failed:
            raise ValueError("PASSED validation cannot contain failed checks")
        if self.status is ValidationStatus.FAILED and not failed:
            raise ValueError("FAILED validation needs at least one failed check")
        return self

    @classmethod
    def from_checks(cls, checks: list[ValidationCheck]) -> "ValidationResult":
        status = (
            ValidationStatus.FAILED
            if any(check.status is ValidationCheckStatus.FAILED for check in checks)
            else ValidationStatus.PASSED
        )
        return cls(status=status, checks=checks)

    @classmethod
    def combine(cls, *results: "ValidationResult") -> "ValidationResult":
        """Combine independent validation results without dropping checks.

        Plugin validation and registry-owned central validation are both
        required.  Concatenating checks preserves every failure, so a passed
        plugin result cannot mask a failed central check (or vice versa).
        """

        if not results:
            raise ValueError("at least one validation result is required")
        return cls.from_checks(
            [check for result in results for check in result.checks]
        )

    @property
    def passed(self) -> bool:
        return self.status is ValidationStatus.PASSED

    @property
    def failed_checks(self) -> list[ValidationCheck]:
        return [
            check
            for check in self.checks
            if check.status is ValidationCheckStatus.FAILED
        ]

    @property
    def warning_checks(self) -> list[ValidationCheck]:
        return [
            check
            for check in self.checks
            if check.status is ValidationCheckStatus.WARNING
        ]

    def error_summary(self) -> str:
        if self.passed:
            return "validation passed"
        return "; ".join(
            f"{check.name}: {check.detail}" for check in self.failed_checks
        )
