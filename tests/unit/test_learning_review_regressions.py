"""Permanent Phase-5 review regressions; all execution/DB-load fixtures are synthetic."""
import sqlite3
from test_learning import lab
from phase4_support import TEST_ONLY_STRATEGY_ID
from tsdb_optimizer.schemas.learning import SampleStatus
from tsdb_optimizer.decision.resource_policy import assess_resource_pressure, load_default_resource_policy


def test_mismatch_is_quarantined_despite_audit_read_failure(lab, monkeypatch):
    store, repo, agent, context, execute, _ = lab
    run, record, before = execute(TEST_ONLY_STRATEGY_ID)
    def fail_read(*args, **kwargs):
        raise sqlite3.OperationalError('review injected read outage')
    monkeypatch.setattr(store, '_connect', fail_read)
    feedback = agent.observe(run, context,
        record=record.model_copy(update={'runtime_result_check':'MISMATCHED'}), before=before)
    assert feedback.warnings  # failure is visible, but safety must still hold
    monkeypatch.undo()
    assert TEST_ONLY_STRATEGY_ID not in agent.registry.selectable_ids(), (
        'Known MISMATCHED version became selectable after storage recovered')


def test_known_db_resource_breach_cannot_receive_normal_reward(lab):
    _, _, agent, context, execute, _ = lab
    for _ in range(3):
        run, record, before = execute()
        before = before.model_copy(update={'db_host': before.db_host.model_copy(update={'cpu_percent':99.0})})
        pressure = assess_resource_pressure(before, load_default_resource_policy(), now=record.started_at)
        assert pressure.breached  # production policy; explicitly synthetic DB metric
        feedback = agent.observe(run, context, record=record, before=before)
        assert not feedback.warnings
    assert feedback.sample_status != SampleStatus.VALID and feedback.reward is None, (
        f'Over-threshold DB sample admitted: {feedback.sample_status}, reward={feedback.reward}, '
        f'model_updated={feedback.model_updated}')


def test_experience_model_version_must_match_persisted_decision(lab):
    store, repo, agent, context, execute, _ = lab
    run, record, before = execute(TEST_ONLY_STRATEGY_ID)
    persisted_version = store.get_decision(run.decision.decision_id)['model_version']
    altered = run.model_copy(update={'decision': run.decision.model_copy(update={'model_version':'never-used-model'})})
    feedback = agent.observe(altered, context, record=record, before=before)
    saved = repo.get(record.execution_id)
    assert saved is None or saved.decision_model_version == persisted_version, (
        f'experience={saved.decision_model_version}, persisted decision={persisted_version}; '
        f'quality={feedback.sample_status}')


import json
from datetime import timedelta
import pytest
from test_learning import ingest
from tsdb_optimizer.agents import QueryAnalysisAgent
from tsdb_optimizer.agents.learning_feedback import LearningFeedbackAgent, feature_identity
from tsdb_optimizer.schemas.learning import digest
from tsdb_optimizer.schemas.monitoring import SystemSourceType


@pytest.mark.parametrize('failure', ['read', 'write'])
def test_mismatch_pending_audit_can_be_retried_without_unblocking(lab, monkeypatch, failure):
    store, repo, agent, context, execute, adapter = lab
    run, record, before = execute(TEST_ONLY_STRATEGY_ID)
    original = run.model_dump_json()
    mismatch = record.model_copy(update={'runtime_result_check':'MISMATCHED'})
    def fail(*args, **kwargs): raise sqlite3.OperationalError('injected audit outage')
    monkeypatch.setattr(store if failure == 'read' else repo,
                        '_connect' if failure == 'read' else 'record', fail)
    feedback = agent.observe(run, context, record=mismatch, before=before)
    assert feedback.quarantine_persistence_pending
    assert any('QUARANTINE_PERSISTENCE_PENDING' in w for w in feedback.warnings)
    assert run.model_dump_json() == original and len(adapter.calls) == 1
    monkeypatch.undo()
    assert TEST_ONLY_STRATEGY_ID not in agent.registry.selectable_ids()
    feedback = agent.observe(run, context, record=mismatch, before=before)
    assert not feedback.warnings and not feedback.quarantine_persistence_pending
    assert repo.is_quarantined(TEST_ONLY_STRATEGY_ID, '0.0.1')
    assert repo.get(record.execution_id).sample_status == SampleStatus.INVALID
    assert len(adapter.calls) == 1


def test_invalid_mismatch_identity_cannot_quarantine_another_strategy(lab):
    _, _, agent, context, execute, _ = lab
    run, record, before = execute()
    bad = record.model_copy(update={'runtime_result_check':'MISMATCHED',
                                    'strategy_id':TEST_ONLY_STRATEGY_ID})
    feedback = agent.observe(run, context, record=bad, before=before)
    assert feedback.warnings and not feedback.quarantine_persistence_pending
    assert TEST_ONLY_STRATEGY_ID in agent.registry.selectable_ids()
    assert 'S0_NATIVE' in agent.registry.selectable_ids()


@pytest.mark.parametrize('source,group_source', [
    ('client_host','db_host'), ('db_host','client_host'), ('db_metrics','client_host'),
])
@pytest.mark.parametrize('metric', ['cpu_percent','memory_percent','disk_usage_percent'])
def test_every_known_source_and_metric_vetoes_learning(lab, source, group_source, metric):
    _, repo, agent, context, execute, _ = lab
    run, record, before = execute()
    snapshot = getattr(before, source) if source != 'db_metrics' else before.db_host.model_copy(
        update={'source_type':SystemSourceType.DB_METRICS})
    before = before.model_copy(update={source:snapshot.model_copy(update={metric:99.0})})
    context = context.model_copy(update={'load_source':group_source,'load_host':group_source})
    feedback = agent.observe(run, context, record=record, before=before)
    assert feedback.sample_status == SampleStatus.LOW_CONFIDENCE
    assert feedback.reward is None and not feedback.model_updated
    assert any(f'{source}.{metric}' in reason for reason in feedback.quality_reasons)
    saved = repo.get(record.execution_id)
    assert saved.quality_version == 2
    assert saved.resource_policy_version == agent.resource_policy.policy_version
    assert saved.resource_policy_hash == agent.resource_policy_hash


@pytest.mark.parametrize('state', ['invalid','stale','future','unknown','expiry_boundary'])
def test_unusable_other_source_does_not_invent_a_breach(lab, state):
    _, _, agent, context, execute, _ = lab
    run, record, before = execute()
    changes = {'cpu_percent':99.0}
    if state == 'invalid': changes['valid'] = False
    elif state == 'unknown':
        changes.update(source_type=SystemSourceType.UNKNOWN, valid=False)
    elif state == 'future': changes['captured_at'] = record.started_at + timedelta(milliseconds=1)
    elif state == 'stale': changes['captured_at'] = record.started_at - timedelta(seconds=60)
    else: changes['captured_at'] = record.started_at - timedelta(milliseconds=before.db_host.stale_after_ms)
    before = before.model_copy(update={'db_host':before.db_host.model_copy(update=changes)})
    feedback = agent.observe(run, context, record=record, before=before)
    assert not feedback.warnings and feedback.sample_status == SampleStatus.VALID
    assert not any('RESOURCE_CONSTRAINT' in r for r in feedback.quality_reasons)


@pytest.mark.parametrize('cpu,expected', [(80.0,SampleStatus.VALID),(80.001,SampleStatus.LOW_CONFIDENCE)])
def test_custom_policy_uses_strict_boundary_at_execution_time(lab, cpu, expected):
    _, repo, old, context, execute, _ = lab
    policy = old.resource_policy.model_copy(update={
        'policy_version':'test-custom-80',
        'db_host':old.resource_policy.db_host.model_copy(update={'cpu_percent':80.0})})
    agent = LearningFeedbackAgent(repo, old.registry, resource_policy=policy)
    run, record, before = execute()
    before = before.model_copy(update={'db_host':before.db_host.model_copy(update={
        'cpu_percent':cpu, 'captured_at':record.started_at})})
    feedback = agent.observe(run, context, record=record, before=before)
    assert feedback.sample_status == expected and not feedback.warnings
    assert repo.get(record.execution_id).resource_policy_version == 'test-custom-80'


@pytest.mark.parametrize('strategy', ['S0_NATIVE',TEST_ONLY_STRATEGY_ID])
def test_model_association_checks_none_and_external_versions(lab, strategy):
    store, repo, agent, context, execute, _ = lab
    run, record, before = execute(strategy)
    persisted = store.get_decision(run.decision.decision_id)['model_version']
    changed = 'not-the-persisted-model' if persisted is None else None
    altered = run.model_copy(update={'decision':run.decision.model_copy(update={'model_version':changed})})
    feedback = agent.observe(altered, context, record=record, before=before)
    assert feedback.warnings and repo.get(record.execution_id) is None
    feedback = agent.observe(run, context, record=record, before=before)
    assert not feedback.warnings and repo.get(record.execution_id).decision_model_version == persisted


def test_old_quality_records_and_heads_are_not_silently_recertified(lab):
    store, repo, agent, context, _, _ = lab
    for _ in range(3): ingest(lab, latency=20)
    for _ in range(3): ingest(lab, TEST_ONLY_STRATEGY_ID, latency=1)
    # Use the exact fixture SQL, not an invented template.
    from phase4_support import TEST_SQL
    analysis = QueryAnalysisAgent().analyze(TEST_SQL)
    assert agent.recommend(analysis, context) is not None
    template, features = feature_identity(analysis)
    group = digest([template,features,context.key])
    model = repo.current_model(group)
    with store._connect() as c:
        body = json.loads(model.model_dump_json())
        body.pop('quality_version'); body.pop('resource_policy_hash')
        c.execute('UPDATE learning_models SET body_json=? WHERE model_version=?',
                  (json.dumps(body),model.model_version))
        for row in c.execute('SELECT execution_id,body_json FROM experiences').fetchall():
            body = json.loads(row['body_json'])
            for key in ('quality_version','resource_policy_version','resource_policy_hash'): body.pop(key)
            c.execute('UPDATE experiences SET body_json=? WHERE execution_id=?',
                      (json.dumps(body),row['execution_id']))
    assert agent.recommend(analysis, context) is None
    rebuilt = repo.build_model(group)
    assert all(s.valid_count == 0 and s.mean_reward is None for s in rebuilt.statistics)
    assert sum(s.observed_count for s in rebuilt.statistics) == 6  # audit evidence retained


def test_policy_fingerprint_prevents_reusing_a_model_under_changed_limits(lab):
    _, repo, agent, context, _, _ = lab
    for _ in range(3): ingest(lab, latency=20)
    for _ in range(3): ingest(lab, TEST_ONLY_STRATEGY_ID, latency=1)
    from phase4_support import TEST_SQL
    analysis = QueryAnalysisAgent().analyze(TEST_SQL)
    assert agent.recommend(analysis, context) is not None
    changed = agent.resource_policy.model_copy(update={'client_host':
        agent.resource_policy.client_host.model_copy(update={'cpu_percent':5.0})})
    # Even a mistakenly reused human version label cannot mix changed policies.
    strict = LearningFeedbackAgent(repo, agent.registry, resource_policy=changed)
    assert strict.resource_policy_hash != agent.resource_policy_hash
    assert strict.recommend(analysis, context) is None


@pytest.mark.parametrize('change', [{'query_id':'unrelated-query'}, {'strategy_version':'unrelated-version'}])
def test_inconsistent_run_cannot_quarantine_a_version(lab, change):
    _, _, agent, context, execute, _ = lab
    run, record, before = execute(TEST_ONLY_STRATEGY_ID)
    altered = run.model_copy(update={'decision':run.decision.model_copy(update=change)})
    mismatch = record.model_copy(update={'runtime_result_check':'MISMATCHED'})
    feedback = agent.observe(altered, context, record=mismatch, before=before)
    assert feedback.warnings and not feedback.quarantine_persistence_pending
    assert TEST_ONLY_STRATEGY_ID in agent.registry.selectable_ids()
