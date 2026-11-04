"""Whole-sequence accounting. Outcome admission is separate from learning admission."""
from collections import Counter
from datetime import datetime
import random
import statistics
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from ..orchestration.models import WorkflowResult
from ..schemas.common import ensure_utc
from ..schemas.learning import Experience
from ..schemas.monitoring import SystemSample

POLICIES = ('S0_NATIVE', 'S1_REPEAT_CACHE', 'ADAPTIVE')
LENGTHS = (1, 2, 8)
SEED = 20261106


def schedule(blocks=30):
    rng = random.Random(SEED)
    for block in range(blocks):
        lengths = list(LENGTHS)
        rng.shuffle(lengths)
        for length in lengths:
            policies = list(POLICIES)
            rng.shuffle(policies)
            for policy in policies:
                yield block, length, policy


class SequenceRequest(BaseModel):
    model_config = ConfigDict(extra='forbid', allow_inf_nan=False)
    block: int = Field(ge=0, lt=45)
    length: Literal[1, 2, 8]
    policy: Literal['S0_NATIVE', 'S1_REPEAT_CACHE', 'ADAPTIVE', 'L1_BATCH']
    index: int = Field(ge=0)
    cache_before: Literal['EMPTY', 'READY']
    cache_entries_after: int = Field(ge=0, le=1)
    started_at: datetime
    finished_at: datetime
    elapsed_ns: int = Field(gt=0, strict=True)
    workflow: WorkflowResult
    experiences: list[Experience]
    admission_sample: SystemSample | None = None
    admission_at: datetime | None = None

    @field_validator('started_at', 'finished_at', 'admission_at')
    @classmethod
    def utc(cls, value):
        return ensure_utc(value)

    @property
    def e2e_ms(self):
        return self.elapsed_ns / 1_000_000

    @model_validator(mode='after')
    def coherent(self):
        w = self.workflow
        if self.admission_sample is not None:
            at = self.admission_at
            if at is None or not self.started_at <= at <= self.finished_at:
                raise ValueError('admission outside timed request')
            for name in ('client_host', 'db_host'):
                s = getattr(self.admission_sample, name)
                if s.source_type.value != name or s.stale_after_ms != 500 or not s.is_usable(now=at, max_age_ms=250):
                    raise ValueError('invalid/stale admission sample')
            s = self.admission_sample.db_host
            if any(getattr(s, k) is None or getattr(s, k) > limit for k, limit in
                    [('cpu_percent', 80), ('memory_percent', 90), ('disk_usage_percent', 90)]):
                raise ValueError('unsafe admission sample')
        if self.index >= self.length or not w.trace.events:
            raise ValueError('invalid request position')
        if w.request_timing is not None and (
                w.request_timing.total_ms > self.e2e_ms
                or w.request_timing.body_ms + .001 < w.trace.events[-1].elapsed_ms):
            raise ValueError('request timing excludes workflow or exceeds outer timer')
        if (self.started_at > w.trace.events[0].at
                or self.finished_at < w.trace.events[-1].at
                or self.e2e_ms < w.trace.events[-1].elapsed_ms
                or abs((self.finished_at-self.started_at).total_seconds()*1000-self.e2e_ms) > 5):
            raise ValueError('request timer does not enclose workflow')
        if w.run is None:
            raise ValueError('missing execution evidence')
        attempts = w.run.execution.attempts
        if self.e2e_ms < sum(a.execution.metrics.client_wall_ms for a in attempts):
            raise ValueError('timer excludes execution/fallback cost')
        if (self.started_at > min(a.execution.started_at for a in attempts)
                or self.finished_at < max(a.execution.finished_at for a in attempts)):
            raise ValueError('request interval excludes attempt')
        ids = [e.execution_id for e in self.experiences]
        if len(ids) != len(set(ids)) or set(ids) != {a.execution.execution_id for a in attempts}:
            raise ValueError('missing/duplicate attempt experience')
        for e in self.experiences:
            if (e.query_id != w.trace.query_id or e.decision_id != w.run.decision.decision_id
                    or e.query_hash != w.trace.sql_sha256 or e.record not in w.records):
                raise ValueError('experience/workflow mismatch')
            if w.run.execution.fallback_used and (e.sample_status.value == 'VALID' or e.reward is not None):
                raise ValueError('fallback cannot train')
        if w.run.decision.query_occurrence != self.index+1:
            raise ValueError('first visit was hidden or occurrence not reset')
        if self.index == 0 and (self.cache_before != 'EMPTY'
                or w.run.decision.selected_strategy_id != 'S0_NATIVE'):
            raise ValueError('first request must be empty Native')
        return self


def validate_sequence(rows, reference, *, sql_hash, epoch):
    """Accept safe fallbacks for cost accounting, never as valid learning labels."""
    from ..schemas.execution import ExecutionStatus
    first = rows[0]
    if len(rows) != first.length:
        raise ValueError('incomplete sequence')
    expected_cache = 'EMPTY'
    fills = set()
    ids = set()
    previous_finish = None
    for i, row in enumerate(rows):
        if (row.block, row.length, row.policy, row.index) != (first.block, first.length, first.policy, i):
            raise ValueError('sequence identity/order differs')
        if row.cache_before != expected_cache:
            raise ValueError('unaccounted cache preparation or clearing')
        if previous_finish is not None and row.started_at < previous_finish:
            raise ValueError('overlapping requests')
        previous_finish = row.finished_at
        w = row.workflow
        if w.trace.sql_sha256 != sql_hash:
            raise ValueError('wrong sequence SQL')
        if w.trace.query_id in ids:
            raise ValueError('duplicate request')
        ids.add(w.trace.query_id)
        result = w.run.execution.attempts[-1].execution
        if (result.status != ExecutionStatus.SUCCESS or result.columns != reference.columns
                or result.rows_preview != reference.rows_preview or result.row_count != reference.row_count
                or result.preview_truncated or result.fetch_mode.value != 'FULL'):
            raise ValueError('failed/incomplete/mismatched result')
        for a in w.run.execution.attempts:
            r = a.execution
            if r.execution_id in ids:
                raise ValueError('duplicate execution')
            ids.add(r.execution_id)
            if r.served_from_cache and (r.cache_source_execution_id not in fills or r.cache_epoch != epoch):
                raise ValueError('cache hit not populated inside sequence')
            if r.strategy_id == 'S1_REPEAT_CACHE' and r.status == ExecutionStatus.SUCCESS and not r.served_from_cache:
                if r.cache_epoch != epoch:
                    raise ValueError('cache epoch differs')
                fills.add(r.execution_id)
        if row.cache_entries_after and not fills:
            raise ValueError('unmeasured cache fill')
        if expected_cache == 'READY' and not row.cache_entries_after:
            raise ValueError('unexpected cache clearing')
        expected_cache = 'READY' if row.cache_entries_after else 'EMPTY'
    return ids


def paired_report(sequences):
    """No filtering of low-confidence or fallback requests from sequence totals."""
    expected = set(schedule())
    if set(sequences) != expected:
        raise ValueError('incomplete planned sequence cells')
    output = {}
    for length in LENGTHS:
        totals = {p: [sum(r.e2e_ms for r in sequences[(b, length, p)]) for b in range(30)] for p in POLICIES}
        contrasts = {}
        for candidate, baseline in ((POLICIES[1], POLICIES[0]), (POLICIES[2], POLICIES[0]), (POLICIES[2], POLICIES[1])):
            delta = [a-b for a, b in zip(totals[candidate], totals[baseline])]
            rng = random.Random(SEED)
            boot = sorted(statistics.mean(rng.choices(delta, k=30)) for _ in range(4000))
            batches = [statistics.mean(delta[i:i+10]) for i in (0, 10, 20)]
            contrasts[candidate+'-'+baseline] = {'mean_ms': statistics.mean(delta),
                'ci99_ms': [boot[20], boot[3979]], 'three_batch_means_ms': batches,
                'faster_within_session': boot[3979] < 0 and all(v < 0 for v in batches)}
        rows = [r for (b, n, p), group in sequences.items() if n == length for r in group]
        output[str(length)] = {'mean_sequence_ms': {p: statistics.mean(v) for p, v in totals.items()},
            'contrasts': contrasts, 'requests': len(rows),
            'fallback_requests': sum(r.workflow.run.execution.fallback_used for r in rows),
            'sample_quality': dict(Counter(e.sample_status.value for r in rows for e in r.experiences)),
            'choices': {p: dict(Counter(r.workflow.run.execution.attempts[-1].execution.strategy_id
                for r in rows if r.policy == p)) for p in POLICIES},
            'hits': {p: sum(r.workflow.run.execution.attempts[-1].execution.served_from_cache
                for r in rows if r.policy == p) for p in POLICIES}}
    return output
