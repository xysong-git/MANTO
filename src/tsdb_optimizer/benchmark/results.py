"""Result equivalence and raw request schema, with no hidden comparison queries."""
from datetime import datetime
import math
from typing import Literal

from pydantic import Field, field_validator, model_validator

from ..execution.models import OptimizationRun
from ..orchestration.models import WorkflowResult
from ..schemas.common import ensure_utc
from ..schemas.execution import ExecutionResult
from ..schemas.learning import Experience, load_identity
from ..schemas.monitoring import SystemSample
from .protocol import Allocation, Contract, checksum


def result_matches(result, reference, query, analysis):
    if (result.status.value != 'SUCCESS' or reference.status.value != 'SUCCESS'
            or result.fetch_mode.value != 'FULL' or reference.fetch_mode.value != 'FULL'
            or result.preview_truncated or reference.preview_truncated
            or result.row_count != query.expected_rows or reference.row_count != query.expected_rows
            or len(result.rows_preview) != result.row_count or len(reference.rows_preview) != reference.row_count
            or result.columns != reference.columns):
        return False
    if query.ordering == 'UNORDERED':
        # Exact multiset comparison preserves duplicate multiplicity.
        return sorted(checksum(row) for row in result.rows_preview) == sorted(checksum(row) for row in reference.rows_preview)
    def cell(a, b):
        if isinstance(a, (int, float)) and not isinstance(a, bool) and isinstance(b, (int, float)) and not isinstance(b, bool):
            return math.isfinite(a) and math.isfinite(b) and math.isclose(a, b,
                rel_tol=analysis.float_rel_tolerance, abs_tol=analysis.float_abs_tolerance)
        return type(a) is type(b) and a == b
    return all(len(a) == len(b) and all(cell(x, y) for x, y in zip(a, b))
               for a, b in zip(result.rows_preview, reference.rows_preview))


class RequestRecord(Contract):
    schema_version: Literal[1] = 1
    allocation: Allocation
    lane: int = Field(ge=0)
    index: int = Field(ge=0)
    role: Literal['WARMUP', 'CACHE_PREP', 'MEASURED']
    sql_sha256: str = Field(pattern=r'^[0-9a-f]{64}$')
    started_at: datetime
    finished_at: datetime
    elapsed_ns: int = Field(gt=0)
    sample: SystemSample
    sample_at: datetime
    observed_load: Literal['LOW', 'MEDIUM', 'HIGH', 'UNKNOWN']
    cache_before: Literal['EMPTY', 'READY']
    cache_entries_after: int = Field(ge=0)
    correctness: Literal['MATCHED', 'MISMATCHED', 'NOT_CHECKED']
    run: OptimizationRun | None = None
    workflow: WorkflowResult | None = None
    experiences: list[Experience] = Field(default_factory=list)
    error_type: str | None = None
    inference: dict | None = None

    @field_validator('started_at', 'finished_at', 'sample_at')
    @classmethod
    def utc(cls, value):
        return ensure_utc(value)

    @model_validator(mode='after')
    def linked(self):
        if self.lane >= self.allocation.concurrency:
            raise ValueError('lane outside concurrency plan')
        if not self.started_at <= self.sample_at <= self.finished_at:
            raise ValueError('sample admission outside request timer')
        if self.observed_load != load_identity(self.sample, 'db_host', self.sample_at, protocol='cpu-activity-v3')[1]:
            raise ValueError('observed load differs from source/time/CPU evidence')
        for source in ('client_host', 'db_host'):
            snapshot = getattr(self.sample, source)
            if snapshot.source_type.value != source or not snapshot.is_usable(now=self.sample_at, max_age_ms=250):
                raise ValueError('invalid pre-request safety snapshot')
            if any(getattr(snapshot, name) is None or getattr(snapshot, name) > limit for name, limit in
                   (('cpu_percent', 80), ('memory_percent', 90), ('disk_usage_percent', 90))):
                raise ValueError('unsafe pre-request snapshot')
        if self.finished_at < self.started_at:
            raise ValueError('request clock moved backwards')
        if abs((self.finished_at-self.started_at).total_seconds()*1e9-self.elapsed_ns) > 5_000_000:
            raise ValueError('wall and monotonic timers disagree')
        if self.workflow is not None and self.workflow.run != self.run:
            raise ValueError('workflow/run mismatch')
        if self.run is None:
            if not self.error_type or self.correctness != 'NOT_CHECKED':
                raise ValueError('missing execution must remain unknown with explicit error')
        else:
            attempts = self.run.execution.attempts
            if self.run.decision.original_sql and any(a.execution.sql_sha256 != self.sql_sha256 for a in attempts):
                raise ValueError('executed SQL differs from pinned workload')
            if self.elapsed_ns/1e6 < sum(a.execution.metrics.client_wall_ms for a in attempts):
                raise ValueError('outer request timer excludes execution/fallback')
            if any(a.execution.started_at < self.started_at or a.execution.finished_at > self.finished_at for a in attempts):
                raise ValueError('execution outside request interval')
            if self.correctness == 'MATCHED' and attempts[-1].execution.status.value != 'SUCCESS':
                raise ValueError('failed query cannot match reference')
            if self.workflow is not None and (self.started_at > self.workflow.trace.events[0].at
                    or self.finished_at < self.workflow.trace.events[-1].at):
                raise ValueError('timer excludes workflow')
        return self

    @property
    def end_to_end_ms(self):
        return self.elapsed_ns/1e6

    @property
    def success(self):
        return self.run is not None and self.run.execution.attempts[-1].execution.status.value == 'SUCCESS'
