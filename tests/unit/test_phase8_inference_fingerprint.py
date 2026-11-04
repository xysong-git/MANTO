"""Request-local inference provenance reuse; no database access."""
from types import SimpleNamespace

import pytest

from tsdb_optimizer.benchmark.policy import BenchmarkEngine
from tsdb_optimizer.decision import DecisionEngine
from tsdb_optimizer.decision.models import AdaptiveAdvice


@pytest.mark.parametrize('group', ['E4', 'A1'])
@pytest.mark.parametrize('has_advice', [True, False])
def test_inference_records_exact_advice_fingerprint_once(monkeypatch, group, has_advice):
    class Bank:
        calls = 0

        @property
        def fingerprint(self):
            self.calls += 1
            return 'frozen-bank-digest'

        def advice(self, *args):
            if not has_advice:
                return None
            return AdaptiveAdvice(strategy_id='S0_NATIVE',
                model_version='PHASE8:'+self.fingerprint, sample_count=30,
                reason='test-only suggestion')

    # Isolate provenance bookkeeping; real Validator/algorithm paths are covered
    # by test_phase8_benchmark and the full decision/executor suites.
    monkeypatch.setattr(DecisionEngine, 'decide', lambda self, **kwargs: kwargs)
    engine = object.__new__(BenchmarkEngine)
    engine.registry = object()
    engine.allocation = SimpleNamespace(group=group)
    engine.bank = Bank()
    engine.preparing = False
    sample = SimpleNamespace(model_dump=lambda **kwargs: {'test_only': True})
    result = engine._benchmark_decide(mode='adaptive', analysis=object(), system_state=sample)
    assert engine.bank.calls == 1
    assert engine.last_inference['model_fingerprint'] == 'frozen-bank-digest'
    assert result['mode'] == 'adaptive'
    if has_advice:
        assert result['adaptive_advice'].model_version == 'PHASE8:'+engine.last_inference['model_fingerprint']
    else:
        assert result['adaptive_advice'] is None
