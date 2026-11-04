"""Review-only counterexamples; TEST ONLY synthetic execution fixtures."""
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
