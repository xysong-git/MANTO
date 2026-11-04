"""Phase-5 contracts. Context equality is deliberately conservative."""
from __future__ import annotations

import hashlib
import json
from datetime import datetime
from enum import StrEnum
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator, model_serializer

from .common import ensure_utc, new_id, utc_now
from .monitoring import ExecutionRecord, SystemSample
from .query_features import QueryFeatures


def digest(value: object) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(',', ':'),
                                     allow_nan=False).encode()).hexdigest()


class SampleStatus(StrEnum):
    VALID = 'VALID'
    LOW_CONFIDENCE = 'LOW_CONFIDENCE'
    INVALID = 'INVALID'


class LearningContext(BaseModel):
    model_config = ConfigDict(extra='forbid', frozen=True)
    schema_version: Literal[1] = 1
    server_id: str = Field(min_length=1)
    server_version: str = Field(min_length=1)
    database_model: Literal['tree', 'table', 'unknown']
    dataset_epoch: str = Field(min_length=1)
    dataset_size: int | None = Field(default=None, ge=0)
    result_rows: int | None = Field(default=None, ge=0)
    cache_condition: str = Field(min_length=1)
    # Effective result source must be filled from the execution, not guessed.
    result_source: Literal['SERVER', 'CACHE_HIT'] = 'SERVER'
    fetch_mode: str = 'FULL'
    load_source: Literal['client_host', 'db_host', 'unknown'] = 'unknown'
    load_host: str = 'UNKNOWN'
    load_bucket: Literal['LOW', 'MEDIUM', 'HIGH', 'UNKNOWN'] = 'UNKNOWN'
    load_protocol: Literal['resource-max-v1', 'cpu-activity-v2', 'cpu-activity-v3'] = 'resource-max-v1'
    feature_version: Literal['agent1-v1'] = 'agent1-v1'

    @field_validator('server_id', 'server_version', 'dataset_epoch', 'cache_condition', 'load_host')
    @classmethod
    def clean(cls, value: str) -> str:
        if not value.strip():
            raise ValueError('context identifier must not be blank')
        return value.strip()

    @model_serializer(mode='wrap')
    def serialize_context(self, handler):
        body = handler(self)
        # Preserve historical JSON identities and SQLite keys; v2 is explicit.
        if self.load_protocol == 'resource-max-v1':
            body.pop('load_protocol', None)
        return body

    @property
    def key(self) -> str:
        return digest(self.model_dump(mode='json'))


def load_identity(sample: SystemSample | None, source: str,
                  at: datetime, *, protocol: str = 'resource-max-v1') -> tuple[str, str]:
    if protocol not in ('resource-max-v1', 'cpu-activity-v2', 'cpu-activity-v3'):
        raise ValueError('unknown load protocol')
    if sample is None or source not in ('client_host', 'db_host'):
        return 'UNKNOWN', 'UNKNOWN'
    snapshot = getattr(sample, source)
    if snapshot.source_type.value != source or not snapshot.is_usable(now=at):
        return 'UNKNOWN', 'UNKNOWN'
    if protocol == 'cpu-activity-v3' and snapshot.cpu_measurement != 'cpu-times-instance-v1':
        return 'UNKNOWN', 'UNKNOWN'
    pressure = (snapshot.cpu_percent if protocol in ('cpu-activity-v2','cpu-activity-v3') else
                max(snapshot.cpu_percent, snapshot.memory_percent, snapshot.disk_usage_percent))
    return snapshot.source_host, ('HIGH' if pressure > 90 else 'MEDIUM' if pressure >= 50 else 'LOW')


class Experience(BaseModel):
    model_config = ConfigDict(extra='forbid')
    schema_version: Literal[1] = 1
    experience_id: str = Field(default_factory=new_id)
    query_id: str
    decision_id: str
    execution_id: str
    query_hash: str
    normalized_template: str | None
    template_key: str
    features: QueryFeatures
    feature_key: str
    context: LearningContext
    strategy_id: str
    strategy_version: str
    certification_suite: str | None
    record: ExecutionRecord
    before: SystemSample | None = None
    during: list[SystemSample] = Field(default_factory=list)
    after: SystemSample | None = None
    is_fallback: bool = False
    cache_source_execution_id: str | None = None
    decision_model_version: str | None = None
    quality_version: Literal[1, 2] = 1  # Old JSON must not silently gain the new admission proof.
    resource_policy_version: str | None = None
    resource_policy_hash: str | None = None
    sample_status: SampleStatus
    quality_reasons: list[str] = Field(default_factory=list)
    reward: float | None = Field(default=None, le=0, allow_inf_nan=False)
    reward_version: Literal['negative-log1p-ms-v1'] = 'negative-log1p-ms-v1'
    created_at: datetime = Field(default_factory=utc_now)

    @field_validator('created_at')
    @classmethod
    def utc(cls, value: datetime) -> datetime:
        return ensure_utc(value)

    @model_validator(mode='after')
    def coherent(self) -> 'Experience':
        if self.quality_version == 2 and not (self.resource_policy_version and self.resource_policy_hash):
            raise ValueError('quality v2 requires the assessed resource policy identity')
        if (self.query_id, self.decision_id, self.execution_id, self.strategy_id) != (
                self.record.query_id, self.record.decision_id, self.record.execution_id,
                self.record.strategy_id):
            raise ValueError('experience and execution record IDs differ')
        if self.features.query_id != self.query_id or self.features.query_hash != self.query_hash:
            raise ValueError('query features do not match experience')
        if self.sample_status != SampleStatus.VALID and self.reward is not None:
            raise ValueError('only VALID samples may have reward')
        if self.sample_status == SampleStatus.VALID:
            if (not self.record.success or self.record.timed_out or self.is_fallback
                    or self.record.runtime_result_check == 'MISMATCHED'
                    or self.record.strategy_certification_status != 'PASSED'
                    or self.context.result_rows != self.record.rows_returned
                    or self.reward is None):
                raise ValueError('VALID requires certified successful execution with reward')
        return self

    @property
    def group_key(self) -> str:
        return digest([self.template_key, self.feature_key, self.context.key])


class StrategyStatistics(BaseModel):
    model_config = ConfigDict(extra='forbid', frozen=True)
    strategy_id: str
    strategy_version: str
    observed_count: int
    valid_count: int
    success_rate: float | None
    mean_latency_ms: float | None
    median_latency_ms: float | None
    variance_ms2: float | None
    mean_reward: float | None
    eligible: bool


class L1Model(BaseModel):
    model_config = ConfigDict(extra='forbid', frozen=True)
    schema_version: Literal[1] = 1
    model_version: str = Field(default_factory=new_id)
    group_key: str
    quality_version: Literal[1, 2] = 1
    resource_policy_hash: str | None = None
    data_revision: int = Field(ge=0)
    sample_execution_ids: tuple[str, ...]
    min_samples: int = Field(ge=2)
    min_improvement: float = Field(ge=0, lt=1)
    statistics: tuple[StrategyStatistics, ...]
    created_at: datetime = Field(default_factory=utc_now)

    @field_validator('created_at')
    @classmethod
    def utc(cls, value: datetime) -> datetime:
        return ensure_utc(value)


class LearningFeedback(BaseModel):
    model_config = ConfigDict(extra='forbid')
    query_id: str
    execution_id: str
    sample_status: SampleStatus = SampleStatus.INVALID
    experience_id: str | None = None
    reward: float | None = None
    model_updated: bool = False
    model_version: str | None = None
    quarantine_persistence_pending: bool = False
    duplicate: bool = False
    quality_reasons: list[str] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)
