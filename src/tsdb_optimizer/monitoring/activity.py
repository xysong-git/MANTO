"""Shared CPU screening evidence contract; never reclassifies client sources."""
from datetime import datetime
from typing import Literal
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator
from ..schemas.common import ensure_utc, utc_now
from ..schemas.monitoring import SystemSnapshot, SystemSourceType


def cpu_bucket(snapshot: SystemSnapshot, at: datetime) -> str:
    if snapshot.source_type != SystemSourceType.DB_HOST:
        raise ValueError('activity requires db_host source')
    if not snapshot.is_usable(now=at) or snapshot.cpu_percent is None:
        return 'UNKNOWN'
    return 'HIGH' if snapshot.cpu_percent > 90 else 'MEDIUM' if snapshot.cpu_percent >= 50 else 'LOW'


class ActivityObservation(BaseModel):
    model_config = ConfigDict(extra='forbid', allow_inf_nan=False)
    protocol: Literal['cpu-activity-v2','cpu-activity-v3'] = 'cpu-activity-v2'
    sampled_at: datetime
    elapsed_s: float = Field(ge=0)
    db_host: SystemSnapshot
    cpu_bucket: Literal['LOW','MEDIUM','HIGH','UNKNOWN']
    memory_pressure: Literal['UNKNOWN'] = 'UNKNOWN'
    disk_saturation: Literal['UNKNOWN'] = 'UNKNOWN'

    @field_validator('sampled_at')
    @classmethod
    def utc(cls, value):
        return ensure_utc(value)

    @model_validator(mode='after')
    def coherent(self):
        if self.protocol=='cpu-activity-v3' and self.db_host.cpu_measurement!='cpu-times-instance-v1':
            raise ValueError('v3 requires independent CPU counters')
        if self.cpu_bucket != cpu_bucket(self.db_host, self.sampled_at):
            raise ValueError('CPU bucket disagrees with source/time/value')
        return self


def observation(snapshot, elapsed):
    at=utc_now()
    return ActivityObservation(protocol='cpu-activity-v3' if snapshot.cpu_measurement else 'cpu-activity-v2',sampled_at=at,elapsed_s=elapsed,db_host=snapshot,
                               cpu_bucket=cpu_bucket(snapshot,at))


def read_observation(raw):
    # Strict compatibility with old computed-field output, without changing it.
    body=dict(raw); body['db_host']=dict(body['db_host'])
    expiry=body['db_host'].pop('valid_until',None)
    result=ActivityObservation.model_validate(body)
    if expiry is not None and ensure_utc(datetime.fromisoformat(expiry)) != result.db_host.valid_until:
        raise ValueError('computed snapshot expiry mismatch')
    return result
