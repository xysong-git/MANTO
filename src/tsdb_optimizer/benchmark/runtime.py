"""Adapters for explicit experiments. A lane owns its connection, cache and SQLite."""
from __future__ import annotations

import time
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from ..adapters.iotdb.tree_adapter import IoTDBTreeAdapter
from ..agents import PerformanceMonitoringAgent
from ..agents.learning_feedback import LearningFeedbackAgent
from ..execution import DecisionExecutionService
from ..learning.repository import ExperienceRepository
from ..orchestration import Orchestrator
from ..schemas.common import utc_now
from ..schemas.learning import LearningContext, load_identity
from ..storage import SQLiteStore
from ..strategies import build_default_registry
from .policy import BenchmarkEngine
from .results import RequestRecord, result_matches


class RecordingLearner(LearningFeedbackAgent):
    """Preserve quality filtering/quarantine; no synchronous L1 updates or advice."""
    def recommend(self, *args, **kwargs):
        return None


class ReferenceMonitor(PerformanceMonitoringAgent):
    def __init__(self, *, reference, query, comparison, **kwargs):
        super().__init__(**kwargs)
        self.reference = reference
        self.query = query
        self.comparison = comparison

    def record_execution(self, execution, **kwargs):
        record = super().record_execution(execution, **kwargs)
        matched = result_matches(execution, self.reference, self.query, self.comparison)
        return record.model_copy(update={'runtime_result_check': 'MATCHED' if matched else 'MISMATCHED'})


class ResourceViolation(BaseModel):
    """Diagnostic evidence only; thresholds and admission behavior are unchanged."""
    model_config = ConfigDict(extra='forbid', frozen=True, allow_inf_nan=False)
    source: Literal['client_host', 'db_host']
    metric: Literal['snapshot', 'cpu_percent', 'memory_percent', 'disk_usage_percent']
    observed: float | None = Field(default=None, ge=0, le=100)
    limit: float | None = Field(default=None, ge=0, le=100)
    reason: Literal['UNUSABLE_SNAPSHOT', 'MISSING_METRIC', 'LIMIT_EXCEEDED']

    def describe(self):
        name = f'{self.source}.{self.metric}'
        if self.reason == 'UNUSABLE_SNAPSHOT':
            return f'{name}=UNUSABLE (source/validity/freshness check failed)'
        if self.reason == 'MISSING_METRIC':
            return f'{name}=UNKNOWN (required; limit={self.limit:g})'
        return f'{name}={self.observed:g} > {self.limit:g}'


class ResourceAdmissionError(RuntimeError):
    def __init__(self, reason, sample, at, violations=()):
        self.violations = tuple(violations)
        detail = '; '.join(item.describe() for item in self.violations)
        super().__init__(reason+(': '+detail if detail else ''))
        self.sample, self.at = sample, at
        self.request_role = None
        self.request_index = None


def admit(monitor):
    sample = monitor.sample_once()
    at = utc_now()
    violations = []
    for source in ('client_host', 'db_host'):
        snapshot = getattr(sample, source)
        if (snapshot is None or snapshot.source_type.value != source
                or not snapshot.is_usable(now=at, max_age_ms=250)):
            violations.append(ResourceViolation(source=source, metric='snapshot', reason='UNUSABLE_SNAPSHOT'))
            continue  # Unusable measurements must not be represented as valid excesses.
        for name, limit in (('cpu_percent', 80), ('memory_percent', 90), ('disk_usage_percent', 90)):
            value = getattr(snapshot, name)
            if value is None or value > limit:
                violations.append(ResourceViolation(source=source, metric=name, observed=value, limit=limit,
                    reason='MISSING_METRIC' if value is None else 'LIMIT_EXCEEDED'))
    if violations:
        reason = ('unsafe/unavailable snapshot; no dispatch' if any(v.reason == 'UNUSABLE_SNAPSHOT' for v in violations)
                  else 'resource safety stop; no dispatch')
        raise ResourceAdmissionError(reason, sample, at, violations)
    return sample, at


class Lane:
    def __init__(self, *, config, allocation, lane, query, reference, settings, monitor,
                 output, bank, adapter_factory=IoTDBTreeAdapter):
        self.config, self.allocation, self.lane = config, allocation, lane
        self.query, self.reference = query, reference
        self.adapter = adapter_factory(settings)
        self.store = SQLiteStore(output/f'{allocation.allocation_id}-{lane}.sqlite', wal_full=True)
        self.registry = build_default_registry(cache_epoch=config.dataset.epoch)
        self.engine = BenchmarkEngine(self.registry, allocation=allocation, bank=bank)
        self.monitor = ReferenceMonitor(reference=reference, query=query, comparison=config.analysis,
            registry=self.registry, monitor=monitor, snapshot_mode='latest', latest_max_age_ms=200,
            latest_wait_timeout_s=.5)
        self.learner = RecordingLearner(ExperienceRepository(self.store), self.registry,
            resource_policy=self.engine.resource_policy, statistics_mode='record_only')
        context = LearningContext(server_id=f'{settings.iotdb_host}:{settings.iotdb_port}',
            server_version=config.dataset.server_version, database_model='tree',
            dataset_epoch=config.dataset.epoch, result_rows=query.expected_rows,
            cache_condition='phase8:'+allocation.cache_condition, load_source='db_host',
            load_protocol='cpu-activity-v3')
        self.store.initialize()
        self.service = DecisionExecutionService(adapter=self.adapter, store=self.store,
            registry=self.registry, engine=self.engine, server_version=config.dataset.server_version,
            database_model='tree', preview_rows=1000)
        self.flow = Orchestrator(adapter=self.adapter, store=self.store, registry=self.registry,
            engine=self.engine, monitor=self.monitor, learner=self.learner, context=context, preview_rows=1000)
        self.adapter.connect()
        self.last_error = None

    def close(self):
        try:
            self.adapter.close()
        finally:
            self.store.close()

    def request(self, index, role='MEASURED'):
        self.last_error = None
        started = utc_now()
        begin = time.perf_counter_ns()
        try:
            sample, at = admit(self.monitor)
        except ResourceAdmissionError as exc:
            exc.request_role, exc.request_index = role, index
            raise
        cache_before = 'READY' if self.adapter.result_cache.entries else 'EMPTY'
        workflow = run = None
        error_type = None
        group = self.allocation.group
        try:
            if role == 'MEASURED' and group not in ('E0', 'E1', 'E2'):
                workflow = self.flow.run(self.query.resolved(), mode='adaptive',
                    timeout_ms=self.config.timeout_ms, source='phase8:'+group)
                run = workflow.run
                if run is None:
                    error_type = 'WorkflowExecutionUnknown'
            else:
                mode = 'baseline' if role == 'WARMUP' or group == 'E0' and role != 'CACHE_PREP' else 'adaptive'
                run = self.service.run(self.query.resolved(), mode=mode,
                    timeout_ms=self.config.timeout_ms, system_state=sample, source='phase8:'+role+':'+group)
        except Exception as exc:
            # Persist unknown dispatch, then the runner stops. Never fabricate an execution.
            error_type = type(exc).__name__
            self.last_error = exc
        elapsed = time.perf_counter_ns()-begin
        finished = utc_now()
        experiences = []
        if workflow is not None and run is not None:
            for attempt in run.execution.attempts:
                experience = self.learner.repository.get(attempt.execution.execution_id)
                if experience is not None:
                    experiences.append(experience)
        correctness = 'NOT_CHECKED' if run is None else ('MATCHED' if result_matches(
            run.execution.attempts[-1].execution, self.reference, self.query, self.config.analysis) else 'MISMATCHED')
        return RequestRecord(allocation=self.allocation, lane=self.lane, index=index, role=role,
            sql_sha256=self.query.resolved_sha256, started_at=started, finished_at=finished,
            elapsed_ns=elapsed, sample=sample, sample_at=at,
            observed_load=load_identity(sample, 'db_host', at, protocol='cpu-activity-v3')[1],
            cache_before=cache_before, cache_entries_after=self.adapter.result_cache.entries,
            correctness=correctness, run=run, workflow=workflow, experiences=experiences,
            error_type=error_type, inference=self.engine.last_inference)

    def prepare(self, emit):
        # Warmup has a separate occurrence history; resetting it is explicit.
        for i in range(self.config.warmup):
            row = self.request(i, 'WARMUP')
            emit(row)
            if not row.success or row.correctness != 'MATCHED':
                raise RuntimeError('warmup failed; no retry')
        self.engine = BenchmarkEngine(self.registry, allocation=self.allocation, bank=self.engine.bank)
        self.service.engine = self.engine
        self.flow.engine = self.engine
        self.adapter.result_cache.clear()
        if self.allocation.cache_condition == 'APPLICATION_WARM':
            self.engine.preparing = True
            try:
                # First request remains Native; second is the certified S1 fill.
                for i in range(2):
                    row = self.request(i, 'CACHE_PREP')
                    emit(row)
                    if not row.success or row.correctness != 'MATCHED':
                        raise RuntimeError('cache preparation failed; no retry')
            finally:
                self.engine.preparing = False
            if self.adapter.result_cache.entries != 1:
                raise RuntimeError('full result not cached; warm condition unproven')
