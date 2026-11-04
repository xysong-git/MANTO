"""Disjoint LinUCB, OFFLINE ONLY. Li et al. Algorithm 1, ridge lambda I.

Pure Python Cholesky solves avoid an undeclared numerical dependency and avoid
explicit matrix inversion. Mutable numerical state is private to one owner.
"""
from __future__ import annotations
import math
from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator, model_validator

from ..errors import StrategyRegistryError
from ..decision.resource_policy import assess_resource_pressure, load_default_resource_policy
from ..schemas.common import ensure_utc, new_id, utc_now
from ..schemas.learning import Experience, SampleStatus, digest, load_identity
from .context import DIMENSIONS, ENCODER_VERSION, encode, partition_key


class AdmissionError(ValueError):
    """Expected rejection of offline evidence, distinct from internal failures."""
    def __init__(self, reason):
        self.reason = reason
        super().__init__(reason)


class Config(BaseModel):
    model_config = ConfigDict(extra='forbid', frozen=True, allow_inf_nan=False)
    alpha: float = Field(default=.1, ge=0, le=10)
    regularization: float = Field(default=1., gt=0, le=1000)
    min_samples: int = Field(default=30, ge=2)
    encoder_version: Literal['linucb-context-v1'] = ENCODER_VERSION
    reward_version: Literal['negative-log1p-ms-v1', 'negative-e2e-seconds-v1', 'negative-batch-service-seconds-v1'] = 'negative-log1p-ms-v1'


def solve(a, b):
    n = len(b)
    lower = [[0.]*n for _ in range(n)]
    for i in range(n):
        for j in range(i+1):
            value = a[i][j] - sum(lower[i][k]*lower[j][k] for k in range(j))
            if i == j:
                if value <= 0 or not math.isfinite(value):
                    raise ValueError('A must be finite positive definite')
                lower[i][j] = math.sqrt(value)
            else:
                lower[i][j] = value/lower[j][j]
    y = [0.]*n
    for i in range(n):
        y[i] = (b[i]-sum(lower[i][j]*y[j] for j in range(i)))/lower[i][i]
    x = [0.]*n
    for i in reversed(range(n)):
        x[i] = (y[i]-sum(lower[j][i]*x[j] for j in range(i+1,n)))/lower[i][i]
    return x


class Arm(BaseModel):
    model_config = ConfigDict(extra='forbid', allow_inf_nan=False)
    a: list[list[float]]
    b: list[float]
    n: int = Field(default=0, ge=0)

    @model_validator(mode='after')
    def dimensions_and_spd(self):
        d = len(DIMENSIONS)
        if len(self.a) != d or any(len(r) != d for r in self.a) or len(self.b) != d:
            raise ValueError('parameter dimension mismatch')
        if any(abs(self.a[i][j]-self.a[j][i]) > 1e-10 for i in range(d) for j in range(d)):
            raise ValueError('A must be symmetric')
        solve(self.a, self.b)
        return self


class Snapshot(BaseModel):
    model_config = ConfigDict(extra='forbid', allow_inf_nan=False)
    schema_version: Literal[1] = 1
    model_version: str = Field(default_factory=new_id)
    online_ready: Literal[False] = False
    domain: Literal['SYNTHETIC', 'REAL_SHADOW']
    partition: str
    baseline_arm: str
    config: Config = Field(default_factory=Config)
    arms: dict[str, Arm]
    training_until: datetime
    created_at: datetime = Field(default_factory=utc_now)
    observations: dict[str, str] = Field(default_factory=dict)  # ID -> digest; dedupe persists
    arm_observations: dict[str, str] = Field(default_factory=dict)

    @field_validator('training_until', 'created_at')
    @classmethod
    def utc(cls, value): return ensure_utc(value)

    @model_validator(mode='after')
    def coherent(self):
        if self.baseline_arm not in self.arms or len(self.arms) < 2:
            raise ValueError('at least baseline plus one arm required')
        if set(self.observations) != set(self.arm_observations):
            raise ValueError('observation identities differ')
        if any(arm not in self.arms for arm in self.arm_observations.values()):
            raise ValueError('unknown observed arm')
        for name, arm in self.arms.items():
            if arm.n != sum(a == name for a in self.arm_observations.values()):
                raise ValueError('arm sample count differs from observation ledger')
        return self


class OfflineLinUCB:
    """Cannot emit a production AdaptiveAdvice. All artifacts stay offline."""
    def __init__(self, arms, *, baseline_arm, partition, training_until,
                 domain='REAL_SHADOW', config=None):
        config = config or Config()
        d = len(DIMENSIONS)
        self._state = Snapshot(domain=domain, partition=partition, baseline_arm=baseline_arm,
            config=config, training_until=training_until,
            arms={name: Arm(a=[[config.regularization if i==j else 0. for j in range(d)]
                              for i in range(d)], b=[0.]*d) for name in arms})

    @classmethod
    def restore(cls, snapshot):
        obj = cls.__new__(cls)
        obj._state = Snapshot.model_validate(snapshot.model_dump()).model_copy(deep=True)
        return obj

    def snapshot(self):
        return self._state.model_copy(deep=True, update={'model_version': new_id(), 'created_at': utc_now()})

    def fingerprint_state(self):
        """Detached canonical data; no temporary snapshot IDs or deep-copy pass."""
        return self._state.model_dump(mode='json', exclude={'model_version', 'created_at'})

    @staticmethod
    def _vector(x):
        x = tuple(x)
        if len(x) != len(DIMENSIONS) or x[0] != 1 or any(not math.isfinite(v) or not 0 <= v <= 1 for v in x):
            raise ValueError('invalid context vector')
        if any(v not in (0., 1.) for v in x[9:]):
            raise ValueError('missing indicators must be binary')
        return x

    def predict(self, arm, x, *, ucb=True):
        x = self._vector(x)
        state = self._state.arms[arm]
        theta = solve(state.a, state.b)
        mean = sum(v*t for v,t in zip(x,theta))
        if not ucb:
            return mean
        uncertainty = math.sqrt(max(0., sum(v*t for v,t in zip(x,solve(state.a,x)))))
        return mean + self._state.config.alpha * uncertainty

    def choose(self, x, eligible_arms, *, explore=False):
        x = self._vector(x)
        candidates = sorted(set(eligible_arms))
        if not candidates or any(a not in self._state.arms for a in candidates):
            raise ValueError('unknown/empty candidate set')
        if self._state.baseline_arm not in candidates:
            raise ValueError('safe baseline required')
        under = [a for a in candidates if self._state.arms[a].n < self._state.config.min_samples]
        if under:
            return min(under, key=lambda a:(self._state.arms[a].n,a)) if explore else self._state.baseline_arm
        return max(candidates, key=lambda a:(self.predict(a,x,ucb=explore),a))

    def choose_registered(self, x, registry, *, explore=False):
        """Real-shadow evaluation re-filters the current certified arm versions."""
        if self._state.domain != 'REAL_SHADOW':
            raise ValueError('synthetic arms have no production registry identity')
        eligible = []
        for arm in self._state.arms:
            strategy_id, separator, version = arm.rpartition('@')
            if not separator:
                continue
            try:
                entry = registry.assert_selectable(strategy_id)
            except StrategyRegistryError:
                continue
            if entry.spec.version == version and entry.spec.verification_status.value == 'VERIFIED':
                eligible.append(arm)
        return self.choose(x, eligible, explore=explore)

    def _update(self, *, execution_id, arm, x, latency_ms, finished_at):
        x = self._vector(x)
        finished_at = ensure_utc(finished_at)
        if finished_at > self._state.training_until:
            raise ValueError('held-out observation cannot enter training')
        if arm not in self._state.arms or not execution_id:
            raise ValueError('unknown arm or empty execution ID')
        if not math.isfinite(latency_ms) or latency_ms < 0:
            raise ValueError('latency must be finite nonnegative milliseconds')
        reward = (-latency_ms/1000. if self._state.config.reward_version in ('negative-e2e-seconds-v1', 'negative-batch-service-seconds-v1')
                  else -math.log1p(latency_ms/1000.))
        signature = digest([arm,x,latency_ms,finished_at.isoformat()])
        prior = self._state.observations.get(execution_id)
        if prior is not None:
            if prior != signature: raise ValueError('conflicting execution evidence')
            return False
        old = self._state.arms[arm]
        updated = Arm(a=[[old.a[i][j]+x[i]*x[j] for j in range(len(x))] for i in range(len(x))],
                      b=[old.b[i]+reward*x[i] for i in range(len(x))], n=old.n+1)
        # Construct first; failed validation leaves the model unmodified.
        self._state.arms[arm] = updated
        self._state.observations[execution_id] = signature
        self._state.arm_observations[execution_id] = arm
        return True

    def update_synthetic(self, **kwargs):
        if self._state.domain != 'SYNTHETIC':
            raise ValueError('synthetic feedback cannot update a real-shadow model')
        return self._update(**kwargs)

    def update_completed_request(self, receipt, experience, workflow, registry):
        from .completed_request import CompletedRequest, E2E_REWARD_VERSION
        if self._state.domain != 'REAL_SHADOW' or self._state.config.reward_version != E2E_REWARD_VERSION:
            raise AdmissionError('COMPLETED_REQUEST_REWARD_VERSION_REQUIRED')
        receipt = CompletedRequest.model_validate(receipt.model_dump(round_trip=True))
        e = receipt.validate_evidence(experience, workflow)
        # Preserve every existing strategy/resource/quality admission check.
        check = OfflineLinUCB(list(self._state.arms), baseline_arm=self._state.baseline_arm,
            partition=partition_key(e), training_until=self._state.training_until)
        check.update_experience(e, registry)
        if self._state.partition != 'completed-request-v1:' + partition_key(e):
            raise AdmissionError('COMPLETED_REQUEST_PARTITION_MISMATCH')
        return self._update(execution_id=e.execution_id, arm=e.strategy_id+'@'+e.strategy_version,
            x=encode(e.features,e.before,at=e.record.started_at), latency_ms=receipt.end_to_end_ms,
            finished_at=receipt.finished_at)

    def update_experience(self, experience: Experience, registry):
        if self._state.config.reward_version != 'negative-log1p-ms-v1':
            raise AdmissionError('ADAPTER_REWARD_CANNOT_UPDATE_E2E_MODEL')
        if self._state.domain != 'REAL_SHADOW':
            raise ValueError('real evidence cannot mix with simulation')
        try:
            e = Experience.model_validate(experience.model_dump(round_trip=True))
        except ValidationError as exc:
            raise AdmissionError('INVALID_EXPERIENCE_SCHEMA') from exc
        if e.sample_status != SampleStatus.VALID:
            raise AdmissionError('SAMPLE_NOT_VALID')
        if e.quality_reasons:
            raise AdmissionError('VALID_WITH_QUALITY_REASONS')
        if e.context.fetch_mode != 'FULL':
            raise AdmissionError('INCOMPLETE_RESULT')
        for name in ('server_id', 'server_version', 'dataset_epoch', 'cache_condition', 'load_host'):
            if getattr(e.context, name).upper() == 'UNKNOWN':
                raise AdmissionError('UNKNOWN_' + name.upper())
        if e.context.result_rows is None:
            raise AdmissionError('UNKNOWN_RESULT_SIZE')
        if e.record.client_wall_ms is None or not math.isfinite(e.record.client_wall_ms):
            raise AdmissionError('INVALID_LATENCY')
        policy = load_default_resource_policy()
        entry = registry.assert_selectable(e.strategy_id)
        if e.certification_suite != entry.spec.semantic_test_suite:
            raise AdmissionError('CERTIFICATION_SUITE_MISMATCH')
        if e.resource_policy_version != policy.policy_version:
            raise AdmissionError('RESOURCE_POLICY_VERSION_MISMATCH')
        snapshot = getattr(e.before, e.context.load_source, None) if e.before else None
        if snapshot is None or any(getattr(snapshot, name) is None for name in
                ('cpu_percent', 'memory_percent', 'disk_usage_percent')):
            raise AdmissionError('LOAD_CONTEXT_UNAVAILABLE')
        host, bucket = load_identity(e.before, e.context.load_source, e.record.started_at, protocol=e.context.load_protocol)
        if (e.sample_status != SampleStatus.VALID or e.quality_version != 2
                or e.resource_policy_hash != digest(policy.model_dump(mode='json'))
                or e.strategy_version != entry.spec.version
                or entry.spec.verification_status.value != 'VERIFIED'
                or e.context.server_version not in entry.spec.compatible_server_versions
                or e.context.database_model != entry.spec.database_model
                or e.reward != -math.log1p(e.record.client_wall_ms/1000.)
                or (host,bucket) != (e.context.load_host,e.context.load_bucket) or bucket == 'UNKNOWN'
                or assess_resource_pressure(e.before, policy, now=e.record.started_at).breached
                or partition_key(e) != self._state.partition):
            raise AdmissionError('EXPERIENCE_NOT_QUALIFIED_FOR_PARTITION')
        arm = f'{e.strategy_id}@{e.strategy_version}'
        if arm not in self._state.arms:
            raise AdmissionError('ARM_NOT_IN_MODEL')
        return self._update(execution_id=e.execution_id, arm=arm,
            x=encode(e.features,e.before,at=e.record.started_at), latency_ms=e.record.client_wall_ms,
            finished_at=e.record.finished_at)

    def online_advice(self, *args, **kwargs):
        return None  # Deliberate deployment interlock, not a learned policy claim.
