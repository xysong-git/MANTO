"""Real, closed caller batches: one delayed label for the executed option.

An option chooses existing Registry strategies across a declared finite batch.
It does not estimate counterfactual savings or infer future request counts.
"""
from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from .completed_request import CompletedRequest
from .context import encode, partition_key
from .model import AdmissionError, Config, OfflineLinUCB, Snapshot
from ..experiments.controlled_policy import ARMS, fingerprint
from ..experiments.sequences import SequenceRequest, validate_sequence
from ..schemas.common import ensure_utc
from ..schemas.learning import digest, load_identity
from ..schemas.monitoring import SystemSample
from ..schemas.query_features import QueryFeatures
from ..errors import StrategyRegistryError

REWARD_VERSION = 'negative-batch-service-seconds-v1'


def batch_family(features):
    """Narrow audited workload family, distinct from an exact SQL template.

    Different device/series identifiers are allowed only within the declared
    small-scale 10-device/16-COUNT workload, not as arbitrary SQL generalization.
    Original SQL still goes through Parser/Registry/Validator on every request.
    """
    if (features.device_count.value != 10 or features.series_count.value != 16
            or features.aggregation_types.value != ['COUNT']
            or any(getattr(features, name).value is not False for name in ('has_group_by', 'has_order_by', 'has_limit'))
            or features.filter_count.value != 2 or features.time_range_ms.value is None
            or not features.devices.value
            or any(not d.path.startswith('root.small_scale.g_0.') for d in features.devices.value)):
        raise ValueError('outside audited batch workload family')
    return 'small-scale-count-10x16-v1'


class BatchContract(BaseModel):
    model_config = ConfigDict(extra='forbid', frozen=True, allow_inf_nan=False)
    schema_version: Literal[1] = 1
    batch_id: str = Field(min_length=1)
    source: Literal['caller_declared_finite_batch'] = 'caller_declared_finite_batch'
    phase: Literal['TRAIN', 'HOLDOUT']
    size: Literal[2, 8]
    query_hash: str = Field(pattern=r'^[0-9a-f]{64}$')
    family_key: str = Field(min_length=1)
    server_id: str
    epoch: str
    declared_at: datetime

    @field_validator('declared_at')
    @classmethod
    def utc(cls, value): return ensure_utc(value)


class BatchFeedback(BaseModel):
    model_config = ConfigDict(extra='forbid', allow_inf_nan=False)
    reward_version: Literal['negative-batch-service-seconds-v1'] = REWARD_VERSION
    contract: BatchContract
    allocated_strategy: Literal['S0_NATIVE', 'S1_REPEAT_CACHE']
    policy: Literal['S0_NATIVE', 'S1_REPEAT_CACHE', 'ADAPTIVE', 'L1_BATCH']
    features: QueryFeatures
    selection_sample: SystemSample
    context_at: datetime
    selected_at: datetime
    selection_ns: int = Field(gt=0, strict=True)
    closed_at: datetime
    requests: list[SequenceRequest]

    @field_validator('context_at', 'selected_at', 'closed_at')
    @classmethod
    def utc(cls, value): return ensure_utc(value)

    @property
    def service_ms(self):
        return self.selection_ns/1_000_000 + sum(r.e2e_ms for r in self.requests)

    @property
    def reward(self): return -self.service_ms/1000

    @property
    def load_bucket(self):
        return load_identity(self.selection_sample, 'db_host', self.context_at, protocol='cpu-activity-v3')[1]

    @property
    def vector(self): return encode(self.features, self.selection_sample, at=self.context_at)

    @property
    def identities(self):
        return [identifier for row in self.requests for identifier in
            [('query:'+row.workflow.trace.query_id), *('execution:'+e.execution_id for e in row.experiences)]]

    @model_validator(mode='after')
    def closed_and_bound(self):
        c = self.contract
        if len(self.requests) != c.size:
            raise ValueError('only complete declared batches can close')
        if (not c.declared_at <= self.context_at <= self.selected_at <= self.requests[0].started_at
                or self.closed_at < self.requests[-1].finished_at):
            raise ValueError('batch timing/order mismatch')
        if abs((self.selected_at-c.declared_at).total_seconds()*1e9-self.selection_ns) > 5_000_000:
            raise ValueError('selection timer disagreement')
        if self.service_ms > (self.closed_at-c.declared_at).total_seconds()*1000+5:
            raise ValueError('service costs exceed closed wall interval')
        if self.features.query_hash != c.query_hash or batch_family(self.features) != c.family_key:
            raise ValueError('batch query family mismatch')
        for name in ('client_host', 'db_host'):
            snapshot = getattr(self.selection_sample, name)
            if snapshot.source_type.value != name or snapshot.stale_after_ms != 500 or not snapshot.is_usable(now=self.context_at, max_age_ms=250):
                raise ValueError('invalid pre-action context')
        if self.load_bucket == 'UNKNOWN':
            raise ValueError('unknown batch context')
        if self.policy in ('S0_NATIVE', 'S1_REPEAT_CACHE') and self.policy != self.allocated_strategy:
            raise ValueError('fixed policy allocation mismatch')
        if c.phase == 'TRAIN' and self.policy not in ('S0_NATIVE', 'S1_REPEAT_CACHE'):
            raise ValueError('training requires predeclared randomized fixed option')
        reference = self.requests[0].workflow.run.execution.attempts[-1].execution
        validate_sequence(self.requests, reference, sql_hash=c.query_hash, epoch=c.epoch)
        for row in self.requests:
            if row.length != c.size or row.policy != self.policy or row.admission_sample is None:
                raise ValueError('request outside declared batch')
            for e in row.experiences:
                from ..agents.learning_feedback import LearningFeedbackAgent
                LearningFeedbackAgent._validated_attempt(row.workflow.run, e.record)
                if (e.context.dataset_epoch != c.epoch or e.context.server_id != c.server_id
                        or e.context.cache_condition != 'delayed-batch-v1'
                        or batch_family(e.features) != c.family_key
                        or e.features.model_dump(exclude={'query_id'}) != self.features.model_dump(exclude={'query_id'})):
                    raise ValueError('mixed batch environment')
            if any(f.model_updated for f in row.workflow.feedback):
                raise ValueError('batch route cannot silently publish synchronous L1')
            if row.index > 0 and not row.workflow.run.execution.fallback_used and row.workflow.trace.effective_mode != 'baseline':
                if row.workflow.run.decision.selected_strategy_id != self.allocated_strategy:
                    raise ValueError('executed option differs from allocation')
        return self

    def admit_learning(self, registry):
        if self.contract.phase != 'TRAIN':
            raise AdmissionError('HOLDOUT_CANNOT_TRAIN')
        for row in self.requests:
            if row.workflow.trace.effective_mode == 'baseline' and row.index > 0:
                raise AdmissionError('SAFETY_DOWNGRADED_BATCH')
            for e in row.experiences:
                try:
                    CompletedRequest.capture(e, row.workflow, started_at=row.started_at,
                        finished_at=row.finished_at, elapsed_ns=row.elapsed_ns)
                except ValueError as exc:
                    raise AdmissionError('BATCH_REQUEST_NOT_QUALIFIED') from exc
                check = OfflineLinUCB(ARMS, baseline_arm=ARMS[0], partition=partition_key(e),
                    training_until=self.closed_at)
                try:
                    check.update_experience(e, registry)
                except StrategyRegistryError as exc:
                    raise AdmissionError('BATCH_STRATEGY_NOT_SELECTABLE') from exc
                if e.context.load_bucket != self.load_bucket:
                    raise AdmissionError('BATCH_LOAD_CHANGED')
        return self


class BatchPolicy:
    """Versioned per-declared-length contextual models; no default deployment."""
    def __init__(self, *, deadline, family_key, server_id, epoch, models=None, owners=None, evidence=None):
        self.scope = {'family_key': family_key, 'server_id': server_id, 'epoch': epoch}
        self.owners = owners or {}
        self.evidence = evidence or {}
        self.models = models or {str(n): OfflineLinUCB(ARMS, baseline_arm=ARMS[0],
            partition='delayed-batch-v1:'+digest([self.scope, n]), training_until=deadline,
            config=Config(reward_version=REWARD_VERSION)) for n in (2, 8)}
        if set(self.models) != {'2', '8'}:
            raise ValueError('missing batch models')
        for n, model in self.models.items():
            state = model.snapshot()
            if (state.domain != 'REAL_SHADOW' or state.config.reward_version != REWARD_VERSION
                    or state.partition != 'delayed-batch-v1:'+digest([self.scope, int(n)])
                    or set(state.arms) != set(ARMS)):
                raise ValueError('batch model scope/reward mismatch')
        batch_ids = set().union(*(m.snapshot().observations for m in self.models.values()))
        if set(self.evidence) != batch_ids or set(self.owners.values()) != batch_ids:
            raise ValueError('batch model evidence ledger mismatch')

    def snapshot(self):
        return {'scope': self.scope, 'models': {n: m.snapshot().model_dump(mode='json') for n,m in self.models.items()},
            'owners': dict(self.owners), 'evidence': dict(self.evidence)}

    @classmethod
    def restore(cls, body):
        models = {n: OfflineLinUCB.restore(Snapshot.model_validate(m)) for n,m in body['models'].items()}
        return cls(deadline=None, models=models, owners=body['owners'], evidence=body['evidence'], **body['scope'])

    @property
    def fingerprint(self): return digest([fingerprint(self.models), self.owners, self.evidence])

    def choose(self, contract, features, sample, at, registry):
        self._scope(contract)
        return self.models[str(contract.size)].choose_registered(encode(features, sample, at=at), registry).split('@')[0]

    def _scope(self, contract):
        if any(getattr(contract, k) != v for k,v in self.scope.items()):
            raise AdmissionError('BATCH_MODEL_SCOPE_MISMATCH')

    def update(self, feedback, registry):
        feedback = BatchFeedback.model_validate(feedback.model_dump(round_trip=True))
        self._scope(feedback.contract)
        feedback.admit_learning(registry)
        batch_id = feedback.contract.batch_id
        checksum = digest(feedback.model_dump(mode='json', round_trip=True))
        if batch_id in self.evidence and self.evidence[batch_id] != checksum:
            raise AdmissionError('CONFLICTING_BATCH_EVIDENCE')
        if any(identifier in self.owners and self.owners[identifier] != batch_id for identifier in feedback.identities):
            raise AdmissionError('EXECUTION_ALREADY_ATTRIBUTED')
        entry = registry.assert_selectable(feedback.allocated_strategy)
        # The actual option owns the complete realized sequence cost. Native
        # prefix and hits do not create separate synthetic option observations.
        changed = self.models[str(feedback.contract.size)]._update(
            execution_id=batch_id,
            arm=feedback.allocated_strategy+'@'+entry.spec.version,
            x=feedback.vector, latency_ms=feedback.service_ms, finished_at=feedback.closed_at)
        if changed:
            self.evidence[batch_id] = checksum
            self.owners.update({identifier: batch_id for identifier in feedback.identities})
        return changed
