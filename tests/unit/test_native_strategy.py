"""S0 Native semantics and validator behavior for the baseline."""

from __future__ import annotations

import pytest

from tsdb_optimizer.agents import QueryAnalysisAgent
from tsdb_optimizer.strategies import (
    CandidateContext,
    CandidatePlan,
    NativeStrategy,
    StrategyStatus,
    StrategyValidator,
    ValidationContext,
    ValidationStatus,
    native_strategy_spec,
)
from phase4_support import PARSER_UNSUPPORTED_SQL, TEST_SQL


def _analysis(sql: str = TEST_SQL, query_id: str = "q-native"):
    return QueryAnalysisAgent().analyze(sql, query_id=query_id)


def _contexts(sql: str = TEST_SQL, query_id: str = "q-native"):
    analysis = _analysis(sql, query_id)
    return (
        CandidateContext(query_id=query_id, analysis=analysis),
        ValidationContext(query_id=query_id, analysis=analysis),
    )


def test_native_spec_is_verified_baseline_without_plan_claims() -> None:
    spec = native_strategy_spec()
    assert spec.strategy_id == "S0_NATIVE"
    assert spec.is_baseline is True
    assert spec.enabled is True
    assert spec.verification_status is StrategyStatus.VERIFIED
    assert spec.plan_effect_expectation is None
    assert spec.allowed_execution_options == ()
    assert spec.compatible_server_versions == ("2.0.6",)


def test_native_is_not_applicable_without_analysis() -> None:
    result = NativeStrategy().is_applicable(None, None)
    assert result.applicable is False
    assert "query_analysis" in result.missing_features


def test_native_is_applicable_for_supported_and_unsupported_parser_subsets() -> None:
    strategy = NativeStrategy()
    supported = strategy.is_applicable(_analysis(), None)
    assert supported.applicable is True
    assert supported.warnings == []

    unsupported = strategy.is_applicable(_analysis(PARSER_UNSUPPORTED_SQL), None)
    assert unsupported.applicable is True
    assert any("does not cover" in warning for warning in unsupported.warnings)


def test_native_candidate_is_the_original_sql_unchanged() -> None:
    strategy = NativeStrategy()
    context, _ = _contexts()
    candidate = strategy.build_candidate(TEST_SQL, context)
    assert candidate.strategy_id == "S0_NATIVE"
    assert candidate.candidate_sql == TEST_SQL
    assert candidate.execution_options == {}
    assert candidate.expected_plan_effect is None


def test_native_candidate_generation_rejects_unsafe_sql() -> None:
    context, _ = _contexts()
    with pytest.raises(Exception, match="SELECT|read-only"):
        NativeStrategy().build_candidate("DELETE FROM root.test.g_0.d_0", context)


def test_native_validation_passes_with_unknown_server_version_and_model() -> None:
    strategy = NativeStrategy()
    context, validation_context = _contexts()
    candidate = strategy.build_candidate(TEST_SQL, context)
    result = strategy.validate(TEST_SQL, candidate, validation_context)
    assert result.status is ValidationStatus.PASSED
    names = {check.name for check in result.warning_checks}
    assert "server_version_compatibility" in names
    assert "database_model_compatibility" in names
    assert result.failed_checks == []


def test_native_validation_rejects_known_version_and_model_mismatch() -> None:
    strategy = NativeStrategy()
    context, _ = _contexts()
    candidate = strategy.build_candidate(TEST_SQL, context)

    version_mismatch = strategy.validate(
        TEST_SQL,
        candidate,
        ValidationContext(
            query_id="q-native",
            analysis=context.analysis,
            server_version="2.0.7",
            database_model="tree",
        ),
    )
    assert version_mismatch.status is ValidationStatus.FAILED
    assert any(
        check.name == "server_version_compatibility"
        for check in version_mismatch.failed_checks
    )

    model_mismatch = strategy.validate(
        TEST_SQL,
        candidate,
        ValidationContext(
            query_id="q-native",
            analysis=context.analysis,
            server_version="2.0.6",
            database_model="table",
        ),
    )
    assert model_mismatch.status is ValidationStatus.FAILED
    assert any(
        check.name == "database_model_compatibility"
        for check in model_mismatch.failed_checks
    )


def test_native_validation_rejects_any_sql_rewrite() -> None:
    strategy = NativeStrategy()
    context, validation_context = _contexts()
    rewritten = CandidatePlan(
        strategy_id="S0_NATIVE",
        strategy_version=strategy.spec.version,
        candidate_sql=f"{TEST_SQL} LIMIT 5",
    )
    result = strategy.validate(TEST_SQL, rewritten, validation_context)
    assert result.status is ValidationStatus.FAILED
    assert any(check.name == "candidate_parser_scope" for check in result.failed_checks)


def test_validator_rejects_unsafe_original_and_candidate() -> None:
    validator = StrategyValidator()
    spec = native_strategy_spec()
    safe_candidate = CandidatePlan(
        strategy_id="S0_NATIVE",
        strategy_version=spec.version,
        candidate_sql=TEST_SQL,
    )
    unsafe_original = validator.validate(
        spec,
        "DELETE FROM root.test.g_0.d_0",
        safe_candidate,
        ValidationContext(query_id="q"),
    )
    assert unsafe_original.status is ValidationStatus.FAILED
    assert {check.name for check in unsafe_original.failed_checks} >= {
        "original_read_only",
        "candidate_parser_scope",
    }

    unsafe_candidate = validator.validate(
        spec,
        TEST_SQL,
        CandidatePlan(
            strategy_id="S0_NATIVE",
            strategy_version=spec.version,
            candidate_sql="DELETE FROM root.test.g_0.d_0",
        ),
        ValidationContext(query_id="q"),
    )
    assert unsafe_candidate.status is ValidationStatus.FAILED
    assert any(
        check.name == "candidate_read_only"
        for check in unsafe_candidate.failed_checks
    )
