"""Frozen L1 comparator for the same admitted whole-batch training evidence."""
from collections import defaultdict
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from ..bandit.episodes import BatchFeedback, batch_family
from ..bandit.model import AdmissionError
from ..errors import StrategyRegistryError
from ..schemas.learning import digest, load_identity


class BatchStatistic(BaseModel):
    model_config = ConfigDict(extra='forbid', frozen=True, allow_inf_nan=False)
    load: Literal['LOW', 'MEDIUM', 'HIGH']
    size: Literal[2, 8]
    strategy: Literal['S0_NATIVE', 'S1_REPEAT_CACHE']
    version: str
    count: int = Field(ge=1)
    total_service_ms: float = Field(ge=0)


class StatisticsSnapshot(BaseModel):
    model_config = ConfigDict(extra='forbid', frozen=True)
    protocol: Literal['batch-mean-v1'] = 'batch-mean-v1'
    reward_version: Literal['negative-batch-service-seconds-v1'] = 'negative-batch-service-seconds-v1'
    scope: dict[str, str]
    min_samples: int = Field(default=30, ge=2)
    frozen: bool
    statistics: list[BatchStatistic]
    evidence: dict[str, str]


class BatchStatistics:
    """Explicit study comparator, not a replacement of production L1."""
    def __init__(self, *, scope, min_samples=30):
        if type(min_samples) is not int or min_samples < 2:
            raise ValueError('min_samples must be an integer >= 2')
        self.scope = dict(scope)
        self.min_samples = min_samples
        self._frozen = False
        self._totals = defaultdict(lambda: [0, 0.])
        self._evidence = {}
        self._owners = {}

    def _scope(self, contract):
        if any(getattr(contract, k) != v for k, v in self.scope.items()):
            raise AdmissionError('L1_BATCH_SCOPE_MISMATCH')

    def observe(self, feedback, registry):
        if self._frozen:
            raise AdmissionError('L1_BATCH_FROZEN')
        f = BatchFeedback.model_validate(feedback.model_dump(round_trip=True))
        self._scope(f.contract)
        f.admit_learning(registry)
        checksum = digest(f.model_dump(mode='json', round_trip=True))
        prior = self._evidence.get(f.contract.batch_id)
        if prior is not None:
            if checksum != prior:
                raise AdmissionError('L1_BATCH_CONFLICT')
            return False
        if any(identifier in self._owners for identifier in f.identities):
            raise AdmissionError('L1_BATCH_REUSED_EXECUTION')
        version = registry.assert_selectable(f.allocated_strategy).spec.version
        key = (f.load_bucket, f.contract.size, f.allocated_strategy, version)
        count, total = self._totals[key]
        # Validate numerical accumulation before changing any state.
        row = BatchStatistic(load=key[0], size=key[1], strategy=key[2], version=version,
                             count=count+1, total_service_ms=total+f.service_ms)
        self._totals[key] = [row.count, row.total_service_ms]
        self._evidence[f.contract.batch_id] = checksum
        self._owners.update({identifier: f.contract.batch_id for identifier in f.identities})
        return True

    def snapshot(self):
        return StatisticsSnapshot(scope=self.scope, min_samples=self.min_samples,
            frozen=self._frozen, evidence=dict(self._evidence), statistics=[
                BatchStatistic(load=k[0], size=k[1], strategy=k[2], version=k[3],
                               count=v[0], total_service_ms=v[1])
                for k, v in sorted(self._totals.items())])

    @property
    def fingerprint(self):
        return digest(self.snapshot().model_dump(mode='json'))

    def freeze(self):
        self._frozen = True
        return self.snapshot()

    def choose(self, contract, features, sample, at, registry):
        if not self._frozen or contract.phase != 'HOLDOUT':
            raise AdmissionError('L1_BATCH_REQUIRES_FROZEN_HOLDOUT')
        self._scope(contract)
        if features.query_hash != contract.query_hash or batch_family(features) != contract.family_key:
            raise AdmissionError('L1_BATCH_FEATURE_MISMATCH')
        load = load_identity(sample, 'db_host', at, protocol='cpu-activity-v3')[1]
        baseline = registry.baseline_strategy_id
        registry.assert_selectable(baseline)
        means = {}
        for strategy in (baseline, 'S1_REPEAT_CACHE'):
            try:
                entry = registry.assert_selectable(strategy)
            except StrategyRegistryError:
                continue
            count, total = self._totals.get((load, contract.size, strategy, entry.spec.version), (0, 0.))
            if count >= self.min_samples:
                means[strategy] = total/count
        if len(means) != 2:
            return baseline
        # Native wins ties, no threshold fitted on held-out results.
        return min(means, key=lambda strategy: (means[strategy], strategy != baseline, strategy))
