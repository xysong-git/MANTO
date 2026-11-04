"""Experimental feature ablation around the unchanged LinUCB implementation."""
from __future__ import annotations

import time

from ..bandit.completed_request import CompletedRequest
from ..bandit.context import encode, partition_key
from ..bandit.model import AdmissionError, Config, OfflineLinUCB, Snapshot
from ..decision import DecisionEngine
from ..decision.models import AdaptiveAdvice
from ..schemas.learning import digest
from .protocol import checksum

ARMS = ('S0_NATIVE@0.1.0', 'S1_REPEAT_CACHE@1.0.0')


def masked_vector(features, sample, at, *, remove_state):
    vector = list(encode(features, sample, at=at))
    if remove_state:
        # CPU/memory values and their missing flags, for BOTH client and DB.
        for i in range(5, 9):
            vector[i] = 0.
            vector[i+8] = 1.
    return tuple(vector)


def model_key(allocation):
    # Real system state remains a feature, not a hidden load-bucket partition.
    return checksum([allocation.workload, allocation.cache_condition, allocation.concurrency])


class PolicyBank:
    def __init__(self, *, deadline, min_samples=30):
        self.deadline = deadline
        self.min_samples = min_samples
        self.models = {}
        self.frozen = False
        self.evidence = {}

    def update(self, allocation, experience, workflow, *, started_at, finished_at, elapsed_ns, registry):
        if self.frozen or allocation.phase != 'TRAIN':
            raise AdmissionError('FROZEN_OR_HOLDOUT_CANNOT_TRAIN')
        try:
            receipt = CompletedRequest.capture(experience, workflow, started_at=started_at,
                finished_at=finished_at, elapsed_ns=elapsed_ns)
            e = receipt.validate_evidence(experience, workflow)
        except ValueError as exc:
            raise AdmissionError('UNQUALIFIED_COMPLETED_REQUEST') from exc
        if e.record.runtime_result_check != 'MATCHED':
            raise AdmissionError('BENCHMARK_REQUIRES_REFERENCE_MATCH')
        # Reuse every existing strategy/quality/resource gate before masking.
        check = OfflineLinUCB(ARMS, baseline_arm=ARMS[0], partition=partition_key(e),
            training_until=self.deadline)
        check.update_experience(e, registry)
        key = model_key(allocation)
        signature = checksum([key, receipt.model_dump(mode='json')])
        if e.execution_id in self.evidence:
            if self.evidence[e.execution_id] != signature:
                raise AdmissionError('CONFLICTING_TRAINING_RECEIPT')
            return False
        # Build both replacements before publishing either arm-bank update.
        replacements = {}
        for variant in ('E4', 'A1'):
            identifier = variant+':'+key
            old = self.models.get(identifier)
            model = OfflineLinUCB.restore(old.snapshot()) if old is not None else OfflineLinUCB(
                ARMS, baseline_arm=ARMS[0], partition='phase8-request-e2e-v1:'+identifier,
                training_until=self.deadline, config=Config(min_samples=self.min_samples,
                    reward_version='negative-e2e-seconds-v1'))
            # Same A/b update/reward as phase 7; only the declared feature mask differs.
            model._update(execution_id=e.execution_id, arm=e.strategy_id+'@'+e.strategy_version,
                x=masked_vector(e.features, e.before, e.record.started_at, remove_state=variant == 'A1'),
                latency_ms=receipt.end_to_end_ms, finished_at=receipt.finished_at)
            replacements[identifier] = model
        self.models.update(replacements)
        self.evidence[e.execution_id] = signature
        return True

    @property
    def fingerprint(self):
        return digest({'models': {k: m.fingerprint_state() for k, m in sorted(self.models.items())},
                       'evidence': self.evidence, 'frozen': self.frozen})

    def freeze(self):
        self.frozen = True
        return self.snapshot()

    def snapshot(self):
        return {'schema_version': 1, 'protocol': 'phase8-request-e2e-v1', 'frozen': self.frozen,
            'deadline': self.deadline.isoformat(), 'min_samples': self.min_samples,
            'evidence': dict(self.evidence), 'fingerprint': self.fingerprint,
            'models': {k: m.snapshot().model_dump(mode='json') for k, m in sorted(self.models.items())}}

    @classmethod
    def restore(cls, body):
        from datetime import datetime
        if body['schema_version'] != 1 or body['protocol'] != 'phase8-request-e2e-v1':
            raise ValueError('unknown benchmark policy protocol')
        obj = cls(deadline=datetime.fromisoformat(body['deadline']), min_samples=body['min_samples'])
        obj.models = {k: OfflineLinUCB.restore(Snapshot.model_validate(v)) for k, v in body['models'].items()}
        obj.evidence = dict(body['evidence'])
        obj.frozen = body['frozen']
        if obj.fingerprint != body['fingerprint']:
            raise ValueError('policy artifact checksum mismatch')
        for key, model in obj.models.items():
            if (model._state.partition != 'phase8-request-e2e-v1:'+key
                    or model._state.config.min_samples != obj.min_samples
                    or model._state.config.reward_version != 'negative-e2e-seconds-v1'
                    or set(model._state.arms) != set(ARMS)):
                raise ValueError('policy artifact scope mismatch')
        return obj

    def advice(self, allocation, variant, analysis, sample, at, registry):
        if not self.frozen:
            raise ValueError('holdout requires a frozen policy bank')
        model = self.models.get(variant+':'+model_key(allocation))
        if model is None:
            return None
        arm = model.choose_registered(masked_vector(analysis.features, sample, at,
            remove_state=variant == 'A1'), registry)
        count = model._state.arms[arm].n
        return AdaptiveAdvice(strategy_id=arm.split('@')[0], model_version='PHASE8:'+self.fingerprint,
            sample_count=count, reason='Frozen held-out benchmark; real prior feedback; no production activation')

    def counts(self):
        return {k: {arm: state.n for arm, state in m._state.arms.items()} for k, m in sorted(self.models.items())}


class BenchmarkEngine(DecisionEngine):
    """Inject only registered strategy suggestions; all normal validation remains."""
    def __init__(self, registry, *, allocation, bank=None):
        super().__init__(registry)
        self.allocation = allocation
        self.bank = bank
        self.preparing = False
        self.last_inference = None

    def decide(self, **kwargs):
        started = time.perf_counter_ns()
        decision = self._benchmark_decide(**kwargs)
        # The inherited timer starts after advice computation. Include the
        # experiment's actual inference/masking cost in decision overhead too.
        return decision.model_copy(update={'decision_overhead_ms': (time.perf_counter_ns()-started)/1e6})

    def _benchmark_decide(self, **kwargs):
        from ..schemas.common import utc_now
        self.last_inference = None
        group = self.allocation.group
        if str(kwargs.get('mode')) == 'baseline' and not self.preparing:
            return super().decide(**kwargs)
        fixed = ('S1_REPEAT_CACHE' if self.preparing or group in ('E1', 'TRAIN_S1') else
                 'S0_NATIVE' if group == 'TRAIN_S0' else None)
        if fixed:
            kwargs['adaptive_advice'] = AdaptiveAdvice(strategy_id=fixed, model_version='PHASE8:FIXED',
                sample_count=0, reason='Predeclared registered fixed preference; applicability still required')
            kwargs['mode'] = 'adaptive'
        elif group in ('E4', 'A1'):
            at = utc_now()
            kwargs['adaptive_advice'] = self.bank.advice(self.allocation, group, kwargs['analysis'],
                kwargs['system_state'], at, self.registry)
            self.last_inference = {'at': at.isoformat(),
                'sample': kwargs['system_state'].model_dump(mode='json', round_trip=True),
                'advice': kwargs['adaptive_advice'].model_dump(mode='json') if kwargs['adaptive_advice'] else None,
                # advice() already hashes this frozen bank for model_version.
                # Reuse that exact request-local provenance, not a cached digest
                # that could survive a bank restore or a different request.
                'model_fingerprint': (kwargs['adaptive_advice'].model_version.removeprefix('PHASE8:')
                    if kwargs['adaptive_advice'] is not None else self.bank.fingerprint)}
            kwargs['mode'] = 'adaptive'
        else:
            kwargs['mode'] = 'rule_based'
            kwargs['adaptive_advice'] = None
        return super().decide(**kwargs)
