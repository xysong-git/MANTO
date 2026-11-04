"""Optional transport-injected advisor; disabled and transport-less by default.

No provider credentials, SDK, raw SQL or execution capability. Transport must
be cancellation-cooperative; real provider integration is intentionally absent.
"""
import asyncio
import time
from pathlib import Path
from typing import Awaitable, Callable

from pydantic import BaseModel, ConfigDict, Field

from .models import AdaptiveAdvice

PROMPT_PATH = Path(__file__).resolve().parents[3]/'configs/prompts/strategy_advisor.txt'


class Reply(BaseModel):
    model_config = ConfigDict(extra='forbid', strict=True)
    strategy_id: str = Field(min_length=1, max_length=100)


class AdvisorResult(BaseModel):
    model_config = ConfigDict(extra='forbid')
    advice: AdaptiveAdvice | None = None
    elapsed_ms: float = Field(ge=0)
    reason: str


class SafeLLMAdvisor:
    def __init__(self, registry, *, enabled=False, transport: Callable[[dict], Awaitable[str]] | None = None,
                 timeout_ms=100, provider_version='UNCONFIGURED'):
        if timeout_ms <= 0: raise ValueError('timeout must be positive')
        self.registry, self.enabled, self.transport = registry, enabled, transport
        self.timeout_ms, self.provider_version = timeout_ms, provider_version

    async def recommend(self, validated_decision):
        start = time.perf_counter()
        def result(reason, advice=None):
            return AdvisorResult(reason=reason, advice=advice, elapsed_ms=(time.perf_counter()-start)*1000)
        if not self.enabled: return result('DISABLED')
        if self.transport is None: return result('UNAVAILABLE')
        try:
            allowed = []
            for item in validated_decision.considered:
                if (item.candidate is not None and item.applicability.applicable
                        and item.validation is not None and item.validation.passed
                        and not (item.rejection_reason or '').startswith('vetoed by resource policy')):
                    entry = self.registry.assert_selectable(item.strategy_id)
                    if entry.spec.version == item.candidate.strategy_version:
                        allowed.append(item.strategy_id)
            baseline = self.registry.baseline_strategy_id
            if baseline not in allowed: return result('NO_SAFE_BASELINE')
            payload = {'instruction': PROMPT_PATH.read_text(), 'eligible_strategy_ids': sorted(allowed),
                       'baseline_strategy_id': baseline}
            raw = await asyncio.wait_for(self.transport(payload), self.timeout_ms/1000)
            if not isinstance(raw,str) or len(raw) > 2048: return result('INVALID_RESPONSE')
            reply = Reply.model_validate_json(raw)
            if reply.strategy_id not in allowed: return result('UNSAFE_STRATEGY')
            self.registry.assert_selectable(reply.strategy_id)  # quarantine may have changed during I/O
            return result('ADVICE', AdaptiveAdvice(strategy_id=reply.strategy_id,
                model_version='llm:'+self.provider_version, sample_count=0,
                reason='Schema-validated optional suggestion; not learned execution evidence'))
        except TimeoutError:
            return result('TIMEOUT')
        except Exception as exc:
            # Error category exposed, never arbitrary provider text or credentials.
            return result('FALLBACK:'+type(exc).__name__)
