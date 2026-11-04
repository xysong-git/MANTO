"""S1_REPEAT_CACHE strategy: epoch gate, SQL subset and exact identity."""

from __future__ import annotations

import pytest

from tsdb_optimizer.agents import QueryAnalysisAgent
from tsdb_optimizer.strategies import (
    CACHE_EPOCH_OPTION,
    CACHE_STRATEGY_ID,
    CandidateContext,
    CandidatePlan,
    RepeatCacheStrategy,
    SelectionPolicy,
    StrategyStatus,
    ValidationContext,
    check_cacheable_sql,
    repeat_cache_strategy_spec,
)
from phase4_support import PARSER_UNSUPPORTED_SQL

EPOCH = "epoch-2026-11-01"
SQL = "SELECT LAST s_333, s_433 FROM root.dbs.g_0.*;"
_Q1 = "SELECT s_333 FROM root.dbs.g_0.* WHERE time = 1640971763900;"
_Q7_INTERVAL = (
    "SELECT count(s_333), count(s_433) FROM root.dbs.g_0.d_81, "
    "root.dbs.g_0.d_82 "
    "GROUP BY ([1640966400000, 1640972449000), 20000ms);"
)

_Q7 = (
    "SELECT count(s_1), sum(s_2) FROM root.dbs.g_0.* "
    "GROUP BY (time, 1h);"
)
_Q9 = (
    "SELECT s_130 FROM root.dbs.g_0.* "
    "ORDER BY time DESC LIMIT 1;"
)


def _analysis(sql: str = SQL):
    return QueryAnalysisAgent().analyze(sql, query_id="q-s1")


def _context() -> CandidateContext:
    return CandidateContext(query_id="q-s1", analysis=_analysis())


def _validation_context() -> ValidationContext:
    return ValidationContext(
        query_id="q-s1",
        analysis=_analysis(),
        server_version="2.0.6",
        database_model="tree",
    )


def _strategy(epoch: str | None = EPOCH) -> RepeatCacheStrategy:
    return RepeatCacheStrategy(cache_epoch=epoch)


def test_spec_is_verified_repeat_policy_and_non_baseline() -> None:
    spec = repeat_cache_strategy_spec()
    assert spec.strategy_id == CACHE_STRATEGY_ID == "S1_REPEAT_CACHE"
    assert spec.enabled is True
    assert spec.verification_status is StrategyStatus.VERIFIED
    assert spec.is_baseline is False
    assert spec.selection_policy is SelectionPolicy.REPEAT_CACHE
    assert spec.allowed_execution_options == (CACHE_EPOCH_OPTION,)
    assert spec.compatible_server_versions == ("2.0.6",)
    assert spec.plan_effect_expectation is None
    assert any("process-local" in item for item in spec.known_limitations)
    assert any("epoch" in item for item in spec.known_limitations)


@pytest.mark.parametrize("sql", [SQL, _Q1, _Q7, _Q7_INTERVAL, _Q9])
def test_workload_shapes_inside_the_verified_subset_are_cacheable(sql: str) -> None:
    decision = check_cacheable_sql(sql)
    assert decision.cacheable is True, decision.reason
    assert decision.cleaned_sql is not None
    assert decision.cleaned_sql.startswith("SELECT")


@pytest.mark.parametrize(
    ("sql", "expected"),
    [
        ("SELECT now() FROM root.dbs.g_0.*", "not in the verified"),
        ("SELECT rand() FROM root.dbs.g_0.*", "not in the verified"),
        ("SELECT uuid() FROM root.dbs.g_0.*", "outside the deterministic"),
        (
            "SELECT s_1 FROM root.dbs.g_0.* WHERE s_1 = 'x'",
            "STRING",
        ),
        (
            "SELECT s_1 FROM root.dbs.g_0.* WHERE s_1 = ?",
            "PLACEHOLDER",
        ),
        (
            "SELECT s_1 FROM root.a.g_0.* JOIN root.b.g_0.*",
            "JOIN",
        ),
        (
            "SELECT s_1 FROM root.a.g_0.* UNION SELECT s_1 FROM root.b.g_0.*",
            "UNION",
        ),
        (
            "SELECT CAST(s_1 AS INT) FROM root.dbs.g_0.*",
            "not in the verified",
        ),
        (
            "SELECT last_value(s_1) FROM root.dbs.g_0.*",
            "not in the verified",
        ),
        ("DELETE FROM root.dbs.g_0.*", "read-only guard"),
        ("SELECT s_1 FROM root.dbs.g_0.*; DROP TABLE root.dbs", "read-only guard"),
    ],
)
def test_outside_subset_is_rejected_rather_than_guessed(
    sql: str, expected: str
) -> None:
    decision = check_cacheable_sql(sql)
    assert decision.cacheable is False
    assert expected in decision.reason


@pytest.mark.parametrize(
    "sql",
    [
        "SELECT s_130 FROM root.test.g_0.d_0 WHERE",
        "SELECT s_130 FROM root.test.g_0.d_0 ORDER BY",
        "SELECT s_130 FROM root.test.g_0.d_0 LIMIT",
        "SELECT s_130 FROM root.test.g_0.d_0 WHERE time BETWEEN 1 AND",
        "SELECT s_130 FROM root.dbs.g_0.* WHERE",
        "SELECT s_130 FROM root.dbs.g_0.* ORDER BY",
    ],
)
def test_unverified_sql_is_not_cacheable(sql: str) -> None:
    decision = check_cacheable_sql(sql)
    assert decision.cacheable is False
    assert "read-only guard rejected" in decision.reason


def test_without_epoch_the_strategy_is_not_applicable() -> None:
    applicability = _strategy(None).is_applicable(_analysis(), None)
    assert applicability.applicable is False
    assert CACHE_EPOCH_OPTION in applicability.missing_features
    assert "no server-side invalidation" in "; ".join(applicability.reasons)


def test_with_epoch_and_supported_sql_the_strategy_is_applicable() -> None:
    applicability = _strategy().is_applicable(_analysis(), None)
    assert applicability.applicable is True
    assert any(EPOCH in reason for reason in applicability.reasons)


def test_non_cacheable_sql_is_not_applicable_even_with_an_epoch() -> None:
    analysis = QueryAnalysisAgent().analyze(
        "SELECT now() FROM root.dbs.g_0.*", query_id="q-s1"
    )
    applicability = _strategy().is_applicable(analysis, None)
    assert applicability.applicable is False
    assert "not in the verified" in "; ".join(applicability.reasons)


def test_missing_analysis_is_not_applicable() -> None:
    applicability = _strategy().is_applicable(None, None)
    assert applicability.applicable is False
    assert "query_analysis" in applicability.missing_features


def test_parser_unsupported_sql_still_uses_the_exact_original_text() -> None:
    analysis = QueryAnalysisAgent().analyze(PARSER_UNSUPPORTED_SQL, query_id="q-s1")
    assert analysis.parse_status.value != "SUCCESS"
    applicability = _strategy().is_applicable(analysis, None)
    assert applicability.applicable is True
    assert any("without rewriting" in warning for warning in applicability.warnings)
    candidate = _strategy().build_candidate(PARSER_UNSUPPORTED_SQL, _context())
    assert candidate.candidate_sql == PARSER_UNSUPPORTED_SQL


def test_candidate_is_exact_cleaned_sql_plus_only_the_epoch_option() -> None:
    candidate = _strategy().build_candidate(SQL, _context())
    assert candidate.strategy_id == CACHE_STRATEGY_ID
    assert candidate.candidate_sql == SQL.rstrip(";")
    assert candidate.execution_options == {CACHE_EPOCH_OPTION: EPOCH}
    assert candidate.expected_plan_effect is None


def test_build_without_epoch_or_outside_subset_fails() -> None:
    with pytest.raises(ValueError, match="without cache_epoch"):
        _strategy(None).build_candidate(SQL, _context())
    with pytest.raises(ValueError, match="not cacheable"):
        _strategy().build_candidate(
            "SELECT now() FROM root.dbs.g_0.*", _context()
        )


def test_validation_passes_for_the_exact_candidate() -> None:
    strategy = _strategy()
    candidate = strategy.build_candidate(SQL, _context())
    validation = strategy.validate(SQL, candidate, _validation_context())
    assert validation.passed is True
    names = {check.name for check in validation.checks}
    assert {
        "cache_epoch_configured",
        "cache_option_identity",
        "cache_sql_identity",
        "cacheable_sql_subset",
    } <= names


def test_validation_rejects_a_different_candidate_sql() -> None:
    strategy = _strategy()
    candidate = CandidatePlan(
        strategy_id=CACHE_STRATEGY_ID,
        strategy_version=strategy.spec.version,
        candidate_sql=f"{SQL} LIMIT 1",
        execution_options={CACHE_EPOCH_OPTION: EPOCH},
    )
    validation = strategy.validate(SQL, candidate, _validation_context())
    assert validation.passed is False
    failed = {check.name for check in validation.failed_checks}
    assert "cache_sql_identity" in failed


def test_validation_rejects_a_tampered_epoch_option() -> None:
    strategy = _strategy()
    candidate = CandidatePlan(
        strategy_id=CACHE_STRATEGY_ID,
        strategy_version=strategy.spec.version,
        candidate_sql=SQL,
        execution_options={CACHE_EPOCH_OPTION: "epoch-other"},
    )
    validation = strategy.validate(SQL, candidate, _validation_context())
    assert validation.passed is False
    failed = {check.name for check in validation.failed_checks}
    assert "cache_option_identity" in failed
    assert "execution options" in validation.error_summary()


def test_validation_rejects_a_missing_epoch_option() -> None:
    strategy = _strategy()
    candidate = CandidatePlan(
        strategy_id=CACHE_STRATEGY_ID,
        strategy_version=strategy.spec.version,
        candidate_sql=SQL,
        execution_options={},
    )
    validation = strategy.validate(SQL, candidate, _validation_context())
    assert validation.passed is False
    assert "cache_option_identity" in {
        check.name for check in validation.failed_checks
    }


def test_validation_without_configured_epoch_fails_closed() -> None:
    strategy = _strategy(None)
    candidate = CandidatePlan(
        strategy_id=CACHE_STRATEGY_ID,
        strategy_version=strategy.spec.version,
        candidate_sql=SQL,
        execution_options={},
    )
    validation = strategy.validate(SQL, candidate, _validation_context())
    assert validation.passed is False
    assert "cache_epoch_configured" in {
        check.name for check in validation.failed_checks
    }


def test_constructor_rejects_a_non_repeat_spec() -> None:
    spec = repeat_cache_strategy_spec().model_copy(
        update={"selection_policy": SelectionPolicy.LOWEST_COST}
    )
    with pytest.raises(ValueError, match="repeat-policy spec"):
        RepeatCacheStrategy(cache_epoch=EPOCH, spec=spec)


def test_constructor_normalizes_the_epoch() -> None:
    strategy = RepeatCacheStrategy(cache_epoch=f"  {EPOCH}  ")
    assert strategy.cache_epoch == EPOCH
    with pytest.raises(ValueError, match="blank"):
        RepeatCacheStrategy(cache_epoch="   ")
