"""Fail-closed candidate validation tests."""

from __future__ import annotations

import pytest

from tsdb_optimizer.agents import QueryAnalysisAgent
from tsdb_optimizer.parsing import SQLParserAdapter
from tsdb_optimizer.strategies import (
    CandidatePlan,
    StrategyStatus,
    StrategyValidator,
    ValidationCheck,
    ValidationCheckStatus,
    ValidationContext,
    ValidationResult,
    ValidationStatus,
)
from phase4_support import (
    PARSER_UNSUPPORTED_SQL,
    TEST_SQL,
    make_test_only_spec,
)

REWRITTEN_SQL = f"{TEST_SQL} LIMIT 5"


def _spec(strategy_id: str = "TEST_ONLY_VALIDATED", **kwargs):
    return make_test_only_spec(strategy_id, **kwargs)


def _candidate(
    *,
    sql: str = TEST_SQL,
    strategy_id: str = "TEST_ONLY_VALIDATED",
    version: str = "0.0.1",
    options: dict | None = None,
    plan_effect: str | None = None,
) -> CandidatePlan:
    return CandidatePlan(
        strategy_id=strategy_id,
        strategy_version=version,
        candidate_sql=sql,
        execution_options=options or {},
        expected_plan_effect=plan_effect,
    )


def _context(
    *,
    server_version: str | None = "2.0.6",
    database_model: str | None = "tree",
    analysis_sql: str = TEST_SQL,
    query_id: str = "q-validator",
) -> ValidationContext:
    analysis = QueryAnalysisAgent().analyze(analysis_sql, query_id=query_id)
    return ValidationContext(
        query_id=query_id,
        analysis=analysis,
        server_version=server_version,
        database_model=database_model,
    )


def _failed_names(result: ValidationResult) -> set[str]:
    return {check.name for check in result.failed_checks}


def test_valid_non_native_candidate_passes_with_known_environment() -> None:
    result = StrategyValidator().validate(
        _spec(),
        TEST_SQL,
        _candidate(),
        _context(),
    )
    assert result.status is ValidationStatus.PASSED
    assert result.failed_checks == []


@pytest.mark.parametrize(
    ("server_version", "database_model", "check_name"),
    [
        (None, "tree", "server_version_compatibility"),
        ("2.0.6", None, "database_model_compatibility"),
        ("2.0.7", "tree", "server_version_compatibility"),
        ("2.0.6", "table", "database_model_compatibility"),
    ],
)
def test_non_native_requires_known_compatible_environment(
    server_version: str | None,
    database_model: str | None,
    check_name: str,
) -> None:
    result = StrategyValidator().validate(
        _spec(),
        TEST_SQL,
        _candidate(),
        _context(server_version=server_version, database_model=database_model),
    )
    assert result.status is ValidationStatus.FAILED
    assert check_name in _failed_names(result)


def test_exact_sql_non_native_candidate_passes_outside_parser_subset() -> None:
    result = StrategyValidator().validate(
        _spec(),
        PARSER_UNSUPPORTED_SQL,
        _candidate(sql=PARSER_UNSUPPORTED_SQL),
        _context(analysis_sql=PARSER_UNSUPPORTED_SQL),
    )
    assert result.status is ValidationStatus.PASSED
    check = next(
        check for check in result.checks if check.name == "candidate_parser_scope"
    )
    assert check.status is ValidationCheckStatus.PASSED
    assert "EXACT_SQL_NO_REWRITE" in check.detail
    assert "parser coverage is not required" in check.detail


def test_exact_sql_non_native_candidate_passes_without_analysis() -> None:
    result = StrategyValidator().validate(
        _spec(),
        TEST_SQL,
        _candidate(),
        ValidationContext(
            query_id="q-validator",
            server_version="2.0.6",
            database_model="tree",
        ),
    )
    assert result.status is ValidationStatus.PASSED
    check = next(
        check for check in result.checks if check.name == "candidate_parser_scope"
    )
    assert check.status is ValidationCheckStatus.PASSED
    assert "EXACT_SQL_NO_REWRITE" in check.detail


def test_rewritten_candidate_without_parser_fails_closed() -> None:
    result = StrategyValidator().validate(
        _spec(),
        TEST_SQL,
        _candidate(sql=REWRITTEN_SQL),
        _context(),
    )
    assert result.status is ValidationStatus.FAILED
    assert any(
        check.name == "candidate_parser_scope"
        and "requires a parser" in check.detail
        for check in result.failed_checks
    )


def test_rewritten_candidate_with_parser_in_verified_subset_passes() -> None:
    result = StrategyValidator(parser=SQLParserAdapter()).validate(
        _spec(),
        TEST_SQL,
        _candidate(sql=REWRITTEN_SQL),
        _context(),
    )
    assert result.status is ValidationStatus.PASSED


def test_rewritten_candidate_outside_parser_subset_fails() -> None:
    unsupported_rewrite = f"{PARSER_UNSUPPORTED_SQL} LIMIT 5"
    result = StrategyValidator(parser=SQLParserAdapter()).validate(
        _spec(),
        TEST_SQL,
        _candidate(sql=unsupported_rewrite),
        _context(),
    )
    assert result.status is ValidationStatus.FAILED
    assert "candidate_parser_scope" in _failed_names(result)


def test_parser_exception_fails_closed() -> None:
    class BrokenParser:
        def parse(self, sql: str):
            del sql
            raise RuntimeError("parser exploded")

    result = StrategyValidator(parser=BrokenParser()).validate(  # type: ignore[arg-type]
        _spec(),
        TEST_SQL,
        _candidate(sql=REWRITTEN_SQL),
        _context(),
    )
    assert result.status is ValidationStatus.FAILED
    assert "candidate_parser_scope" in _failed_names(result)


def test_unregistered_execution_options_fail() -> None:
    result = StrategyValidator().validate(
        _spec(),
        TEST_SQL,
        _candidate(options={"unknown_option": 1}),
        _context(),
    )
    assert result.status is ValidationStatus.FAILED
    assert any(
        check.name == "execution_options" and "unregistered" in check.detail
        for check in result.failed_checks
    )


def test_declared_but_unimplemented_execution_options_fail() -> None:
    result = StrategyValidator().validate(
        _spec(allowed_execution_options=["fetch_size"]),
        TEST_SQL,
        _candidate(options={"fetch_size": 1000}),
        _context(),
    )
    assert result.status is ValidationStatus.FAILED
    assert any(
        check.name == "execution_options" and "not implemented" in check.detail
        for check in result.failed_checks
    )


def test_declared_plan_effect_fails_because_no_verifier_exists() -> None:
    result = StrategyValidator().validate(
        _spec(plan_effect_expectation="LIMIT pushdown"),
        TEST_SQL,
        _candidate(plan_effect="LIMIT pushdown"),
        _context(),
    )
    assert result.status is ValidationStatus.FAILED
    assert any(
        check.name == "plan_effect_contract" and "no verified" in check.detail
        for check in result.failed_checks
    )


def test_unregistered_plan_effect_claim_fails() -> None:
    result = StrategyValidator().validate(
        _spec(),
        TEST_SQL,
        _candidate(plan_effect="LIMIT pushdown"),
        _context(),
    )
    assert result.status is ValidationStatus.FAILED
    assert any(
        check.name == "plan_effect_contract" and "declares none" in check.detail
        for check in result.failed_checks
    )


def test_identity_and_version_mismatch_fail() -> None:
    validator = StrategyValidator()
    wrong_id = validator.validate(
        _spec(),
        TEST_SQL,
        _candidate(strategy_id="OTHER_STRATEGY"),
        _context(),
    )
    assert wrong_id.status is ValidationStatus.FAILED
    assert "strategy_identity" in _failed_names(wrong_id)

    wrong_version = validator.validate(
        _spec(),
        TEST_SQL,
        _candidate(version="9.9.9"),
        _context(),
    )
    assert wrong_version.status is ValidationStatus.FAILED
    assert "strategy_identity" in _failed_names(wrong_version)


def test_native_passthrough_without_analysis_is_a_warning_not_a_failure() -> None:
    native = StrategyValidator().validate(
        _spec(
            strategy_id="S0_NATIVE",
            is_baseline=True,
        ),
        TEST_SQL,
        _candidate(strategy_id="S0_NATIVE"),
        ValidationContext(query_id="q", server_version=None, database_model=None),
    )
    # The test-only spec is not the audited Native spec, but it is marked as the
    # baseline here to exercise the no-analysis passthrough rule.
    assert native.status is ValidationStatus.PASSED
    assert any(
        check.name == "candidate_parser_scope"
        and check.status is ValidationCheckStatus.WARNING
        for check in native.checks
    )


def test_validation_result_cannot_hide_failed_checks() -> None:
    with pytest.raises(ValueError, match="PASSED validation"):
        ValidationResult(
            status=ValidationStatus.PASSED,
            checks=[
                ValidationCheck(
                    name="x",
                    status=ValidationCheckStatus.FAILED,
                    detail="failed",
                )
            ],
        )
    with pytest.raises(ValueError, match="needs at least one failed"):
        ValidationResult(
            status=ValidationStatus.FAILED,
            checks=[
                ValidationCheck(
                    name="x",
                    status=ValidationCheckStatus.PASSED,
                    detail="passed",
                )
            ],
        )


def test_strategy_status_values_are_audited() -> None:
    assert {status.value for status in StrategyStatus} == {
        "VERIFIED",
        "NOT_VERIFIED",
        "UNSUPPORTED",
    }
