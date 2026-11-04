"""Serial four-agent workflow. No hidden SQL, retries, or comparison execution."""
from __future__ import annotations

import logging
import time

from ..agents import PerformanceMonitoringAgent, QueryAnalysisAgent
from ..agents.learning_feedback import LearningFeedbackAgent
from ..agents.optimization_decision import OptimizationDecisionAgent
from ..decision import DecisionMode
from ..request_memo import request_scope, store_session
from ..errors import UnsafeSQLError
from ..execution import StrategyExecutor
from ..execution.models import OptimizationRun
from ..learning.repository import ExperienceRepository
from ..schemas.common import QueryRequest, new_id, utc_now
from ..schemas.execution import ExecutionStatus
from ..schemas.learning import LearningContext, load_identity
from ..schemas.query_features import ParseStatus
from ..sql_guard import sql_sha256, validate_readonly_sql
from ..strategies import ValidationContext, build_default_registry
from .models import State, TraceEvent, WorkflowResult, WorkflowTrace

logger = logging.getLogger(__name__)


class Orchestrator:
    """One serial owner per adapter/registry; learning is a post-execution batch.

    Caller-owned adapters/stores are not closed here. Context identifiers are
    declarations, never database discovery or predictions. Missing identifiers
    remain UNKNOWN and prevent positive learning under Agent 4's quality gate.
    """

    def __init__(self, *, adapter, store, registry=None, engine=None, monitor=None,
                 learner=None, analyzer=None, context: LearningContext | None = None,
                 preview_rows=20):
        if preview_rows < 1:
            raise ValueError('preview_rows must be positive')
        self.store = store
        self.store.initialize()
        self.registry = registry if registry is not None else build_default_registry()
        self.analyzer = analyzer or QueryAnalysisAgent()
        self.engine = engine or OptimizationDecisionAgent(self.registry, analyzer=self.analyzer)
        self.monitor = monitor or PerformanceMonitoringAgent(registry=self.registry)
        self.learner = learner or LearningFeedbackAgent(
            ExperienceRepository(store), self.registry, resource_policy=self.engine.resource_policy)
        if any(agent.registry is not self.registry for agent in (self.engine, self.monitor, self.learner)):
            raise ValueError('all agents must share one registry')
        if self.learner.resource_policy != self.engine.resource_policy:
            raise ValueError('decision and learning resource policies must match')
        if self.learner.repository.store is not store:
            raise ValueError('learning and execution must share one store')
        self.executor = StrategyExecutor(adapter=adapter, registry=self.registry)
        self.preview_rows = preview_rows
        self.context = context or LearningContext(server_id='UNKNOWN', server_version='UNKNOWN',
            database_model='unknown', dataset_epoch='UNKNOWN', cache_condition='UNKNOWN',
            load_source='client_host')

    @request_scope
    @store_session
    def run(self, sql: str, *, mode=DecisionMode.RULE_BASED, source='orchestrator',
            timeout_ms=None) -> WorkflowResult:
        mode = DecisionMode(mode)
        if timeout_ms is not None and timeout_ms < 0:
            raise ValueError('timeout_ms must be non-negative')
        server_version = None if self.context.server_version == 'UNKNOWN' else self.context.server_version
        database_model = None if self.context.database_model == 'unknown' else self.context.database_model
        started = time.perf_counter()
        query_id = new_id()
        trace = WorkflowTrace(query_id=query_id, sql_sha256=sql_sha256(sql),
                              requested_mode=mode.value, effective_mode=mode.value)
        decision = None
        report = None
        run = None
        records = []
        feedback = []
        before = None

        def event(state, **details):
            item = TraceEvent(index=len(trace.events), state=state,
                elapsed_ms=round((time.perf_counter()-started)*1000, 3), query_id=query_id,
                decision_id=decision.decision_id if decision else None,
                execution_ids=[a.execution.execution_id for a in report.attempts] if report else [],
                details=details)
            # Validate transitions on every change, not just on serialization.
            WorkflowTrace.model_validate({**trace.model_dump(), 'events': [*trace.events, item]})
            trace.events.append(item)
            logger.info('workflow transition', extra={'event': 'workflow_transition',
                'query_id': query_id, 'decision_id': item.decision_id, 'state': state.value,
                'execution_ids': item.execution_ids, 'event_index': item.index,
                'elapsed_ms': item.elapsed_ms, 'fallback_used': details.get('fallback_used'),
                'fallback_trigger': details.get('fallback_trigger'),
                'final_status': details.get('final_status'), 'error_type': details.get('error_type')})

        def warning(code, exc):
            # Never copy arbitrary exception text (credentials/SQL may be embedded).
            trace.warnings.append(f'{code}: {type(exc).__name__}')
            logger.error('workflow auxiliary failure', extra={'event': code,
                'query_id': query_id, 'error_type': type(exc).__name__})

        event(State.RECEIVED)
        try:
            parsing_started = time.perf_counter_ns()
            cleaned = validate_readonly_sql(sql)
            trace.sql_sha256 = sql_sha256(cleaned)
            request = QueryRequest(query_id=query_id, sql=cleaned, source=source, timeout_ms=timeout_ms)
            analysis = self.analyzer.analyze(cleaned, query_id=query_id)
            parsing_finished = time.perf_counter_ns()
            self.store.record_query(request, trace.sql_sha256)
            query_stored = time.perf_counter_ns()
            event(State.PARSED, parse_status=analysis.parse.parse_status.value,
                  parsing_ms=(parsing_finished-parsing_started)/1e6,
                  query_persistence_ms=(query_stored-parsing_finished)/1e6)
            try:
                before = self.monitor.sample_once()
            except Exception as exc:
                warning('SNAPSHOT_FAILED', exc)
            event(State.SNAPSHOT, client_snapshot_id=before.client_host.snapshot_id if before else None,
                db_snapshot_id=before.db_host.snapshot_id if before else None,
                client_source=before.client_host.source_type.value if before else 'UNKNOWN',
                db_source=before.db_host.source_type.value if before else 'UNKNOWN')
            host, bucket = load_identity(before, self.context.load_source, utc_now(), protocol=self.context.load_protocol)
            retrieval_context = self.context.model_copy(update={'load_host': host, 'load_bucket': bucket})
            advice = None
            try:
                # Exact context only. Unknown result_rows naturally finds no qualified model.
                advice = self.learner.recommend(analysis, retrieval_context, diagnostics=trace.warnings)
            except Exception as exc:
                warning('RETRIEVAL_FAILED', exc)
            event(State.RETRIEVED, model_version=advice.model_version if advice else None,
                  advice_strategy=advice.strategy_id if advice else None,
                  context_key=retrieval_context.key,
                  expected_result_rows=self.context.result_rows)
            # Recheck AFTER retrieval; an initially fresh sample can expire during I/O.
            reasons = []
            if analysis.parse.parse_status != ParseStatus.SUCCESS:
                reasons.append('PARSER_UNSUPPORTED_NATIVE')
            if before is None or not before.client_host.is_usable(now=utc_now()):
                reasons.append('CLIENT_STATE_UNAVAILABLE_OR_STALE_NATIVE')
            if before is not None:
                for name in ('db_host', 'db_metrics'):
                    snapshot = getattr(before, name)
                    if snapshot is not None and any(getattr(snapshot, field) is not None for field in
                            ('cpu_percent', 'memory_percent', 'disk_usage_percent')) and (
                            snapshot.source_type.value != name or not snapshot.is_usable(now=utc_now())):
                        reasons.append(name.upper() + '_STATE_UNAVAILABLE_OR_STALE_NATIVE')
            effective_mode = DecisionMode.BASELINE if reasons else mode
            trace.effective_mode = effective_mode.value
            trace.warnings.extend(reasons)
            decision = self.engine.decide(query_id=query_id, original_sql=cleaned,
                mode=effective_mode, analysis=analysis, system_state=before,
                server_version=server_version, database_model=database_model,
                adaptive_advice=advice if effective_mode == DecisionMode.ADAPTIVE else None)
            self.store.record_decision(decision)
            event(State.DECIDED, strategy_id=decision.selected_strategy_id,
                  strategy_version=decision.strategy_version, model_version=decision.model_version,
                  effective_mode=effective_mode.value, degradation_reasons=reasons,
                  decision_warnings=decision.warnings)
            selected = next(c for c in decision.considered if c.selected)
            entry = self.registry.assert_selectable(decision.selected_strategy_id)
            validation = self.registry.validate_candidate(entry, cleaned, selected.candidate,
                ValidationContext(query_id=query_id, analysis=analysis,
                    server_version=server_version, database_model=database_model))
            event(State.VALIDATED, passed=validation.passed,
                  executor_revalidates=True)
            # Only Executor dispatches. Unknown exception means dispatch count UNKNOWN.
            trace.database_dispatches = None
            report = self.executor.execute(decision, preview_rows=self.preview_rows,
                timeout_ms=timeout_ms, analysis=analysis, system_state=before, require_fresh_state=True,
                server_version=server_version, database_model=database_model)
            trace.database_dispatches = (
                sum(a.execution.request_dispatched is True for a in report.attempts)
                if all(a.execution.request_dispatched is not None for a in report.attempts) else None)
            for attempt in report.attempts:
                try:
                    self.store.record_execution(attempt.execution, decision_id=decision.decision_id,
                        attempt_index=attempt.attempt_index, is_fallback=attempt.is_fallback,
                        requested_strategy_id=attempt.requested_strategy_id)
                except Exception as exc:
                    warning('EXECUTION_PERSISTENCE_FAILED', exc)
            report = report.model_copy(update={'end_to_end_ms': round((time.perf_counter()-started)*1000, 3)})
            run = OptimizationRun(decision=decision, execution=report)
            try:
                self.store.record_execution_report(report)
            except Exception as exc:
                warning('REPORT_PERSISTENCE_FAILED', exc)
            event(State.EXECUTED, final_status=report.final_status.value,
                database_dispatches=trace.database_dispatches, fallback_used=report.fallback_used,
                fallback_trigger=report.fallback_trigger.value,
                fallback_reason=report.fallback_reason,
                fallback_blocked_reason=report.fallback_blocked_reason,
                attempts=[{'execution_id': a.execution.execution_id,
                    'strategy_id': a.execution.strategy_id, 'is_fallback': a.is_fallback,
                    'timed_out': a.execution.timed_out, 'request_dispatched': a.execution.request_dispatched,
                    'request_completed': a.execution.request_completed,
                    'served_from_cache': a.execution.served_from_cache} for a in report.attempts])
            after = None
            try:
                after = self.monitor.sample_once()
            except Exception as exc:
                warning('AFTER_SNAPSHOT_FAILED', exc)
            for attempt in report.attempts:
                try:
                    records.append(self.monitor.record_execution(attempt.execution,
                        decision_id=decision.decision_id, snapshots=before if before else [],
                        timeout_ms=timeout_ms))
                except Exception as exc:
                    warning('MONITOR_FAILED', exc)
            event(State.MONITORED, record_ids=[r.execution_id for r in records],
                  runtime_result_checks=[r.runtime_result_check for r in records])
            # Bounded per-query batch: each actual attempt only, no counterfactual rows.
            for record in records:
                result = next(a.execution for a in report.attempts if a.execution.execution_id == record.execution_id)
                host, bucket = load_identity(before, self.context.load_source, result.started_at, protocol=self.context.load_protocol)
                context = self.context.model_copy(update={'load_host': host, 'load_bucket': bucket,
                    'result_rows': result.row_count if self.context.result_rows is None else self.context.result_rows,
                    'fetch_mode': result.fetch_mode.value,
                    'result_source': 'CACHE_HIT' if result.served_from_cache else 'SERVER'})
                try:
                    item = self.learner.observe(run, context, record=record, before=before,
                                               after=after, analysis=analysis)
                    feedback.append(item)
                    trace.warnings.extend(w.split(':', 1)[0] for w in item.warnings)
                except Exception as exc:
                    warning('LEARNING_FAILED', exc)
            event(State.LEARNED, feedback=[{'execution_id': f.execution_id,
                'sample_status': f.sample_status.value if f.sample_status else None,
                'model_updated': f.model_updated, 'model_version': f.model_version,
                'quality_reasons': f.quality_reasons, 'warning_codes': [w.split(':', 1)[0] for w in f.warnings]}
                for f in feedback])
            final = report.attempts[-1].execution
            state = State.SUCCEEDED if final.status == ExecutionStatus.SUCCESS else (
                State.TIMED_OUT if final.timed_out else State.FAILED)
            event(state)
        except Exception as exc:
            warning('WORKFLOW_FAILED', exc)
            event(State.REJECTED if isinstance(exc, UnsafeSQLError) and report is None else State.FAILED,
                  error_type=type(exc).__name__)
        try:
            self.store.record_workflow_trace(trace)
        except Exception as exc:
            warning('TRACE_PERSISTENCE_FAILED', exc)
        return WorkflowResult(trace=trace, run=run, records=records, feedback=feedback)
