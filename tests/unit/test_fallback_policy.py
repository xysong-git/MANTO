"""Fallback is allowed only when the adapter supplies retry-safety evidence."""

from __future__ import annotations

from tsdb_optimizer.execution import FallbackPolicy, FallbackTrigger
from tsdb_optimizer.schemas import RetryUnsafeReason
from tsdb_optimizer.schemas.execution import ExecutionStatus
from phase4_support import TEST_ONLY_STRATEGY_ID, execution_result

QUERY_ID = "q-fallback"


def _policy(**overrides):
    values = {
        "selected_strategy_id": TEST_ONLY_STRATEGY_ID,
        "baseline_strategy_id": "S0_NATIVE",
        "first_result": None,
        "preflight_failure_reason": None,
    }
    values.update(overrides)
    return FallbackPolicy().evaluate(**values)


def _error_result(**overrides):
    values = {
        "query_id": QUERY_ID,
        "strategy_id": TEST_ONLY_STRATEGY_ID,
        "status": ExecutionStatus.ERROR,
        "request_dispatched": True,
        "request_completed": False,
        "retry_unsafe_reason": (
            RetryUnsafeReason.REQUEST_COMPLETION_UNCONFIRMED
        ),
    }
    values.update(overrides)
    return execution_result(**values)


def test_baseline_never_falls_back_to_itself() -> None:
    decision = _policy(
        selected_strategy_id="S0_NATIVE",
        first_result=_error_result(strategy_id="S0_NATIVE"),
    )
    assert decision.allowed is False
    assert decision.trigger is FallbackTrigger.NONE
    assert "baseline" in decision.reason


def test_missing_first_result_without_preflight_failure_is_denied() -> None:
    decision = _policy()
    assert decision.allowed is False
    assert decision.trigger is FallbackTrigger.NONE


def test_preflight_failure_allows_fallback_before_dispatch() -> None:
    decision = _policy(preflight_failure_reason="candidate validation failed")
    assert decision.allowed is True
    assert decision.trigger is FallbackTrigger.CANDIDATE_VALIDATION_FAILED
    assert "before any database request" in decision.reason


def test_successful_first_request_is_not_retried() -> None:
    decision = _policy(
        first_result=execution_result(
            query_id=QUERY_ID,
            strategy_id=TEST_ONLY_STRATEGY_ID,
        )
    )
    assert decision.allowed is False
    assert "succeeded" in decision.reason


def test_timeout_blocks_fallback_even_if_adapter_claims_not_dispatched() -> None:
    result = _error_result(
        timed_out=True,
        request_dispatched=False,
        request_completed=False,
        retry_unsafe_reason=RetryUnsafeReason.TIMEOUT_OR_UNKNOWN_SERVER_STATE,
    )
    decision = _policy(first_result=result)
    assert decision.allowed is False
    assert "timed out" in decision.reason


def test_not_dispatched_error_allows_fallback() -> None:
    result = _error_result(
        request_dispatched=False,
        request_completed=False,
        retry_unsafe_reason=None,
    )
    decision = _policy(first_result=result)
    assert decision.allowed is True
    assert decision.trigger is FallbackTrigger.FIRST_REQUEST_NOT_DISPATCHED


def test_explicitly_retry_safe_error_allows_fallback() -> None:
    result = _error_result(
        request_completed=True,
        retry_safe=True,
        retry_unsafe_reason=None,
    )
    decision = _policy(first_result=result)
    assert decision.allowed is True
    assert decision.trigger is FallbackTrigger.FIRST_REQUEST_RETRY_SAFE


def test_unconfirmed_completion_blocks_fallback() -> None:
    result = _error_result()
    decision = _policy(first_result=result)
    assert decision.allowed is False
    assert decision.trigger is FallbackTrigger.NONE
    assert "completion is not confirmed" in decision.reason
    assert "REQUEST_COMPLETION_UNCONFIRMED" in decision.reason


def test_timeout_unsafe_reason_is_reported_when_completion_unknown() -> None:
    result = _error_result(
        request_dispatched=True,
        request_completed=False,
        retry_unsafe_reason=RetryUnsafeReason.TIMEOUT_OR_UNKNOWN_SERVER_STATE,
    )
    decision = _policy(first_result=result)
    assert decision.allowed is False
    assert "TIMEOUT_OR_UNKNOWN_SERVER_STATE" in decision.reason
