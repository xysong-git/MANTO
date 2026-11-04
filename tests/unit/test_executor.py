"""Executor: one selected candidate, conservative Native fallback."""

from __future__ import annotations

import pytest

from tsdb_optimizer.agents import QueryAnalysisAgent
from tsdb_optimizer.decision import DecisionEngine, DecisionMode
from tsdb_optimizer.errors import AdapterError, ExecutionError
from tsdb_optimizer.execution import FallbackTrigger, StrategyExecutor
from tsdb_optimizer.schemas import RetryUnsafeReason
from tsdb_optimizer.schemas.execution import ExecutionStatus
from tsdb_optimizer.strategies import build_default_registry
from phase4_support import (
    ScriptedAdapter,
    SelfApprovingStrategyDouble,
    StaticCostEstimator,
    TEST_ONLY_STRATEGY_ID,
    TEST_SQL,
    PassThroughStrategyDouble,
    execution_result,
    make_test_only_spec,
)

QUERY_ID = "q-executor"
SERVER_VERSION = "2.0.6"
DATABASE_MODEL = "tree"


def _analysis(query_id: str = QUERY_ID):
    return QueryAnalysisAgent().analyze(TEST_SQL, query_id=query_id)


def _fast_decision(*, plugin=None, query_id: str = QUERY_ID):
    registry = build_default_registry(allow_uncertified_for_testing=True)
    registry.register(
        plugin or PassThroughStrategyDouble(spec=make_test_only_spec())
    )
    engine = DecisionEngine(
        registry,
        cost_estimator=StaticCostEstimator(
            {"S0_NATIVE": 10.0, TEST_ONLY_STRATEGY_ID: 1.0}
        ),
    )
    return engine.decide(
        query_id=query_id,
        original_sql=TEST_SQL,
        mode=DecisionMode.RULE_BASED,
        analysis=_analysis(query_id),
        server_version=SERVER_VERSION,
        database_model=DATABASE_MODEL,
    )


def _baseline_decision(*, query_id: str = QUERY_ID):
    return DecisionEngine(build_default_registry()).decide(
        query_id=query_id,
        original_sql=TEST_SQL,
        mode=DecisionMode.BASELINE,
        analysis=_analysis(query_id),
        server_version=SERVER_VERSION,
        database_model=DATABASE_MODEL,
    )


def _execute(decision, adapter):
    return StrategyExecutor(
        adapter=adapter,
        registry=build_default_registry()
        if decision.selected_strategy_id == "S0_NATIVE"
        else _decision_registry(decision),
    ).execute(
        decision,
        analysis=_analysis(decision.query_id),
        server_version=SERVER_VERSION,
        database_model=DATABASE_MODEL,
    )


def _decision_registry(decision):
    # Reconstruct the same test-only registry used to produce the decision.
    registry = build_default_registry(allow_uncertified_for_testing=True)
    registry.register(PassThroughStrategyDouble(spec=make_test_only_spec()))
    return registry


def _success(query_id: str, strategy_id: str):
    return execution_result(query_id=query_id, strategy_id=strategy_id)


def _timeout(query_id: str, strategy_id: str):
    return execution_result(
        query_id=query_id,
        strategy_id=strategy_id,
        status=ExecutionStatus.ERROR,
        timed_out=True,
        request_dispatched=True,
        request_completed=False,
        retry_unsafe_reason=RetryUnsafeReason.TIMEOUT_OR_UNKNOWN_SERVER_STATE,
    )


def _not_dispatched_error(query_id: str, strategy_id: str):
    return execution_result(
        query_id=query_id,
        strategy_id=strategy_id,
        status=ExecutionStatus.ERROR,
        request_dispatched=False,
        request_completed=False,
    )


def _unconfirmed_error(query_id: str, strategy_id: str):
    return execution_result(
        query_id=query_id,
        strategy_id=strategy_id,
        status=ExecutionStatus.ERROR,
        request_dispatched=True,
        request_completed=False,
        retry_unsafe_reason=RetryUnsafeReason.REQUEST_COMPLETION_UNCONFIRMED,
    )


def _retry_safe_error(query_id: str, strategy_id: str):
    return execution_result(
        query_id=query_id,
        strategy_id=strategy_id,
        status=ExecutionStatus.ERROR,
        request_dispatched=True,
        request_completed=True,
        retry_safe=True,
    )


def test_executor_executes_selected_candidate_exactly_once() -> None:
    decision = _fast_decision()
    adapter = ScriptedAdapter([_success(QUERY_ID, TEST_ONLY_STRATEGY_ID)])

    report = _execute(decision, adapter)

    assert len(adapter.calls) == 1
    request, preview_rows, strategy_id = adapter.calls[0]
    assert request.sql == decision.candidate_sql
    assert preview_rows == 20
    assert strategy_id == TEST_ONLY_STRATEGY_ID
    assert "EXPLAIN" not in request.sql.upper()
    assert "ANALYZE" not in request.sql.upper()
    assert report.fallback_used is False
    assert report.fallback_trigger is FallbackTrigger.NONE
    assert report.final_status is ExecutionStatus.SUCCESS
    assert len(report.attempts) == 1
    assert report.attempts[0].attempt_index == 1
    assert report.attempts[0].is_fallback is False
    assert report.attempts[0].requested_strategy_id == TEST_ONLY_STRATEGY_ID
    assert report.total_wall_ms >= 0.0


def test_not_dispatched_failure_falls_back_to_native() -> None:
    decision = _fast_decision()
    adapter = ScriptedAdapter(
        [
            _not_dispatched_error(QUERY_ID, TEST_ONLY_STRATEGY_ID),
            _success(QUERY_ID, "S0_NATIVE"),
        ]
    )

    report = _execute(decision, adapter)

    assert len(adapter.calls) == 2
    assert adapter.calls[0][2] == TEST_ONLY_STRATEGY_ID
    assert adapter.calls[1][2] == "S0_NATIVE"
    assert adapter.calls[1][0].sql == policy_native_sql(decision)
    assert report.fallback_used is True
    assert report.fallback_trigger is FallbackTrigger.FIRST_REQUEST_NOT_DISPATCHED
    assert report.final_status is ExecutionStatus.SUCCESS
    assert [attempt.is_fallback for attempt in report.attempts] == [False, True]
    assert report.attempts[1].fallback_reason


def policy_native_sql(decision) -> str:
    return decision.original_sql


def test_explicit_retry_safety_allows_fallback() -> None:
    decision = _fast_decision()
    adapter = ScriptedAdapter(
        [
            _retry_safe_error(QUERY_ID, TEST_ONLY_STRATEGY_ID),
            _success(QUERY_ID, "S0_NATIVE"),
        ]
    )

    report = _execute(decision, adapter)

    assert len(adapter.calls) == 2
    assert report.fallback_used is True
    assert report.fallback_trigger is FallbackTrigger.FIRST_REQUEST_RETRY_SAFE
    assert report.final_status is ExecutionStatus.SUCCESS


@pytest.mark.parametrize(
    "first_result",
    [
        _timeout(QUERY_ID, TEST_ONLY_STRATEGY_ID),
        _unconfirmed_error(QUERY_ID, TEST_ONLY_STRATEGY_ID),
    ],
    ids=["timeout", "unconfirmed_completion"],
)
def test_unsafe_retry_state_blocks_fallback(first_result) -> None:
    decision = _fast_decision()
    adapter = ScriptedAdapter([first_result, _success(QUERY_ID, "S0_NATIVE")])

    report = _execute(decision, adapter)

    assert len(adapter.calls) == 1
    assert report.fallback_used is False
    assert report.fallback_trigger is FallbackTrigger.NONE
    assert report.fallback_blocked_reason
    assert report.final_status is ExecutionStatus.ERROR
    assert any(
        warning.startswith("NATIVE_FALLBACK_BLOCKED")
        for warning in report.warnings
    )


def test_baseline_failure_is_not_retried_as_itself() -> None:
    decision = _baseline_decision()
    adapter = ScriptedAdapter(
        [_unconfirmed_error(QUERY_ID, "S0_NATIVE"), _success(QUERY_ID, "S0_NATIVE")]
    )

    report = _execute(decision, adapter)

    assert len(adapter.calls) == 1
    assert report.fallback_used is False
    assert report.fallback_blocked_reason is None
    assert report.final_status is ExecutionStatus.ERROR


def test_forged_candidate_preflight_failure_falls_back_without_dispatch() -> None:
    decision = _fast_decision()
    forged_sql = f"{TEST_SQL} LIMIT 5"
    decision.candidate_sql = forged_sql
    decision.considered[0].candidate.candidate_sql = forged_sql
    adapter = ScriptedAdapter([_success(QUERY_ID, "S0_NATIVE")])

    report = _execute(decision, adapter)

    assert len(adapter.calls) == 1
    assert adapter.calls[0][2] == "S0_NATIVE"
    assert report.fallback_used is True
    assert report.fallback_trigger is FallbackTrigger.CANDIDATE_VALIDATION_FAILED
    assert report.attempts[0].is_fallback is True
    assert report.attempts[0].attempt_index == 1
    assert any(
        warning.startswith("SELECTED_CANDIDATE_PREFLIGHT_FAILED")
        for warning in report.warnings
    )


def test_executor_enforces_central_validation_on_selected_candidate() -> None:
    decision = _fast_decision()
    registry = build_default_registry(allow_uncertified_for_testing=True)
    registry.register(SelfApprovingStrategyDouble(spec=make_test_only_spec()))
    adapter = ScriptedAdapter([_success(QUERY_ID, "S0_NATIVE")])
    executor = StrategyExecutor(adapter=adapter, registry=registry)

    report = executor.execute(
        decision,
        analysis=_analysis(),
        server_version=None,
        database_model=DATABASE_MODEL,
    )

    assert len(adapter.calls) == 1
    assert adapter.calls[0][2] == "S0_NATIVE"
    assert report.fallback_used is True
    assert report.fallback_trigger is FallbackTrigger.CANDIDATE_VALIDATION_FAILED
    assert report.attempts[0].requested_strategy_id == "S0_NATIVE"
    assert any(
        warning.startswith("SELECTED_CANDIDATE_PREFLIGHT_FAILED")
        and "server version is unknown" in warning
        for warning in report.warnings
    )


def test_fallback_that_fails_central_validation_raises() -> None:
    decision = _fast_decision()
    registry = build_default_registry(allow_uncertified_for_testing=True)
    registry.register(SelfApprovingStrategyDouble(spec=make_test_only_spec()))
    adapter = ScriptedAdapter([_success(QUERY_ID, TEST_ONLY_STRATEGY_ID)])
    executor = StrategyExecutor(adapter=adapter, registry=registry)

    with pytest.raises(
        ExecutionError, match="Native fallback candidate failed validation"
    ):
        executor.execute(
            decision,
            analysis=_analysis(),
            server_version=SERVER_VERSION,
            database_model="table",
        )
    assert adapter.calls == []


def test_forged_baseline_candidate_raises_execution_error() -> None:
    decision = _baseline_decision()
    forged_sql = f"{TEST_SQL} LIMIT 5"
    decision.candidate_sql = forged_sql
    decision.considered[0].candidate.candidate_sql = forged_sql
    adapter = ScriptedAdapter([])

    with pytest.raises(ExecutionError, match="baseline candidate failed preflight"):
        _execute(decision, adapter)
    assert adapter.calls == []


def test_forged_unregistered_strategy_raises_execution_error() -> None:
    decision = _fast_decision()
    decision.selected_strategy_id = "CAND_NOT_REGISTERED"
    decision.considered[0].strategy_id = "CAND_NOT_REGISTERED"
    adapter = ScriptedAdapter([])

    with pytest.raises(ExecutionError, match="not selectable"):
        _execute(decision, adapter)
    assert adapter.calls == []


def test_adapter_strategy_label_mismatch_raises() -> None:
    decision = _fast_decision()
    adapter = ScriptedAdapter([_success(QUERY_ID, "S0_NATIVE")])
    executor = StrategyExecutor(adapter=adapter, registry=_decision_registry(decision))

    with pytest.raises(ExecutionError, match="label"):
        executor.execute(
            decision,
            analysis=_analysis(),
            server_version=SERVER_VERSION,
            database_model=DATABASE_MODEL,
        )


def test_adapter_sql_hash_mismatch_raises() -> None:
    decision = _fast_decision()
    wrong_hash = execution_result(
        query_id=QUERY_ID,
        strategy_id=TEST_ONLY_STRATEGY_ID,
        sql="SELECT s_999 FROM root.test.g_0.d_0",
    )
    adapter = ScriptedAdapter([wrong_hash])
    executor = StrategyExecutor(adapter=adapter, registry=_decision_registry(decision))

    with pytest.raises(ExecutionError, match="SQL hash"):
        executor.execute(
            decision,
            analysis=_analysis(),
            server_version=SERVER_VERSION,
            database_model=DATABASE_MODEL,
        )
    assert len(adapter.calls) == 1


def test_adapter_error_before_result_is_not_turned_into_fallback() -> None:
    decision = _fast_decision()
    adapter = ScriptedAdapter(
        [
            AdapterError("connection lost before dispatch"),
            _success(QUERY_ID, "S0_NATIVE"),
        ]
    )

    with pytest.raises(ExecutionError, match="could not execute"):
        _execute(decision, adapter)
    # Exactly one adapter call was attempted; no execution row could be
    # invented and no retry was issued after an unknown adapter failure.
    assert len(adapter.calls) == 1


def test_unexpected_adapter_exception_is_wrapped_without_retry() -> None:
    decision = _fast_decision()
    adapter = ScriptedAdapter(
        [
            RuntimeError("adapter bug after dispatch"),
            _success(QUERY_ID, "S0_NATIVE"),
        ]
    )

    with pytest.raises(
        ExecutionError, match="unexpected error.*RuntimeError: adapter bug"
    ) as raised:
        _execute(decision, adapter)

    assert isinstance(raised.value.__cause__, RuntimeError)
    assert len(adapter.calls) == 1


def test_invalid_executor_arguments_are_rejected() -> None:
    decision = _fast_decision()
    executor = StrategyExecutor(
        adapter=ScriptedAdapter([]),
        registry=_decision_registry(decision),
    )
    with pytest.raises(ValueError, match="preview_rows"):
        executor.execute(decision, preview_rows=0)
    with pytest.raises(ValueError, match="timeout_ms"):
        executor.execute(decision, timeout_ms=-1)


def test_executor_passes_validated_cache_epoch_option_to_the_adapter() -> None:
    epoch = "epoch-2026-11-01"
    registry = build_default_registry(allow_uncertified_for_testing=True, cache_epoch=epoch)
    engine = DecisionEngine(registry)
    analysis = _analysis()
    for _ in range(2):
        decision = engine.decide(
            query_id=QUERY_ID,
            original_sql=TEST_SQL,
            mode=DecisionMode.RULE_BASED,
            analysis=analysis,
            server_version=SERVER_VERSION,
            database_model=DATABASE_MODEL,
        )
    assert decision.selected_strategy_id == "S1_REPEAT_CACHE"
    adapter = ScriptedAdapter(
        [execution_result(query_id=QUERY_ID, strategy_id="S1_REPEAT_CACHE")]
    )

    report = StrategyExecutor(adapter=adapter, registry=registry).execute(
        decision,
        analysis=analysis,
        server_version=SERVER_VERSION,
        database_model=DATABASE_MODEL,
    )

    assert report.fallback_used is False
    assert report.final_status is ExecutionStatus.SUCCESS
    assert adapter.execution_options == [{"cache_epoch": epoch}]


def test_native_fallback_after_cache_failure_carries_no_cache_option() -> None:
    epoch = "epoch-2026-11-01"
    registry = build_default_registry(allow_uncertified_for_testing=True, cache_epoch=epoch)
    engine = DecisionEngine(registry)
    analysis = _analysis()
    for _ in range(2):
        decision = engine.decide(
            query_id=QUERY_ID,
            original_sql=TEST_SQL,
            mode=DecisionMode.RULE_BASED,
            analysis=analysis,
            server_version=SERVER_VERSION,
            database_model=DATABASE_MODEL,
        )
    assert decision.selected_strategy_id == "S1_REPEAT_CACHE"
    adapter = ScriptedAdapter(
        [
            _not_dispatched_error(QUERY_ID, "S1_REPEAT_CACHE"),
            _success(QUERY_ID, "S0_NATIVE"),
        ]
    )

    report = StrategyExecutor(adapter=adapter, registry=registry).execute(
        decision,
        analysis=analysis,
        server_version=SERVER_VERSION,
        database_model=DATABASE_MODEL,
    )

    assert report.fallback_used is True
    assert report.fallback_trigger is FallbackTrigger.FIRST_REQUEST_NOT_DISPATCHED
    assert report.final_status is ExecutionStatus.SUCCESS
    assert adapter.execution_options == [{"cache_epoch": epoch}, {}]
    assert adapter.calls[1][2] == "S0_NATIVE"
    assert adapter.calls[1][0].sql == TEST_SQL
