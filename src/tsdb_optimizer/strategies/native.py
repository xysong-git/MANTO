"""S0_NATIVE: the verified pass-through baseline and fallback strategy."""

from __future__ import annotations

from ..errors import UnsafeSQLError
from ..schemas.monitoring import SystemSample
from ..schemas.query_features import ParseStatus, QueryAnalysisResult
from ..sql_guard import validate_readonly_sql
from .models import (
    ApplicabilityResult,
    CandidateContext,
    CandidatePlan,
    StrategySpec,
    StrategyStatus,
    ValidationContext,
    ValidationResult,
)
from .validator import StrategyValidator


def native_strategy_spec() -> StrategySpec:
    """Return the audited S0 declaration used by the registry."""

    return StrategySpec(
        strategy_id="S0_NATIVE",
        name="Native SQL baseline and fallback",
        database_model="tree",
        compatible_server_versions=("2.0.6",),
        enabled=True,
        verification_status=StrategyStatus.VERIFIED,
        is_baseline=True,
        implementation_class="tsdb_optimizer.strategies.native.NativeStrategy",
        applicability_rules={
            "sql": "single, read-only SELECT",
            "rewrite": "none",
            "execution_count": "exactly one request per attempt",
        },
        execution_control="execute original SQL once via tree Session",
        semantic_test_suite="tests/unit/test_native_strategy.py",
        plan_effect_expectation=None,
        allowed_execution_options=(),
        known_limitations=(
            "no optimization",
            "client fetch_mode is not varied by this strategy",
            "verified against apache-iotdb 2.0.6 tree model",
        ),
        version="0.1.0",
    )


class NativeStrategy:
    """Read-only pass-through strategy.

    It changes neither SQL nor execution options.  The server remains the
    authority on syntax and semantics; Agent 1 parser unsupported status does
    not block S0, which is important for the verified Native fallback.
    """

    def __init__(
        self,
        spec: StrategySpec | None = None,
        validator: StrategyValidator | None = None,
    ):
        self.spec = spec or native_strategy_spec()
        if self.spec.strategy_id != "S0_NATIVE" or not self.spec.is_baseline:
            raise ValueError("NativeStrategy requires the S0_NATIVE baseline spec")
        self.validator = validator or StrategyValidator()

    def is_applicable(
        self,
        query_features: QueryAnalysisResult | None,
        system_state: SystemSample | None,
    ) -> ApplicabilityResult:
        del system_state  # S0 does not depend on a system snapshot.
        if query_features is None:
            return ApplicabilityResult(
                strategy_id=self.spec.strategy_id,
                applicable=False,
                reasons=["no Agent-1 analysis is available for the query"],
                missing_features=["query_analysis"],
            )
        sql = query_features.parse.sql
        try:
            validate_readonly_sql(sql)
        except UnsafeSQLError as exc:
            return ApplicabilityResult(
                strategy_id=self.spec.strategy_id,
                applicable=False,
                reasons=[f"read-only SQL guard rejected the query: {exc}"],
            )
        warnings: list[str] = []
        if query_features.parse_status is not ParseStatus.SUCCESS:
            warnings.append(
                "Agent-1 parser does not cover this SQL subset; S0 still passes the "
                "original SQL to the server without rewriting it"
            )
        return ApplicabilityResult(
            strategy_id=self.spec.strategy_id,
            applicable=True,
            reasons=[
                "single read-only SELECT",
                "Native pass-through changes no SQL and claims no plan effect",
            ],
            warnings=warnings,
        )

    def build_candidate(
        self,
        original_sql: str,
        context: CandidateContext,
    ) -> CandidatePlan:
        del context  # The Native candidate depends only on the original SQL.
        cleaned = validate_readonly_sql(original_sql)
        return CandidatePlan(
            strategy_id=self.spec.strategy_id,
            strategy_version=self.spec.version,
            candidate_sql=cleaned,
            execution_options={},
            expected_plan_effect=None,
        )

    def validate(
        self,
        original_sql: str,
        candidate: CandidatePlan,
        context: ValidationContext,
    ) -> ValidationResult:
        return self.validator.validate(self.spec, original_sql, candidate, context)


__all__ = ["NativeStrategy", "native_strategy_spec"]
