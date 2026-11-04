#!/usr/bin/env python
"""Dedicated read-only benchmark. Never called from the ordinary query path.

Print raw observations and derived L1 statistics; do not claim causal speedup.
The caller must guarantee the supplied dataset epoch stays immutable.
"""
from __future__ import annotations

import argparse
from collections import Counter
import json
from pathlib import Path
import random
import tempfile

from tsdb_optimizer.adapters.iotdb.tree_adapter import IoTDBTreeAdapter
from tsdb_optimizer.agents import PerformanceMonitoringAgent, QueryAnalysisAgent
from tsdb_optimizer.agents.learning_feedback import LearningFeedbackAgent
from tsdb_optimizer.config import load_settings
from tsdb_optimizer.execution import DecisionExecutionService
from tsdb_optimizer.learning.repository import ExperienceRepository
from tsdb_optimizer.schemas.learning import LearningContext, load_identity
from tsdb_optimizer.storage import SQLiteStore
from tsdb_optimizer.strategies import build_default_registry
from tsdb_optimizer.sql_guard import validate_readonly_sql

ROOT = Path(__file__).resolve().parents[1]


def benchmark(*, epoch: str, repeats: int = 4) -> dict:
    if repeats < 3:
        raise ValueError('at least three repeated observations required')
    settings = load_settings()
    adapter = IoTDBTreeAdapter(settings)
    registry = build_default_registry(cache_epoch=epoch)
    monitor = PerformanceMonitoringAgent(registry=registry)
    monitor.sample_once()  # Establish the real CPU/I/O baseline.
    raw = []
    try:
        doctor = adapter.doctor()
        if not doctor.ok or doctor.server.version != '2.0.6' or doctor.server.model != 'tree':
            raise RuntimeError('requires verified IoTDB 2.0.6 tree model')
        with tempfile.TemporaryDirectory(prefix='phase5-evidence-') as directory:
            store = SQLiteStore(Path(directory) / 'experience.sqlite')
            store.initialize()
            repo = ExperienceRepository(store)
            learner = LearningFeedbackAgent(repo, registry, min_samples=3)
            service = DecisionExecutionService(adapter=adapter, store=store, registry=registry,
                server_version=doctor.server.version, database_model=doctor.server.model, preview_rows=1000)
            for relative in ['queries_dbm/q4.sql', 'queries_dbm/q5.sql']:
                sql = next(line.strip() for line in (ROOT/'query'/relative).read_text().splitlines()
                           if line.strip().upper().startswith('SELECT'))
                sql = validate_readonly_sql(sql)
                # Explicit diagnostic warmup/reference; not an online reward.
                reference = service.run(sql, mode='baseline', timeout_ms=30000).execution.attempts[-1].execution
                if reference.status.value != 'SUCCESS' or reference.preview_truncated:
                    raise RuntimeError('benchmark requires a complete reference result')
                schedule = ['S0_NATIVE'] * repeats + ['S1_REPEAT_CACHE'] * repeats
                random.Random(20261102).shuffle(schedule)
                for strategy in schedule:
                    if strategy == 'S1_REPEAT_CACHE':
                        adapter.result_cache.clear()
                    for path in (['SERVER', 'CACHE_HIT'] if strategy == 'S1_REPEAT_CACHE' else ['SERVER']):
                        before = monitor.sample_once()
                        run = service.run(sql, mode='baseline' if strategy == 'S0_NATIVE' else 'rule_based',
                                          timeout_ms=30000, source='phase5-controlled-benchmark')
                        result = run.execution.attempts[-1].execution
                        if result.strategy_id != strategy or result.status.value != 'SUCCESS':
                            raise RuntimeError('benchmark execution did not take intended strategy')
                        if result.served_from_cache != (path == 'CACHE_HIT'):
                            raise RuntimeError('cache capture did not produce intended result source')
                        matched = (result.columns == reference.columns and result.row_count == reference.row_count
                                   and result.rows_preview == reference.rows_preview and not result.preview_truncated)
                        record = monitor.record_execution(result, decision_id=run.decision.decision_id, snapshots=before)
                        record = record.model_copy(update={'runtime_result_check': 'MATCHED' if matched else 'MISMATCHED'})
                        host, bucket = load_identity(before, 'client_host', result.started_at)
                        context = LearningContext(server_id=f'{settings.iotdb_host}:{settings.iotdb_port}',
                            server_version=doctor.server.version, database_model='tree', dataset_epoch=epoch,
                            result_rows=result.row_count,
                            cache_condition='warm-serial-explicit-cache-reset', result_source=path,
                            load_source='client_host', load_host=host, load_bucket=bucket)
                        feedback = learner.observe(run, context, record=record, before=before,
                                                   after=monitor.sample_once())
                        raw.append({'workload': relative, 'query_id': result.query_id,
                            'decision_id': run.decision.decision_id, 'execution_id': result.execution_id,
                            'strategy_id': strategy, 'strategy_version': run.decision.strategy_version,
                            'result_source': path, 'load_bucket': bucket,
                            'client_wall_ms': result.metrics.client_wall_ms,
                            'request_dispatched': result.request_dispatched, 'matched': matched,
                            'feedback': feedback.model_dump(mode='json')})
                        if not matched or feedback.warnings:
                            raise RuntimeError(f'benchmark correctness/learning failed: {feedback.warnings}')
            with store._connect() as connection:
                groups = [row[0] for row in connection.execute('SELECT DISTINCT group_key FROM experiences')]
                experiences = [json.loads(row[0]) for row in connection.execute(
                    'SELECT body_json FROM experiences ORDER BY created_at,execution_id')]
            models = []
            for group in sorted(groups):
                model = repo.build_model(group).model_dump(mode='json')
                published = repo.current_model(group)
                model['published_model_version'] = published.model_version if published else None
                models.append(model)
            return {'schema_version': 1, 'server_version': doctor.server.version, 'client_version': doctor.server.client_version,
                'dataset_epoch': epoch, 'seed': 20261102, 'repeats': repeats,
                'db_metrics': 'UNKNOWN_NO_AGENT', 'latency_unit': 'ms',
                'sample_status_counts': dict(Counter(item['feedback']['sample_status'] for item in raw)),
                'observations': raw, 'experiences': experiences, 'statistics': models,
                'limitations': ['single-machine warm serial diagnostic benchmark',
                    'client load only, not DB load', 'dataset size UNKNOWN; epoch declared by caller',
                    'CACHE_HIT and SERVER populations are isolated',
                    'no production speedup or online-learning generalization claimed']}
    finally:
        adapter.close()


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--dataset-epoch', required=True)
    parser.add_argument('--repeats', type=int, default=4)
    args = parser.parse_args()
    print(json.dumps(benchmark(epoch=args.dataset_epoch, repeats=args.repeats), indent=2, allow_nan=False))
