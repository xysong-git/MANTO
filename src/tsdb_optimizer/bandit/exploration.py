"""Offline randomized evidence protocol. Never enables a deployed model."""
from __future__ import annotations

import math
import random
from collections import Counter
from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from ..schemas.common import ensure_utc, new_id, utc_now
from ..schemas.learning import Experience

STRATEGIES = ('S0_NATIVE', 'S1_REPEAT_CACHE')


class Assignment(BaseModel):
    model_config = ConfigDict(extra='forbid', frozen=True)
    protocol_version: Literal['randomized-cache-state-v1'] = 'randomized-cache-state-v1'
    assignment_id: str = Field(default_factory=new_id)
    assigned_at: datetime = Field(default_factory=utc_now)
    phase: Literal['train', 'holdout']
    seed: int
    index: int = Field(ge=0)
    workload_sha256: str = Field(pattern=r'^[0-9a-f]{64}$')
    cache_state_before: Literal['EMPTY', 'READY']
    background_workers: int = Field(ge=0, le=3)
    strategy_id: Literal['S0_NATIVE', 'S1_REPEAT_CACHE']
    propensity: Literal[0.5] = 0.5

    @field_validator('assigned_at')
    @classmethod
    def utc(cls, value):
        return ensure_utc(value)


class Trial(BaseModel):
    model_config = ConfigDict(extra='forbid', allow_inf_nan=False)
    assignment: Assignment
    experience: Experience
    service_wall_ms: float = Field(ge=0)
    background_started: int = Field(ge=0)
    background_completed: int = Field(ge=0)
    background_inflight_before: int = Field(ge=0, le=3)

    @model_validator(mode='after')
    def coherent(self):
        e, a = self.experience, self.assignment
        if (self.background_started-self.background_completed != self.background_inflight_before
                or self.background_inflight_before > a.background_workers):
            raise ValueError('background counters are inconsistent')
        if e.strategy_id != a.strategy_id or a.assigned_at > e.record.started_at:
            raise ValueError('assignment must precede the selected execution')
        if e.query_hash != a.workload_sha256:
            raise ValueError('assignment SQL identity mismatch')
        if e.context.result_source != ('CACHE_HIT' if a.cache_state_before == 'READY'
                                       and a.strategy_id == 'S1_REPEAT_CACHE' else 'SERVER'):
            raise ValueError('result source differs from controlled cache state')
        return self


def draw(rng: random.Random, **context) -> Assignment:
    # Independent draws: every action retains positive known conditional probability.
    return Assignment(strategy_id=rng.choice(STRATEGIES), **context)


def summarize(trials: list[Trial], *, min_samples=30) -> dict:
    ids = [t.experience.execution_id for t in trials]
    assignments = [t.assignment.assignment_id for t in trials]
    if len(set(ids)) != len(ids) or len(set(assignments)) != len(assignments):
        raise ValueError('duplicate assignment or execution')
    train = [t for t in trials if t.assignment.phase == 'train']
    test = [t for t in trials if t.assignment.phase == 'holdout']
    if train and test and max(t.experience.record.finished_at for t in train) >= min(
            t.experience.record.started_at for t in test):
        raise ValueError('training and holdout windows overlap')
    cells = {}
    for t in trials:
        a, e = t.assignment, t.experience
        key = f'{a.phase}/{a.cache_state_before}/background={a.background_workers}'
        cell = cells.setdefault(key, {'counts': Counter(), 'quality': Counter(),
            'measured_load_buckets': Counter(), 'result_sources': Counter(), 'latencies_ms': {}})
        cell['quality'][e.sample_status.value] += 1
        cell['measured_load_buckets'][e.context.load_bucket] += 1
        cell['result_sources'][e.context.result_source] += 1
        if e.sample_status.value == 'VALID':
            cell['counts'][a.strategy_id] += 1
            cell['latencies_ms'].setdefault(a.strategy_id, []).append(t.service_wall_ms)
    for cell in cells.values():
        cell['per_arm_sample_gate'] = all(cell['counts'][s] >= min_samples for s in STRATEGIES)
        # Cache-result source is reported, never pooled into the legacy training partition.
        # Estimate a fixed-arm contrast only within randomized pre-action cells.
        means = {s: sum(v)/len(v) for s,v in cell['latencies_ms'].items()}
        cell['mean_service_ms'] = means
        cell['s1_minus_s0_mean_ms'] = (means[STRATEGIES[1]]-means[STRATEGIES[0]]
                                      if len(means)==2 else None)
        cell['confidence_interval'] = None  # No unsupported significance claim.
    return {'unique_executions': len(ids), 'cells': cells, 'online_ready': False,
        'limitations': ['background worker count is not DB host CPU or total DB concurrency',
            'fixed-arm observational summaries are not adaptive policy benefit',
            'post-action SERVER/CACHE_HIT partitions remain isolated in the legacy model',
            'no confidence interval or causal adaptive benefit asserted']}
