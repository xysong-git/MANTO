"""Agent 4: explicit ingestion of real executions and safe L1 recommendations."""
from __future__ import annotations

import logging
import math
from typing import Literal

from ...decision.resource_policy import ResourcePolicy, assess_resource_pressure, load_default_resource_policy
from ...decision.models import AdaptiveAdvice
from ...execution.models import OptimizationRun
from ...errors import StrategyRegistryError
from ...learning.repository import ExperienceRepository
from ...schemas.learning import (
    Experience, LearningContext, LearningFeedback, SampleStatus, digest, load_identity,
)
from ...schemas.monitoring import ExecutionRecord, SystemSample
from ...schemas.query_features import ParseStatus, QueryAnalysisResult
from ...strategies import StrategyRegistry, StrategyStatus
from ..query_analysis import QueryAnalysisAgent

logger = logging.getLogger(__name__)


def feature_identity(analysis: QueryAnalysisResult) -> tuple[str, str]:
    # Exact SQL is the fallback retrieval identity for unsupported parser syntax.
    # No normalized template is invented for unsupported syntax.
    template = analysis.parse.template_hash or analysis.parse.query_hash
    features = analysis.features.model_dump(mode='json', exclude={
        'query_id', 'query_hash', 'template_hash', 'normalized_template', 'warnings'})
    return template, digest(features)


class LearningFeedbackAgent:
    def __init__(self, repository: ExperienceRepository, registry: StrategyRegistry,
                 *, min_samples: int = 3, min_improvement: float = 0.05,
                 resource_policy: ResourcePolicy | None = None,
                 statistics_mode: Literal['synchronous', 'record_only'] = 'synchronous'):
        if min_samples < 2 or not 0 <= min_improvement < 1:
            raise ValueError('min_samples >= 2 and 0 <= min_improvement < 1 required')
        if statistics_mode not in ('synchronous', 'record_only'):
            raise ValueError('unknown statistics mode')
        self.statistics_mode = statistics_mode
        self.repository = repository
        self.registry = registry
        self.registry.set_quarantine_checker(self.repository.is_quarantined)
        self.resource_policy = resource_policy or load_default_resource_policy()
        self.resource_policy_hash = digest(self.resource_policy.model_dump(mode="json"))
        self.min_samples = min_samples
        self.min_improvement = min_improvement

    def observe(self, run: OptimizationRun, context: LearningContext, *,
                record: ExecutionRecord, before: SystemSample | None = None,
                during: list[SystemSample] | None = None, after: SystemSample | None = None,
                analysis: QueryAnalysisResult | None = None) -> LearningFeedback:
        """Never dispatch SQL. A learning failure cannot replace a query result.

        Caller retains `run`; this method returns feedback only. The explicit
        record identifies the actual attempt, including a possible fallback.
        """
        feedback = LearningFeedback(query_id=run.decision.query_id, execution_id=record.execution_id)
        try:
            attempt, version = self._validated_attempt(run, record)
            if record.runtime_result_check == 'MISMATCHED':
                self.registry.quarantine_version(attempt.execution.strategy_id, version)
                feedback.quarantine_persistence_pending = True
            experience = self._experience(run, context, record, before, during or [], after, analysis)
            added = self.repository.record(experience)
            experience = self.repository.get(experience.execution_id)
            if self.repository.is_quarantined(experience.strategy_id, experience.strategy_version):
                feedback.quarantine_persistence_pending = False
                experience = experience.model_copy(update={'sample_status': SampleStatus.INVALID, 'reward': None})
            feedback.sample_status = experience.sample_status
            feedback.experience_id = experience.experience_id
            feedback.reward = experience.reward
            feedback.quality_reasons = experience.quality_reasons
            feedback.duplicate = not added
            if self.statistics_mode == 'synchronous' and added and experience.sample_status == SampleStatus.VALID:
                try:
                    model = self.repository.build_model(experience.group_key,
                        min_samples=self.min_samples, min_improvement=self.min_improvement,
                        resource_policy_hash=self.resource_policy_hash)
                    if any(s.eligible for s in model.statistics):
                        self.repository.publish(model)
                        feedback.model_updated = True
                        feedback.model_version = model.model_version
                except Exception as exc:
                    logger.exception('L1 model update failed', extra={'event': 'learning_model_error',
                                     'execution_id': record.execution_id})
                    feedback.warnings.append(f'MODEL_UPDATE_FAILED: {type(exc).__name__}: {exc}')
        except Exception as exc:
            logger.exception('experience ingestion failed', extra={'event': 'learning_write_error',
                             'query_id': run.decision.query_id, 'execution_id': record.execution_id})
            feedback.warnings.append(f'LEARNING_FAILED: {type(exc).__name__}: {exc}')
            if feedback.quarantine_persistence_pending:
                feedback.warnings.append('QUARANTINE_PERSISTENCE_PENDING: runtime version blocked; retry audit persistence')
        return feedback

    @staticmethod
    def _validated_attempt(run, record):
        """Bind safety evidence to the actual attempt without reading any store."""
        decision = run.decision
        attempt = next((a for a in run.execution.attempts if a.execution.execution_id == record.execution_id), None)
        if attempt is None:
            raise ValueError('execution record is not an attempt of this run')
        result = attempt.execution
        if (result.query_id != decision.query_id
                or run.execution.query_id != decision.query_id
                or run.execution.decision_id != decision.decision_id):
            raise ValueError('run identity does not match the actual decision and attempt')
        for field, expected in {
            'query_id': result.query_id, 'decision_id': decision.decision_id,
            'strategy_id': result.strategy_id, 'success': result.status.value == 'SUCCESS',
            'timed_out': result.timed_out, 'client_wall_ms': result.metrics.client_wall_ms,
            'execute_call_ms': result.metrics.execute_call_ms,
            'result_fetch_ms': result.metrics.result_fetch_ms,
            'started_at': result.started_at, 'finished_at': result.finished_at,
            'rows_returned': result.row_count, 'error_type': result.error_type,
            'error_message': result.error_message,
        }.items():
            if getattr(record, field) != expected:
                raise ValueError(f'execution record differs from actual execution: {field}')
        entries = {e.strategy_id: e for e in decision.considered}
        if result.strategy_id not in entries:
            raise ValueError('actual attempt strategy has no decision version evidence')
        version = entries[result.strategy_id].strategy_version
        if not attempt.is_fallback and (
                result.strategy_id != decision.selected_strategy_id or version != decision.strategy_version):
            raise ValueError('actual attempt strategy/version differs from selected decision')
        if attempt.is_fallback and result.strategy_id != decision.fallback_strategy_id:
            raise ValueError('fallback attempt does not match decision fallback identity')
        return attempt, version

    def _experience(self, run, context, record, before, during, after, analysis):
        attempt, version = self._validated_attempt(run, record)
        decision = run.decision
        result = attempt.execution
        measured_analysis = QueryAnalysisAgent().analyze(decision.original_sql, query_id=decision.query_id)
        if analysis is not None and (
                analysis.parse.sql != measured_analysis.parse.sql
                or analysis.features != measured_analysis.features):
            raise ValueError('caller analysis differs from deterministic query features')
        analysis = measured_analysis
        if analysis.query_id != decision.query_id or analysis.parse.sql != decision.original_sql:
            raise ValueError('analysis does not describe this execution')
        template, features = feature_identity(analysis)
        suite = None
        certified = False
        try:
            entry = self.registry.assert_selectable(result.strategy_id)
            certified = (entry.spec.version == version
                         and entry.spec.verification_status == StrategyStatus.VERIFIED
                         and context.server_version in entry.spec.compatible_server_versions
                         and context.database_model == entry.spec.database_model)
            if certified:
                suite = entry.spec.semantic_test_suite
        except StrategyRegistryError as exc:
            # Registry rejection is explicit negative evidence, not success.
            logger.warning('strategy certification rejected: %s', exc,
                           extra={'event': 'learning_certification_rejected',
                                  'execution_id': record.execution_id})
            certified = False
        invalid = []
        low = []
        if not result.status.value == 'SUCCESS' or result.timed_out:
            invalid.append('EXECUTION_FAILED')
        if record.runtime_result_check == 'MISMATCHED':
            invalid.append('RESULT_MISMATCH')
        if not certified or record.strategy_certification_status != 'PASSED':
            invalid.append('STRATEGY_NOT_CERTIFIED_FOR_CONTEXT')
        if attempt.is_fallback or run.execution.fallback_used:
            low.append('FALLBACK_CONDITIONED_SAMPLE')
        if result.fetch_mode.value != 'FULL':
            invalid.append('INCOMPLETE_RESULT')
        if not math.isfinite(result.metrics.client_wall_ms) or result.metrics.client_wall_ms < 0:
            invalid.append('INVALID_LATENCY')
        if analysis.parse_status != ParseStatus.SUCCESS:
            low.append('UNSUPPORTED_TEMPLATE')
        for name in ('server_id', 'server_version', 'dataset_epoch', 'cache_condition'):
            if getattr(context, name).upper() == 'UNKNOWN':
                low.append(f'UNKNOWN_{name.upper()}')
        expected_source = 'CACHE_HIT' if result.served_from_cache else 'SERVER'
        if context.result_source != expected_source or context.fetch_mode != result.fetch_mode.value:
            invalid.append('RESULT_CONTEXT_MISMATCH')
        if context.result_rows is None:
            low.append('UNKNOWN_RESULT_SIZE')
        elif context.result_rows != result.row_count:
            invalid.append('RESULT_SIZE_MISMATCH')
        if result.cache_epoch is not None and context.dataset_epoch != result.cache_epoch:
            invalid.append('DATASET_EPOCH_MISMATCH')
        host, bucket = load_identity(before, context.load_source, result.started_at, protocol=context.load_protocol)
        if bucket == 'UNKNOWN' or (host, bucket) != (context.load_host, context.load_bucket):
            low.append('LOAD_CONTEXT_UNAVAILABLE_OR_MISMATCHED')
        pressure = assess_resource_pressure(before, self.resource_policy, now=result.started_at)
        if pressure.breached:
            low.append(f'RESOURCE_CONSTRAINT_EXCEEDED: policy_version={pressure.policy_version}; {pressure.details}')
        status = SampleStatus.INVALID if invalid else SampleStatus.LOW_CONFIDENCE if low else SampleStatus.VALID
        reward = -math.log1p(result.metrics.client_wall_ms / 1000.0) if status == SampleStatus.VALID else None
        # Derive certification rather than granting it based on success.
        record = record.model_copy(update={'strategy_certification_status': 'PASSED' if certified else 'NOT_CERTIFIED'})
        return Experience(
            query_id=decision.query_id, decision_id=decision.decision_id,
            execution_id=result.execution_id, query_hash=analysis.parse.query_hash,
            normalized_template=analysis.parse.normalized_template, template_key=template,
            features=analysis.features, feature_key=features, context=context,
            strategy_id=result.strategy_id, strategy_version=version,
            certification_suite=suite, record=record, before=before, during=during, after=after,
            is_fallback=attempt.is_fallback, cache_source_execution_id=result.cache_source_execution_id,
            decision_model_version=decision.model_version, sample_status=status,
            quality_reasons=[*invalid, *low], reward=reward, quality_version=2,
            resource_policy_version=self.resource_policy.policy_version,
            resource_policy_hash=self.resource_policy_hash)

    def recommend(self, analysis: QueryAnalysisResult, context: LearningContext, *,
                  diagnostics: list[str] | None = None) -> AdaptiveAdvice | None:
        """Fail safely to no advice; callers still run Agent 2 and Validator."""
        try:
            return self._recommend(analysis, context)
        except Exception as exc:
            logger.exception('L1 advice unavailable', extra={'event': 'learning_retrieval_error'})
            if diagnostics is not None:
                diagnostics.append(f'RETRIEVAL_FAILED: {type(exc).__name__}')
            return None

    def _recommend(self, analysis: QueryAnalysisResult, context: LearningContext) -> AdaptiveAdvice | None:
        template, features = feature_identity(analysis)
        model = self.repository.current_model(digest([template, features, context.key]))
        if (model is None or model.min_samples < self.min_samples
                or model.quality_version != 2 or model.resource_policy_hash != self.resource_policy_hash):
            return None
        usable = {}
        for stat in model.statistics:
            if not stat.eligible or self.repository.is_quarantined(stat.strategy_id, stat.strategy_version):
                continue
            try:
                entry = self.registry.assert_selectable(stat.strategy_id)
            except StrategyRegistryError as exc:
                logger.warning('L1 strategy rejected: %s', exc,
                               extra={'event': 'learning_strategy_rejected',
                                      'strategy_id': stat.strategy_id})
                continue
            if (entry.spec.version != stat.strategy_version
                    or context.server_version not in entry.spec.compatible_server_versions
                    or context.database_model != entry.spec.database_model):
                continue
            usable[stat.strategy_id] = stat
        baseline = usable.get(self.registry.baseline_strategy_id)
        if baseline is None:
            return None
        selected = min(usable.values(), key=lambda s: (s.median_latency_ms, s.strategy_id))
        if selected.strategy_id == baseline.strategy_id or not (
                selected.median_latency_ms < baseline.median_latency_ms * (1-max(model.min_improvement, self.min_improvement))):
            return None
        return AdaptiveAdvice(strategy_id=selected.strategy_id, model_version=model.model_version,
            sample_count=selected.valid_count,
            reason=f'L1 exact-context median {selected.median_latency_ms:.3f} ms; '
                   f'baseline {baseline.median_latency_ms:.3f} ms; min_samples={model.min_samples}')
