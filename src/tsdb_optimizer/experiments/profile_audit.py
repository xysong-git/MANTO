"""Strict screening transcript audit. Aborted runs remain inspectable, never complete."""
from collections import Counter
from datetime import datetime
import hashlib
import json
import math
from typing import Literal
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator
from ..monitoring.activity import read_observation
from ..schemas.common import TimingMetrics, ensure_utc
from ..sql_guard import validate_readonly_sql, sql_sha256

Hash = str


class Case(BaseModel):
    model_config=ConfigDict(extra='forbid',strict=True)
    scale: Literal['small_scale','medium_scale','large_scale']
    type: Literal['q03_count_aggregation','q04_sum_aggregation','q05_group_by_time','q06_group_by_time_value_filter']
    path: str = Field(min_length=1)
    line: int = Field(ge=1)
    file_sha256: str = Field(pattern=r'^[0-9a-f]{64}$')
    sql_sha256: str = Field(pattern=r'^[0-9a-f]{64}$')
    sql: str

    @model_validator(mode='after')
    def sql_identity(self):
        if sql_sha256(validate_readonly_sql(self.sql)) != self.sql_sha256:
            raise ValueError('case SQL hash mismatch')
        return self


class PlannedRound(BaseModel):
    model_config=ConfigDict(extra='forbid',strict=True)
    repeat: int = Field(ge=0)
    workers: Literal[1,2,4]
    case: Case


class Result(BaseModel):
    model_config=ConfigDict(extra='forbid',allow_inf_nan=False)
    execution_id: str = Field(min_length=1)
    status: Literal['SUCCESS','ERROR']
    strategy_id: Literal['S0_NATIVE']
    row_count: int = Field(ge=0,strict=True)
    metrics: TimingMetrics
    fetch_mode: Literal['FULL']
    started_at: datetime
    finished_at: datetime
    error_type: str | None
    warnings: list[str]
    fallback: Literal[False]
    error_message: str | None = None
    request_completed: bool | None = None  # absent in original first journal
    sql_sha256: str | None = Field(default=None,pattern=r'^[0-9a-f]{64}$')

    @field_validator('started_at','finished_at')
    @classmethod
    def utc(cls,value): return ensure_utc(value)

    @model_validator(mode='after')
    def coherent(self):
        if self.finished_at < self.started_at or not all(math.isfinite(v) for v in self.metrics.model_dump().values()):
            raise ValueError('invalid result timing')
        if self.status=='SUCCESS' and (self.error_type or self.error_message or self.request_completed is False):
            raise ValueError('successful result contradicts failure metadata')
        return self


def summarize(path):
    events=[json.loads(line) for line in path.read_text().splitlines()]
    if not events or events[0]['kind']!='protocol': raise ValueError('missing protocol')
    protocol=events[0]['body']
    if protocol.get('protocol') not in ('cpu-activity-v2','cpu-activity-v3') or protocol.get('purpose')!='NATIVE_SCREENING_NOT_LEARNING':
        raise ValueError('unsupported screening protocol')
    if protocol.get('max_connections')!=4 or protocol.get('round_limit_s')!=60 or protocol.get('query_timeout_ms')!=30000:
        raise ValueError('unexpected screening bounds')
    if protocol.get('cpu_boundaries_percent')!=[50,90]: raise ValueError('CPU boundary mismatch')
    plan=[PlannedRound.model_validate(b) for b in protocol['schedule']]
    if not plan or protocol.get('repeats') not in (1,2,3): raise ValueError('invalid schedule')
    keys=[(p.repeat,p.workers,p.case.path,p.case.line) for p in plan]
    if len(set(keys))!=len(keys) or any(p.repeat>=protocol['repeats'] for p in plan):
        raise ValueError('duplicate or invalid planned round')
    starts,ends,samples={},{},{}
    seen_ids=set(); active=None; terminal=None; failed=False; safety_stop=False
    sql_binding_verified=True
    for item in events[1:]:
        if terminal is not None: raise ValueError('records after terminal event')
        b=item['body'];kind=item['kind']
        if kind=='round_start':
            idx=b['index']
            if active is not None or failed or safety_stop or type(idx) is not int or idx!=len(starts) or idx>=len(plan):
                raise ValueError('invalid round start/order')
            actual=PlannedRound.model_validate({k:v for k,v in b.items() if k not in ('index','before')})
            if actual!=plan[idx]: raise ValueError('round differs from declared schedule')
            before=read_observation(b['before'])
            if before.protocol!=protocol['protocol']:raise ValueError('observation protocol mismatch')
            if before.elapsed_s!=0: raise ValueError('invalid pre-action elapsed time')
            starts[idx]=b;active=idx
        elif kind=='sample':
            idx=b['index']
            if active is None or idx!=active: raise ValueError('sample outside active round')
            obs=read_observation({k:v for k,v in b.items() if k!='index'})
            if obs.protocol!=protocol['protocol']:raise ValueError('observation protocol mismatch')
            previous=samples.get(idx,[])
            first=read_observation(starts[idx]['before'])
            if (obs.elapsed_s>60 or obs.elapsed_s<(previous[-1].elapsed_s if previous else 0)
                    or obs.sampled_at<(previous[-1].sampled_at if previous else first.sampled_at)):
                raise ValueError('sample time outside round')
            if obs.db_host.source_host!=first.db_host.source_host:
                raise ValueError('sample host changed within round')
            samples.setdefault(idx,[]).append(obs)
        elif kind=='round_complete':
            idx=b['index']
            if active is None or idx!=active: raise ValueError('invalid round completion')
            elapsed=b['elapsed_s']
            if type(elapsed) not in (int,float) or not math.isfinite(elapsed) or elapsed<0:
                raise ValueError('invalid elapsed time')
            if samples.get(idx) and elapsed<samples[idx][-1].elapsed_s: raise ValueError('completion before sample')
            results=[Result.model_validate(row) for row in b['results']]
            if len(results)!=plan[idx].workers: raise ValueError('result count differs from workers')
            for r in results:
                if r.sql_sha256 is None:
                    sql_binding_verified=False
                    if protocol.get('result_identity_version')==2 or protocol.get('process_supervisor'):
                        raise ValueError('missing executed SQL identity')
                if r.started_at < read_observation(starts[idx]['before']).sampled_at:
                    raise ValueError('execution precedes pre-action observation')
                if r.execution_id in seen_ids: raise ValueError('duplicate execution ID')
                seen_ids.add(r.execution_id)
                if r.sql_sha256 is not None and r.sql_sha256!=plan[idx].case.sql_sha256:
                    raise ValueError('executed SQL differs from schedule')
            failed=elapsed>60 or any(r.status!='SUCCESS' or r.warnings for r in results)
            ends[idx]=b;active=None
        elif kind=='safety_stop':
            if active is not None or safety_stop: raise ValueError('invalid safety stop')
            safety_stop=True
        elif kind=='aborted':
            terminal='aborted'
        elif kind=='complete':
            if active is not None or failed or safety_stop or len(ends)!=len(plan) or b.get('rounds')!=len(plan):
                raise ValueError('incomplete or failed schedule cannot complete')
            terminal='complete'
        else:
            raise ValueError('unknown journal event')
    if terminal is None: raise ValueError('journal lacks terminal event')
    rounds=[]
    for idx,start in starts.items():
        end=ends.get(idx);obs=samples.get(idx,[])
        cpu=[x.db_host.cpu_percent for x in obs if x.cpu_bucket!='UNKNOWN']
        rounds.append({'index':idx,'scale':start['case']['scale'],'type':start['case']['type'],
            'workers':start['workers'],'repeat':start['repeat'],'sql_sha256':start['case']['sql_sha256'],
            'elapsed_s':end['elapsed_s'] if end else None,'results':end['results'] if end else [],
            'sample_count':len(obs),'cpu_min_percent':min(cpu) if cpu else None,
            'cpu_max_percent':max(cpu) if cpu else None,'cpu_mean_percent':sum(cpu)/len(cpu) if cpu else None,
            'cpu_buckets':dict(Counter(x.cpu_bucket for x in obs))})
    return {'protocol':protocol['protocol'],'purpose':protocol['purpose'],
        'schedule_complete':terminal=='complete',
        'execution_sql_binding_verified':bool(seen_ids) and sql_binding_verified,
        'all_executions_successful':terminal=='complete' and not failed,
        'journal_sha256':hashlib.sha256(path.read_bytes()).hexdigest(),'journal':str(path),
        'epoch':protocol['epoch'],'rounds':rounds,'online_ready':False,
        'limitations':protocol['limitations']+['screening is not learning readiness evidence',
            'shared host CPU includes other processes; old results may lack per-execution SQL hashes']}
