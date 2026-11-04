"""Pinned workloads and deterministic, independently replayable allocations."""
from __future__ import annotations

import hashlib
import json
import random
import re
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, StrictInt, model_validator

from ..sql_guard import sql_sha256, validate_readonly_sql
from ..errors import UnsafeSQLError

GROUPS = ('E0', 'E1', 'E2', 'E3', 'E4', 'A1', 'A2')
GROUP_DESCRIPTIONS = {
    'E0': 'Native through audited baseline service; no learning/agent monitoring',
    'E1': 'Fixed registered S1 preference; applicability and Native fallback retained',
    'E2': 'Existing rule-based decision/execution service; no learning',
    'E3': 'Four-agent rule-based workflow; experience recording, no model updates/advice',
    'E4': 'Four-agent frozen LinUCB learned from earlier real training requests',
    'A1': 'E4 with client/DB CPU and memory model features masked in fit and inference',
    'A2': 'E4 without historical model/advice or updates; existing rule-based fallback',
}


class Contract(BaseModel):
    model_config = ConfigDict(extra='forbid', frozen=True, allow_inf_nan=False)


class Query(Contract):
    sql: str = Field(min_length=1)
    # Only integer literals are interpolated; identifiers/SQL fragments are not parameters.
    parameters: dict[str, StrictInt] = Field(default_factory=dict)
    resolved_sha256: str = Field(pattern=r'^[0-9a-f]{64}$')
    expected_rows: int = Field(ge=0, le=1000)
    fetch_mode: Literal['FULL'] = 'FULL'
    ordering: Literal['ORDERED', 'UNORDERED'] = 'UNORDERED'

    def resolved(self):
        placeholders = re.findall(r'\{([A-Za-z_][A-Za-z_0-9]*)\}', self.sql)
        if set(placeholders) != set(self.parameters):
            raise ValueError('SQL literal parameters must exactly match placeholders')
        sql = self.sql
        for name, value in self.parameters.items():
            if type(value) is not int:
                raise ValueError('only integer SQL literal parameters are supported')
            sql = sql.replace('{'+name+'}', str(value))
        if '{' in sql or '}' in sql:
            raise ValueError('unsupported SQL template syntax')
        try:
            cleaned = validate_readonly_sql(sql)
        except UnsafeSQLError as exc:
            raise ValueError('benchmark workload must be a single safe read-only SELECT') from exc
        if sql_sha256(cleaned) != self.resolved_sha256:
            raise ValueError('resolved SQL differs from pinned checksum')
        return cleaned

    @model_validator(mode='after')
    def pinned(self):
        self.resolved()
        return self


class Workload(Contract):
    name: str = Field(pattern=r'^[a-z][a-z0-9_]{0,50}$')
    train: Query
    holdout: Query
    source_scale: Literal['small_scale', 'medium_scale', 'large_scale']


class Dataset(Contract):
    epoch: str = Field(min_length=1)
    manifest: str
    manifest_sha256: str = Field(pattern=r'^[0-9a-f]{64}$')
    server_version: Literal['2.0.6'] = '2.0.6'
    database_model: Literal['tree'] = 'tree'
    immutable: Literal[True] = True


class Analysis(Contract):
    confidence: float = Field(default=.99, gt=.5, lt=1)
    bootstrap_replicates: int = Field(default=4000, ge=200, le=10000)
    seed: int = 20261110
    minimum_blocks: int = Field(default=30, ge=2)
    float_abs_tolerance: float = Field(default=0., ge=0)
    float_rel_tolerance: float = Field(default=0., ge=0)


class BenchmarkConfig(Contract):
    schema_version: Literal[1] = 1
    protocol: Literal['phase8-request-e2e-v1'] = 'phase8-request-e2e-v1'
    name: str = Field(pattern=r'^[a-z][a-z0-9_]{0,60}$')
    dataset: Dataset
    workloads: list[Workload] = Field(min_length=1, max_length=20)
    groups: list[Literal['E0', 'E1', 'E2', 'E3', 'E4', 'A1', 'A2']] = Field(default_factory=lambda: list(GROUPS))
    # These are application-cache conditions, never claims of OS/IoTDB cold cache.
    cache_conditions: list[Literal['APPLICATION_EMPTY', 'APPLICATION_WARM']] = Field(
        default_factory=lambda: ['APPLICATION_EMPTY', 'APPLICATION_WARM'])
    loads: list[Literal['LOW', 'MEDIUM']] = Field(default_factory=lambda: ['LOW'])
    load_driver: Literal['ambient', 'cpu_cotenant'] = 'ambient'
    concurrency: list[int] = Field(default_factory=lambda: [1])
    warmup: int = Field(default=2, ge=0, le=20)
    requests_per_lane: int = Field(default=8, ge=1, le=100)
    train_blocks: int = Field(default=30, ge=1, le=500)
    test_blocks: int = Field(default=30, ge=2, le=500)
    order: Literal['random', 'ABBA'] = 'random'
    seed: int = 20261110
    min_samples: int = Field(default=30, ge=30, le=10000)
    timeout_ms: int = Field(default=30000, ge=1, le=30000)
    round_limit_s: int = Field(default=60, ge=10, le=60)
    analysis: Analysis = Field(default_factory=Analysis)
    # Algo and resource policy are fixed. They are not knobs for result fitting.
    training_mode: Literal['randomized_then_frozen'] = 'randomized_then_frozen'
    uncontrolled_factors: list[str] = Field(default_factory=lambda: [
        'IoTDB/OS/storage caches are UNKNOWN; application reset is not a cold-server reset',
        'DB_HOST metrics describe the whole host, not exclusive IoTDB process/query usage',
        'TCP boundary checks cannot rule out short-lived external clients between checks',
        'Thread scheduling, background services, thermal/frequency drift remain uncontrolled',
        'Frozen holdout does not measure continuous online updating or dynamic-write freshness',
        'Request reward does not attribute cache-fill cost to unknown future requests',
        'Phase 7 is NOT ACCEPTED; this study does not change production readiness',
    ])

    @model_validator(mode='after')
    def coherent(self):
        for field in ('groups', 'cache_conditions', 'loads', 'concurrency'):
            values = getattr(self, field)
            if not values or len(values) != len(set(values)):
                raise ValueError(f'{field} must be nonempty and unique')
        if any(type(n) is not int or not 1 <= n <= 4 for n in self.concurrency):
            raise ValueError('concurrency must be bounded integers 1..4')
        if len({w.name for w in self.workloads}) != len(self.workloads):
            raise ValueError('duplicate workload name')
        train = {w.train.resolved_sha256 for w in self.workloads}
        test = {w.holdout.resolved_sha256 for w in self.workloads}
        if train & test:
            raise ValueError('training and holdout SQL hashes must be disjoint')
        if len(train) != len(self.workloads) or len(test) != len(self.workloads):
            raise ValueError('duplicate SQL across workload cases')
        if self.order == 'ABBA' and len(self.groups) != 2:
            raise ValueError('ABBA is a two-group crossover; use random for seven groups')
        if self.load_driver == 'ambient' and self.loads != ['LOW']:
            raise ValueError('ambient driver supports LOW only; do not relabel ambient as MEDIUM')
        return self

    @property
    def checksum(self):
        return checksum(self.model_dump(mode='json'))


class Allocation(Contract):
    allocation_id: str
    phase: Literal['TRAIN', 'HOLDOUT']
    block: int = Field(ge=0)
    workload: str
    cache_condition: Literal['APPLICATION_EMPTY', 'APPLICATION_WARM']
    target_load: Literal['LOW', 'MEDIUM']
    concurrency: int = Field(ge=1, le=4)
    group: Literal['TRAIN_S0', 'TRAIN_S1', 'E0', 'E1', 'E2', 'E3', 'E4', 'A1', 'A2']
    position: int = Field(ge=0)
    repeat: int = Field(ge=0, le=1)


def checksum(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(',', ':'), allow_nan=False).encode()).hexdigest()


def load_config(path):
    return BenchmarkConfig.model_validate_json(Path(path).read_text())


def schedule(config):
    rng = random.Random(config.seed)
    rows = []
    for phase, blocks in (('TRAIN', config.train_blocks), ('HOLDOUT', config.test_blocks)):
        for block in range(blocks):
            cells = [(w.name, cache, load, n) for w in config.workloads
                     for cache in config.cache_conditions for load in config.loads for n in config.concurrency]
            rng.shuffle(cells)
            for workload, cache, load, n in cells:
                groups = ['TRAIN_S0', 'TRAIN_S1'] if phase == 'TRAIN' else list(config.groups)
                rng.shuffle(groups)
                if phase == 'HOLDOUT' and config.order == 'ABBA':
                    groups = [groups[0], groups[1], groups[1], groups[0]]
                seen = {}
                for position, group in enumerate(groups):
                    repeat = seen.get(group, 0)
                    seen[group] = repeat + 1
                    body = dict(phase=phase, block=block, workload=workload, cache_condition=cache,
                        target_load=load, concurrency=n, group=group, position=position, repeat=repeat)
                    rows.append(Allocation(allocation_id=checksum([config.checksum, body])[:24], **body))
    return rows
