"""Independent rereview checks. Synthetic execution fixtures are TEST ONLY."""
from concurrent.futures import ThreadPoolExecutor
import json
import math
from pathlib import Path
import statistics

import pytest
from test_learning import lab, ingest
from phase4_support import TEST_ONLY_STRATEGY_ID
from tsdb_optimizer.schemas.learning import Experience, SampleStatus


def test_concurrent_identical_feedback_counts_one_real_execution(lab):
    store, repo, agent, context, execute, adapter = lab
    run, record, before = execute()
    with ThreadPoolExecutor(max_workers=6) as pool:
        results = list(pool.map(lambda _: agent.observe(run, context, record=record, before=before), range(6)))
    assert all(not result.warnings for result in results)
    assert sum(not result.duplicate for result in results) == 1
    assert len({result.experience_id for result in results}) == 1
    saved = repo.get(record.execution_id)
    model = repo.build_model(saved.group_key)
    assert len(model.statistics) == 1
    assert model.statistics[0].valid_count == 1 and not model.statistics[0].eligible
    assert len(adapter.calls) == 1


def test_a_model_built_before_mismatch_cannot_be_republished(lab):
    _, repo, agent, context, _, adapter = lab
    for _ in range(3): ingest(lab, latency=20)
    for _ in range(3): run, record, before, _ = ingest(lab, TEST_ONLY_STRATEGY_ID, latency=1)
    saved = repo.get(record.execution_id)
    old = repo.current_model(saved.group_key)
    feedback = agent.observe(run, context,
        record=record.model_copy(update={'runtime_result_check':'MISMATCHED'}), before=before)
    assert feedback.sample_status == SampleStatus.INVALID and not feedback.warnings
    with pytest.raises(ValueError, match='statistics|revision'):
        repo.publish(old)
    assert repo.current_model(saved.group_key) is None
    assert TEST_ONLY_STRATEGY_ID not in agent.registry.selectable_ids()
    assert len(adapter.calls) == 6


def test_real_benchmark_statistics_recompute_from_recorded_executions():
    path = Path(__file__).with_name('phase5_learning_fixes_2026-11-02.json')
    raw = json.loads(path.read_text())
    rows = [Experience.model_validate(e) for e in raw['experiences']]
    assert len(rows) == len({e.execution_id for e in rows}) == 24
    assert len(raw['observations']) == 24
    assert sum(o['request_dispatched'] for o in raw['observations']) == 16
    assert sum(e.sample_status == SampleStatus.VALID for e in rows) == 12
    for e in rows:
        expected = -math.log1p(e.record.client_wall_ms / 1000.0) if e.sample_status == SampleStatus.VALID else None
        assert e.reward == expected and e.quality_version == 2
        assert e.context.result_rows == e.record.rows_returned
        assert e.record.runtime_result_check == 'MATCHED'  # dedicated differential benchmark only
        assert e.before.db_host.source_type.value == 'unknown' and not e.before.db_host.valid
    for model in raw['statistics']:
        group = [e for e in rows if e.group_key == model['group_key']]
        assert sorted(e.execution_id for e in group) == sorted(model['sample_execution_ids'])
        assert len({e.context.result_source for e in group}) == 1
        for stat in model['statistics']:
            observations = [e for e in group if (e.strategy_id,e.strategy_version) ==
                            (stat['strategy_id'],stat['strategy_version'])]
            valid = [e for e in observations if e.sample_status == SampleStatus.VALID
                     and e.resource_policy_hash == model['resource_policy_hash']]
            assert stat['observed_count'] == len(observations)
            assert stat['valid_count'] == len(valid)
            assert stat['success_rate'] == sum(e.record.success for e in observations) / len(observations)
            latencies = [e.record.client_wall_ms for e in valid]
            for field, value in {
                'mean_latency_ms': statistics.mean(latencies) if valid else None,
                'median_latency_ms': statistics.median(latencies) if valid else None,
                'variance_ms2': statistics.pvariance(latencies) if valid else None,
                'mean_reward': statistics.mean(e.reward for e in valid) if valid else None,
            }.items():
                assert stat[field] is None if value is None else stat[field] == pytest.approx(value)
