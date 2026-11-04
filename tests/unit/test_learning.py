"""Synthetic, TEST ONLY execution evidence for Phase-5 statistics and failure paths."""
import math
import sqlite3

import pytest
from pydantic import ValidationError

from tsdb_optimizer.agents import PerformanceMonitoringAgent, QueryAnalysisAgent
from tsdb_optimizer.agents.learning_feedback import LearningFeedbackAgent, feature_identity
from tsdb_optimizer.decision import AdaptiveAdvice
from tsdb_optimizer.execution import DecisionExecutionService
from tsdb_optimizer.learning.repository import ExperienceRepository
from tsdb_optimizer.schemas.common import utc_now
from tsdb_optimizer.schemas.learning import LearningContext, SampleStatus, digest
from tsdb_optimizer.schemas.monitoring import SystemSample, SystemSnapshot, SystemSourceType
from tsdb_optimizer.schemas.execution import ExecutionStatus
from tsdb_optimizer.storage import SQLiteStore
from tsdb_optimizer.strategies import build_default_registry
from phase4_support import (ScriptedAdapter, PassThroughStrategyDouble, make_test_only_spec,
                            TEST_ONLY_STRATEGY_ID, TEST_SQL, execution_result)


def sample():
    def snapshot(source):
        return SystemSnapshot(source_host=source.value, source_type=source,
            cpu_percent=10, memory_percent=20, disk_usage_percent=30,
            disk_read_bytes_per_sec=1, disk_write_bytes_per_sec=2, active_queries=0, valid=True)
    return SystemSample(client_host=snapshot(SystemSourceType.CLIENT_HOST),
                        db_host=snapshot(SystemSourceType.DB_HOST))


@pytest.fixture
def lab(tmp_path):
    store = SQLiteStore(tmp_path / 'learning.sqlite')
    store.initialize()
    registry = build_default_registry(allow_uncertified_for_testing=True)
    registry.register(PassThroughStrategyDouble(spec=make_test_only_spec()))
    adapter = ScriptedAdapter([])
    service = DecisionExecutionService(adapter=adapter, store=store, registry=registry,
                                      server_version='2.0.6', database_model='tree')
    repo = ExperienceRepository(store)
    agent = LearningFeedbackAgent(repo, registry)
    monitor = PerformanceMonitoringAgent(registry=registry)
    context = LearningContext(server_id='test-only-server', server_version='2.0.6',
        database_model='tree', dataset_epoch='test-data-v1', result_rows=1, cache_condition='warm-serial',
        load_source='client_host', load_host='client_host', load_bucket='LOW')

    def execute(strategy='S0_NATIVE', latency=10.0, failed=False, sql=TEST_SQL):
        before = sample()
        adapter.outcomes.append(lambda request, strategy_id: execution_result(
            query_id=request.query_id, strategy_id=strategy_id, sql=request.sql,
            client_wall_ms=latency, status=ExecutionStatus.ERROR if failed else ExecutionStatus.SUCCESS))
        run = service.run(sql, mode='baseline' if strategy == 'S0_NATIVE' else 'adaptive',
            adaptive_advice=None if strategy == 'S0_NATIVE' else AdaptiveAdvice(
                strategy_id=strategy, model_version='TEST_ONLY-model', sample_count=3, reason='synthetic test'))
        result = run.execution.attempts[0].execution
        record = monitor.record_execution(result, decision_id=run.decision.decision_id, snapshots=before)
        return run, record, before

    return store, repo, agent, context, execute, adapter


def ingest(lab, strategy='S0_NATIVE', latency=10, **kwargs):
    _, _, agent, context, execute, _ = lab
    run, record, before = execute(strategy, latency, **kwargs)
    feedback = agent.observe(run, context, record=record, before=before)
    return run, record, before, feedback


def test_real_protocol_records_reward_statistics_and_advice_changes_decision(lab):
    store, repo, agent, context, execute, adapter = lab
    analysis = QueryAnalysisAgent().analyze(TEST_SQL)
    assert agent.recommend(analysis, context) is None
    first = ingest(lab, latency=10)
    assert first[-1].sample_status == SampleStatus.VALID
    assert first[-1].reward == -math.log1p(.01)
    assert not first[-1].model_updated
    for latency in [20, 30]: ingest(lab, latency=latency)
    for latency in [1, 2, 3]: ingest(lab, TEST_ONLY_STRATEGY_ID, latency)
    advice = agent.recommend(analysis, context)
    assert advice.strategy_id == TEST_ONLY_STRATEGY_ID
    assert advice.sample_count == 3
    # Advice affects a later decision, still subject to the real Registry/Validator.
    from tsdb_optimizer.decision import DecisionEngine
    d = DecisionEngine(agent.registry).decide(query_id='next', original_sql=TEST_SQL,
        mode='adaptive', server_version='2.0.6', database_model='tree', adaptive_advice=advice)
    assert d.selected_strategy_id == TEST_ONLY_STRATEGY_ID
    template, feature = feature_identity(analysis)
    rows = repo.retrieve(template_key=template, feature_key=feature, context_key=context.key)
    assert len(rows) == 6
    model = repo.current_model(rows[0].group_key)
    stats = {s.strategy_id: s for s in model.statistics}
    assert stats['S0_NATIVE'].mean_latency_ms == 20
    assert stats['S0_NATIVE'].median_latency_ms == 20
    assert stats['S0_NATIVE'].variance_ms2 == pytest.approx(200 / 3)
    assert stats['S0_NATIVE'].success_rate == 1
    assert len(model.sample_execution_ids) == 6
    assert len(adapter.calls) == 6  # learning dispatched nothing
    before = sample()
    adapter.outcomes.append(lambda request, strategy: execution_result(
        query_id=request.query_id, strategy_id=strategy, sql=request.sql, client_wall_ms=2))
    service = DecisionExecutionService(adapter=adapter, store=store, registry=agent.registry,
        server_version='2.0.6', database_model='tree')
    next_run = service.run(TEST_SQL, mode='adaptive', adaptive_advice=advice)
    result = next_run.execution.attempts[0].execution
    record = PerformanceMonitoringAgent(registry=agent.registry).record_execution(
        result, decision_id=next_run.decision.decision_id, snapshots=before)
    assert record.runtime_result_check == 'NOT_CHECKED'
    assert not agent.observe(next_run, context, record=record, before=before).warnings
    assert store.get_decision(next_run.decision.decision_id)['model_version'] == advice.model_version
    assert repo.get(result.execution_id).decision_model_version == advice.model_version


def test_failed_and_low_confidence_samples_do_not_update_reward(lab):
    _, repo, agent, context, execute, _ = lab
    run, record, before, feedback = ingest(lab, failed=True)
    assert feedback.sample_status == SampleStatus.INVALID and feedback.reward is None
    for changes in ({'dataset_epoch': 'UNKNOWN'}, {'load_bucket': 'HIGH'}, {'load_source': 'unknown'}, {'result_rows': None}):
        run, record, before = execute()
        feedback = agent.observe(run, context.model_copy(update=changes), record=record, before=before)
        assert feedback.sample_status == SampleStatus.LOW_CONFIDENCE
        assert feedback.reward is None and not feedback.model_updated
    run, record, _ = execute()
    feedback = agent.observe(run, context, record=record)
    assert feedback.sample_status == SampleStatus.LOW_CONFIDENCE


def test_duplicate_and_conflicting_execution_do_not_double_count(lab):
    _, repo, agent, context, _, _ = lab
    run, record, before, feedback = ingest(lab)
    again = agent.observe(run, context, record=record, before=before)
    assert again.duplicate and not again.model_updated
    assert again.experience_id == feedback.experience_id
    changed = agent.observe(run, context.model_copy(update={'cache_condition':'different'}), record=record, before=before)
    assert 'LEARNING_FAILED' in changed.warnings[0]
    template, feature = feature_identity(QueryAnalysisAgent().analyze(TEST_SQL))
    assert len(repo.retrieve(template_key=template, feature_key=feature, context_key=context.key)) == 1


def test_mismatch_quarantines_history_revokes_models_and_cannot_be_reenabled(lab):
    _, repo, agent, context, _, _ = lab
    for _ in range(3): ingest(lab, latency=20)
    bad = None
    for _ in range(3): bad = ingest(lab, TEST_ONLY_STRATEGY_ID, 1)
    analysis = QueryAnalysisAgent().analyze(TEST_SQL)
    assert agent.recommend(analysis, context) is not None
    run, record, before, _ = bad
    mismatch = record.model_copy(update={'runtime_result_check':'MISMATCHED'})
    feedback = agent.observe(run, context, record=mismatch, before=before)
    assert feedback.sample_status == SampleStatus.INVALID and feedback.reward is None
    assert repo.is_quarantined(TEST_ONLY_STRATEGY_ID, '0.0.1')
    assert agent.recommend(analysis, context) is None
    template, feature = feature_identity(analysis)
    rows = repo.retrieve(template_key=template, feature_key=feature, context_key=context.key,
                        strategy_id=TEST_ONLY_STRATEGY_ID, strategy_version='0.0.1')
    assert len(rows) == 3
    assert all(e.sample_status == SampleStatus.INVALID and e.reward is None for e in rows)
    from tsdb_optimizer.errors import StrategyRegistryError
    with pytest.raises(StrategyRegistryError, match='quarantined'):
        agent.registry.assert_selectable(TEST_ONLY_STRATEGY_ID)
    # A later caller recommendation cannot execute the isolated strategy.
    new_run, _, _, _ = ingest(lab, TEST_ONLY_STRATEGY_ID, 1)
    assert new_run.decision.selected_strategy_id == 'S0_NATIVE'
    assert all(a.execution.strategy_id == 'S0_NATIVE' for a in new_run.execution.attempts)


@pytest.mark.parametrize('field,value', [
    ('server_id','other-server'), ('server_version','2.0.7'), ('database_model','table'),
    ('dataset_epoch','v2'), ('dataset_size',100), ('result_rows',2), ('cache_condition','cold'),
    ('result_source','CACHE_HIT'), ('fetch_mode','LIMITED'), ('load_source','db_host'),
    ('load_host','other-host'), ('load_bucket','MEDIUM'),
])
def test_context_dimensions_never_mix(lab, field, value):
    _, repo, agent, context, _, _ = lab
    ingest(lab)
    template, feature = feature_identity(QueryAnalysisAgent().analyze(TEST_SQL))
    other = context.model_copy(update={field:value})
    assert repo.retrieve(template_key=template, feature_key=feature, context_key=other.key) == []
    assert agent.recommend(QueryAnalysisAgent().analyze(TEST_SQL), other) is None


def test_features_templates_and_strategy_versions_never_mix(lab):
    _, repo, _, context, _, _ = lab
    ingest(lab)
    template, feature = feature_identity(QueryAnalysisAgent().analyze(TEST_SQL))
    for t, f in [('other-template',feature),(template,'other-features')]:
        assert repo.retrieve(template_key=t, feature_key=f, context_key=context.key) == []
    assert repo.retrieve(template_key=template, feature_key=feature, context_key=context.key,
                        strategy_id='S0_NATIVE', strategy_version='future') == []


def test_model_atomic_publish_rollback_and_stale_revision(lab):
    store, repo, agent, context, _, _ = lab
    for _ in range(3): run, record, before, _ = ingest(lab)
    template, features = feature_identity(QueryAnalysisAgent().analyze(TEST_SQL))
    group = digest([template, features, context.key])
    old = repo.current_model(group)
    fresh = repo.build_model(group)
    # Failure after inserting model but before moving the pointer rolls back both.
    with store._connect() as c:
        c.execute("CREATE TRIGGER fail_head BEFORE UPDATE ON learning_heads BEGIN SELECT RAISE(ABORT,'injected'); END")
    with pytest.raises(sqlite3.IntegrityError): repo.publish(fresh)
    assert repo.current_model(group).model_version == old.model_version
    with store._connect() as c:
        assert c.execute('SELECT 1 FROM learning_models WHERE model_version=?',(fresh.model_version,)).fetchone() is None
        c.execute('DROP TRIGGER fail_head')
    ingest(lab)
    with pytest.raises(ValueError, match='differ|revision'): repo.publish(fresh)


def test_learning_write_and_model_failures_do_not_change_query(lab, monkeypatch):
    _, repo, agent, context, execute, adapter = lab
    run, record, before = execute()
    original = run.model_dump_json()
    def fail(*args, **kwargs): raise sqlite3.OperationalError('injected failure')
    monkeypatch.setattr(repo, 'record', fail)
    feedback = agent.observe(run, context, record=record, before=before)
    assert feedback.warnings and not feedback.model_updated
    assert run.model_dump_json() == original and len(adapter.calls) == 1
    monkeypatch.undo()
    for _ in range(2): ingest(lab)
    monkeypatch.setattr(repo, 'publish', fail)
    run, record, before = execute()
    original = run.model_dump_json()
    feedback = agent.observe(run, context, record=record, before=before)
    assert any('MODEL_UPDATE_FAILED' in w for w in feedback.warnings)
    assert run.model_dump_json() == original and len(adapter.calls) == 4


def test_fake_record_is_rejected_and_no_experience_is_written(lab):
    store, _, agent, context, execute, _ = lab
    run, record, before = execute()
    feedback = agent.observe(run, context, record=record.model_copy(update={'client_wall_ms':0}), before=before)
    assert feedback.warnings
    with store._connect() as c:
        assert c.execute('SELECT count(*) FROM experiences').fetchone()[0] == 0


def test_minimum_and_improvement_thresholds(lab):
    _, repo, agent, context, _, _ = lab
    for _ in range(3): ingest(lab, latency=10)
    for _ in range(3): ingest(lab, TEST_ONLY_STRATEGY_ID, 9.9)
    assert agent.recommend(QueryAnalysisAgent().analyze(TEST_SQL), context) is None
    stricter = LearningFeedbackAgent(repo, agent.registry, min_samples=5)
    assert stricter.recommend(QueryAnalysisAgent().analyze(TEST_SQL), context) is None
    with pytest.raises(ValueError): LearningFeedbackAgent(repo, agent.registry, min_samples=1)
    with pytest.raises(ValidationError): LearningContext(server_id=' ',server_version='2.0.6',database_model='tree',dataset_epoch='v1',cache_condition='warm')


def test_v6_migration_preserves_actual_audit_rows_and_reports(lab):
    store, repo, agent, context, _, _ = lab
    run, record, before, feedback = ingest(lab)
    old_execution = store.get_execution(record.execution_id)
    old_report = store.get_execution_report(run.decision.decision_id)
    with store._connect() as c:
        for table in ('learning_heads','learning_models','experiences','strategy_quarantine','learning_group_state','learning_state'):
            c.execute(f'DROP TABLE {table}')
        c.execute('PRAGMA user_version=6')
    store.initialize()
    assert store.get_execution(record.execution_id) == old_execution
    assert store.get_execution_report(run.decision.decision_id) == old_report
    assert not agent.observe(run, context, record=record, before=before).warnings
    with store._connect() as c:
        assert c.execute('PRAGMA user_version').fetchone()[0] == 8
        assert c.execute('PRAGMA foreign_key_check').fetchall() == []


def test_isolation_survives_restart_and_only_matches_strategy_version(lab):
    store, repo, agent, context, execute, _ = lab
    run, record, before = execute(TEST_ONLY_STRATEGY_ID)
    feedback = agent.observe(run, context, record=record.model_copy(update={'runtime_result_check':'MISMATCHED'}), before=before)
    assert feedback.sample_status == SampleStatus.INVALID
    registry = build_default_registry(allow_uncertified_for_testing=True)
    registry.register(PassThroughStrategyDouble(spec=make_test_only_spec()))
    new = LearningFeedbackAgent(ExperienceRepository(SQLiteStore(store.path)), registry)
    assert TEST_ONLY_STRATEGY_ID not in registry.selectable_ids()
    assert not repo.is_quarantined(TEST_ONLY_STRATEGY_ID, 'new-version')
    from tsdb_optimizer.execution import StrategyExecutor
    from tsdb_optimizer.errors import ExecutionError
    # Even a decision issued before quarantine cannot be dispatched afterwards.
    with pytest.raises(ExecutionError, match='quarantined'):
        StrategyExecutor(adapter=lab[-1], registry=registry).execute(run.decision,
            server_version='2.0.6', database_model='tree')


def test_mismatch_blocks_runtime_even_when_persistence_fails(lab, monkeypatch):
    _, repo, agent, context, execute, _ = lab
    run, record, before = execute(TEST_ONLY_STRATEGY_ID)
    def fail(*args): raise sqlite3.OperationalError('disk failure')
    monkeypatch.setattr(repo, 'record', fail)
    feedback = agent.observe(run, context, record=record.model_copy(update={'runtime_result_check':'MISMATCHED'}), before=before)
    assert feedback.warnings
    assert TEST_ONLY_STRATEGY_ID not in agent.registry.selectable_ids()


def test_unsupported_parser_is_low_confidence_not_invented_template(lab):
    from phase4_support import PARSER_UNSUPPORTED_SQL
    _, repo, agent, context, execute, _ = lab
    run, record, before = execute(sql=PARSER_UNSUPPORTED_SQL)
    feedback = agent.observe(run, context, record=record, before=before)
    assert not feedback.warnings and feedback.sample_status == SampleStatus.LOW_CONFIDENCE
    row = repo.get(record.execution_id)
    assert row.normalized_template is None and row.reward is None


@pytest.mark.parametrize('change', [
    {'server_version':'9.9'}, {'database_model':'table'},
])
def test_uncertified_environment_cannot_train(lab, change):
    _, _, agent, context, execute, _ = lab
    run, record, before = execute()
    feedback = agent.observe(run, context.model_copy(update=change), record=record, before=before)
    assert feedback.sample_status == SampleStatus.INVALID and feedback.reward is None


def test_retrieval_failure_returns_no_advice(lab, monkeypatch, caplog):
    _, repo, agent, context, _, _ = lab
    def fail(*args): raise sqlite3.DatabaseError('corrupted')
    monkeypatch.setattr(repo, 'current_model', fail)
    assert agent.recommend(QueryAnalysisAgent().analyze(TEST_SQL), context) is None
    assert any(getattr(r,'event',None) == 'learning_retrieval_error' for r in caplog.records)


def test_in_memory_repository_can_publish_atomic_models():
    store = SQLiteStore(':memory:')
    store.initialize()
    repo = ExperienceRepository(store)
    model = repo.build_model('empty-context')
    repo.publish(model)
    assert repo.current_model('empty-context') == model
    store.close()


def test_forged_model_statistics_cannot_be_published(lab):
    _, repo, _, context, _, _ = lab
    run, record, before, _ = ingest(lab)
    analysis = QueryAnalysisAgent().analyze(TEST_SQL)
    template, features = feature_identity(analysis)
    model = repo.build_model(digest([template,features,context.key]))
    stat = model.statistics[0].model_copy(update={'median_latency_ms':0.0})
    with pytest.raises(ValueError, match='statistics'):
        repo.publish(model.model_copy(update={'statistics':(stat,)}))


def test_load_groups_have_independent_models_and_preferences(lab):
    _, repo, agent, low, execute, _ = lab
    analysis = QueryAnalysisAgent().analyze(TEST_SQL)
    medium = low.model_copy(update={'load_bucket': 'MEDIUM'})

    def add(context, strategy, latency):
        run, record, before = execute(strategy, latency)
        if context.load_bucket == 'MEDIUM':
            # Explicit synthetic state, created before the synthetic execution.
            before = before.model_copy(update={'client_host': before.client_host.model_copy(
                update={'cpu_percent': 60.0})})
        feedback = agent.observe(run, context, record=record, before=before)
        assert not feedback.warnings and feedback.sample_status == SampleStatus.VALID
        return run, record, before

    for _ in range(3): add(low, 'S0_NATIVE', 20)
    for _ in range(3): add(low, TEST_ONLY_STRATEGY_ID, 1)
    low_model_version = agent.recommend(analysis, low).model_version
    for _ in range(3): add(medium, 'S0_NATIVE', 5)
    last = None
    for _ in range(3): last = add(medium, TEST_ONLY_STRATEGY_ID, 30)
    # Updating MEDIUM must not invalidate an unrelated LOW model.
    assert agent.recommend(analysis, low).model_version == low_model_version
    assert agent.recommend(analysis, medium) is None  # Native wins here.
    template, features = feature_identity(analysis)
    model = repo.current_model(digest([template, features, medium.key]))
    assert {s.strategy_id: s.median_latency_ms for s in model.statistics} == {
        'S0_NATIVE': 5, TEST_ONLY_STRATEGY_ID: 30}
    # A semantic mismatch quarantines this version in every load context.
    run, record, before = last
    feedback = agent.observe(run, medium, record=record.model_copy(
        update={'runtime_result_check':'MISMATCHED'}), before=before)
    assert feedback.sample_status == SampleStatus.INVALID
    assert agent.recommend(analysis, low) is None
    assert repo.current_model(digest([template, features, low.key])) is None


def test_failed_sample_counts_for_success_rate_but_has_no_latency_reward(lab):
    _, repo, agent, context, _, _ = lab
    for _ in range(3): ingest(lab)
    run, record, before, feedback = ingest(lab, failed=True)
    assert feedback.sample_status == SampleStatus.INVALID
    template, features = feature_identity(QueryAnalysisAgent().analyze(TEST_SQL))
    group = digest([template,features,context.key])
    assert repo.current_model(group) is None  # Old success-only snapshot is stale.
    stats = repo.build_model(group).statistics[0]
    assert stats.observed_count == 4 and stats.valid_count == 3
    assert stats.success_rate == .75 and not stats.eligible
    assert stats.mean_latency_ms == 10 and stats.variance_ms2 == 0
    assert agent.recommend(QueryAnalysisAgent().analyze(TEST_SQL), context) is None


def test_forged_analysis_cannot_create_a_false_context(lab):
    _, _, agent, context, execute, _ = lab
    run, record, before = execute()
    forged = QueryAnalysisAgent().analyze(TEST_SQL, query_id=run.decision.query_id)
    forged = forged.model_copy(update={'features': forged.features.model_copy(
        update={'query_hash':'f' * 64})})
    feedback = agent.observe(run, context, record=record, before=before, analysis=forged)
    assert feedback.experience_id is None and feedback.warnings


@pytest.mark.parametrize('timeout', [False, True])
def test_timeout_and_fallback_attempts_never_receive_performance_reward(lab, timeout):
    store, repo, agent, context, _, adapter = lab
    from tsdb_optimizer.schemas.execution import RetryUnsafeReason
    before = sample()
    adapter.outcomes.append(lambda request, strategy: execution_result(
        query_id=request.query_id, strategy_id=strategy, sql=request.sql,
        status=ExecutionStatus.ERROR, timed_out=timeout,
        request_dispatched=timeout, request_completed=False,
        retry_unsafe_reason=RetryUnsafeReason.TIMEOUT_OR_UNKNOWN_SERVER_STATE if timeout else None))
    if not timeout:
        adapter.outcomes.append(lambda request, strategy: execution_result(
            query_id=request.query_id, strategy_id=strategy, sql=request.sql))
    service = DecisionExecutionService(adapter=adapter, store=store, registry=agent.registry,
        server_version='2.0.6', database_model='tree')
    run = service.run(TEST_SQL, mode='adaptive', adaptive_advice=AdaptiveAdvice(
        strategy_id=TEST_ONLY_STRATEGY_ID, model_version='TEST_ONLY', reason='failure test', sample_count=3))
    assert run.execution.fallback_used is not timeout
    assert len(run.execution.attempts) == (1 if timeout else 2)
    monitor = PerformanceMonitoringAgent(registry=agent.registry)
    statuses = []
    for attempt in run.execution.attempts:
        record = monitor.record_execution(attempt.execution, decision_id=run.decision.decision_id,
                                          snapshots=before)
        feedback = agent.observe(run, context, record=record, before=before)
        assert not feedback.warnings and feedback.reward is None and not feedback.model_updated
        statuses.append(feedback.sample_status)
    assert statuses == ([SampleStatus.INVALID] if timeout else [SampleStatus.INVALID, SampleStatus.LOW_CONFIDENCE])


def test_different_result_sizes_are_not_comparable_reward(lab):
    _, _, agent, context, execute, _ = lab
    run, record, before = execute()
    feedback = agent.observe(run, context.model_copy(update={'result_rows':2}),
                             record=record, before=before)
    assert feedback.sample_status == SampleStatus.INVALID and feedback.reward is None
    assert feedback.quality_reasons == ['RESULT_SIZE_MISMATCH']
