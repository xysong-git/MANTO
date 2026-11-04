"""Conservative Native fallback policy.

The adapter cannot prove server-side cancellation after a timeout.  Therefore a
failed first request is retried only when the adapter explicitly reports that
the request was not dispatched or that completion is confirmed and the result
is retry-safe.  Current IoTDB adapter errors are deliberately not retry-safe.
"""

from __future__ import annotations

from ..schemas.execution import ExecutionResult, ExecutionStatus
from .models import FallbackDecision, FallbackTrigger


class FallbackPolicy:
    def evaluate(
        self,
        *,
        selected_strategy_id: str,
        baseline_strategy_id: str,
        first_result: ExecutionResult | None,
        preflight_failure_reason: str | None,
    ) -> FallbackDecision:
        if selected_strategy_id == baseline_strategy_id:
            return FallbackDecision(
                allowed=False,
                trigger=FallbackTrigger.NONE,
                reason=(
                    "selected strategy is the Native baseline; retrying it would "
                    "duplicate the same request"
                ),
            )
        if first_result is None:
            if (preflight_failure_reason or "").strip():
                return FallbackDecision(
                    allowed=True,
                    trigger=FallbackTrigger.CANDIDATE_VALIDATION_FAILED,
                    reason=(
                        "candidate validation failed before any database request "
                        f"was dispatched: {preflight_failure_reason}"
                    ),
                )
            return FallbackDecision(
                allowed=False,
                trigger=FallbackTrigger.NONE,
                reason=(
                    "no first execution result exists and no pre-dispatch "
                    "validation failure was recorded"
                ),
            )
        if first_result.status is ExecutionStatus.SUCCESS:
            return FallbackDecision(
                allowed=False,
                trigger=FallbackTrigger.NONE,
                reason="the first execution succeeded; fallback is not needed",
            )
        if first_result.timed_out:
            return FallbackDecision(
                allowed=False,
                trigger=FallbackTrigger.NONE,
                reason=(
                    "the first request timed out; server-side completion is "
                    "unknown, so a retry could duplicate work"
                ),
            )
        if not first_result.request_dispatched:
            return FallbackDecision(
                allowed=True,
                trigger=FallbackTrigger.FIRST_REQUEST_NOT_DISPATCHED,
                reason=(
                    "the adapter reports the first request was not dispatched; "
                    "Native fallback cannot duplicate server work"
                ),
            )
        if first_result.retry_safe:
            return FallbackDecision(
                allowed=True,
                trigger=FallbackTrigger.FIRST_REQUEST_RETRY_SAFE,
                reason=(
                    "the adapter explicitly reports the first request completed "
                    "and is safe to retry"
                ),
            )
        unsafe_reason = (
            first_result.retry_unsafe_reason.value
            if first_result.retry_unsafe_reason is not None
            else "REQUEST_COMPLETION_UNCONFIRMED"
        )
        return FallbackDecision(
            allowed=False,
            trigger=FallbackTrigger.NONE,
            reason=(
                "first request completion is not confirmed; refusing retry "
                f"({unsafe_reason})"
            ),
        )


__all__ = ["FallbackPolicy"]
