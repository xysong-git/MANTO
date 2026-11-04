"""Uniform interface implemented by every registered strategy plugin."""

from __future__ import annotations

from typing import Protocol

from ..schemas.monitoring import SystemSample
from ..schemas.query_features import QueryAnalysisResult
from .models import (
    ApplicabilityResult,
    CandidateContext,
    CandidatePlan,
    StrategySpec,
    ValidationContext,
    ValidationResult,
)


class OptimizationStrategy(Protocol):
    """A strategy never executes SQL while deciding or validating."""

    spec: StrategySpec

    def is_applicable(
        self,
        query_features: QueryAnalysisResult | None,
        system_state: SystemSample | None,
    ) -> ApplicabilityResult:
        """Return applicability with explicit supporting/blocking reasons."""

    def build_candidate(
        self,
        original_sql: str,
        context: CandidateContext,
    ) -> CandidatePlan:
        """Build one candidate; never execute it here."""

    def validate(
        self,
        original_sql: str,
        candidate: CandidatePlan,
        context: ValidationContext,
    ) -> ValidationResult:
        """Validate without contacting IoTDB."""


__all__ = ["OptimizationStrategy"]
