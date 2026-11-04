"""Synthetic failure injection; no simulated evidence is called real DB evidence."""
from datetime import timedelta
import sqlite3

import pytest
from pydantic import ValidationError

from tsdb_optimizer.agents import PerformanceMonitoringAgent
from tsdb_optimizer.orchestration import Orchestrator, State, WorkflowTrace
from tsdb_optimizer.schemas.common import utc_now
from tsdb_optimizer.schemas.execution import ExecutionStatus, RetryUnsafeReason
from tsdb_optimizer.schemas.learning import LearningContext
from tsdb_optimizer.storage import SQLiteStore
from tsdb_optimizer.strategies import build_default_registry
from phase4_support import ScriptedAdapter, TEST_SQL, PARSER_UNSUPPORTED_SQL, execution_result
from test_learning import sample, lab as learning_lab, ingest


@pytest.fixture
def lab(tmp_path, monkeypatch):
    adapter = ScriptedAdapter([])
    store = SQLiteStore(tmp_path/'workflow.sqlite')
    registry = build_default_registry(cache_epoch='test-v1')
    monitor = PerformanceMonitoringAgent(registry=registry)
    monkeypatch.setattr(monitor, 'sample_once', sample)
    context = LearningContext(server_id='test', server_version='2.0.6', database_model='tree',
        dataset_epoch='test-v1', cache_condition='warm-serial', load_source='client_host', result_rows=1)
    flow = Orchestrator(adapter=adapter, store=store, registry=registry, monitor=monitor, context=context)
    def enqueue(**kw):
        adapter.outcomes.append(lambda req, strategy: execution_result(query_id=req.query_id,
            strategy_id=strategy, sql=req.sql, **kw))
    return flow, adapter, store, enqueue


def states(result):
    return [e.state.value for e in result.trace.events]


@pytest.mark.parametrize('mode', ['baseline', 'rule_based', 'adaptive'])
def test_complete_trace_ids_one_dispatch_and_real_records(lab, mode):
    flow, adapter, store, enqueue = lab
    enqueue()
    result = flow.run(TEST_SQL, mode=mode)
    assert states(result) == ['RECEIVED', 'PARSED', 'SNAPSHOT', 'RETRIEVED', 'DECIDED',
                             'VALIDATED', 'EXECUTED', 'MONITORED', 'LEARNED', 'SUCCEEDED'], result
    assert len(adapter.calls) == result.trace.database_dispatches == 1
    assert result.records[0].runtime_result_check == 'NOT_CHECKED'
    assert result.feedback[0].sample_status.value == 'VALID'
    assert store.count('queries') == store.count('decisions') == store.count('executions') == 1
    assert store.get_workflow_trace(result.trace.query_id) == result.trace
    assert flow.learner.repository.get(result.records[0].execution_id).query_id == result.trace.query_id
    assert result.trace.events[-1].decision_id == result.run.decision.decision_id
    assert result.trace.events[-1].execution_ids == [result.records[0].execution_id]
    assert result.request_timing.body_ms >= result.trace.events[-1].elapsed_ms - .001
    parsed = result.trace.events[1]
    assert parsed.details['parsing_ms'] >= 0
    assert parsed.details['query_persistence_ms'] >= 0


def test_historical_receipt_does_not_gain_null_timing_field(lab):
    from tsdb_optimizer.orchestration.models import WorkflowResult
    flow, _, _, enqueue = lab
    enqueue()
    raw = flow.run(TEST_SQL).model_dump(mode='json', round_trip=True)
    raw.pop('request_timing')
    restored = WorkflowResult.model_validate(raw)
    assert restored.request_timing is None
    assert restored.model_dump(mode='json', round_trip=True) == raw


def test_reject_unsafe_before_database_and_persist_trace(lab):
    flow, adapter, store, _ = lab
    result = flow.run('DELETE FROM root.secret')
    assert states(result) == ['RECEIVED', 'REJECTED']
    assert not adapter.calls and result.trace.database_dispatches == 0
    assert store.get_workflow_trace(result.trace.query_id) is not None
    assert 'root.secret' not in result.trace.model_dump_json()


def test_unsupported_readonly_stays_native_on_repeats(lab):
    flow, adapter, _, enqueue = lab
    for _ in range(2):
        enqueue()
        result = flow.run(PARSER_UNSUPPORTED_SQL)
        assert states(result)[-1] == 'SUCCEEDED'
        assert result.run.decision.selected_strategy_id == 'S0_NATIVE'
        assert 'PARSER_UNSUPPORTED_NATIVE' in result.trace.warnings
        assert result.feedback[0].reward is None
    assert len(adapter.calls) == 2


@pytest.mark.parametrize('kind', ['stale', 'invalid', 'future', 'failure'])
def test_bad_client_snapshot_degrades_to_native(lab, monkeypatch, kind):
    flow, adapter, _, enqueue = lab
    enqueue(); flow.run(TEST_SQL)
    before = sample()
    if kind == 'failure':
        def fail(): raise RuntimeError('snapshot unavailable')
        monkeypatch.setattr(flow.monitor, 'sample_once', fail)
    else:
        updates = {'captured_at': utc_now()-timedelta(days=1)} if kind == 'stale' else (
            {'captured_at': utc_now()+timedelta(days=1)} if kind == 'future' else {'valid': False})
        before = before.model_copy(update={'client_host': before.client_host.model_copy(update=updates)})
        monkeypatch.setattr(flow.monitor, 'sample_once', lambda: before)
    enqueue()
    result = flow.run(TEST_SQL, mode='adaptive')
    assert result.run.decision.selected_strategy_id == 'S0_NATIVE'
    assert result.trace.effective_mode == 'baseline'
    assert result.feedback[0].reward is None
    assert states(result)[-1] == 'SUCCEEDED'
    assert len(adapter.calls) == 2


@pytest.mark.parametrize('target', ['record', 'publish', 'observe', 'monitor', 'trace', 'execution'])
def test_auxiliary_failure_never_replaces_success(lab, monkeypatch, target):
    flow, adapter, store, enqueue = lab
    def fail(*args, **kwargs): raise sqlite3.OperationalError('TEST ONLY failure')
    if target == 'record': monkeypatch.setattr(flow.learner.repository, 'record', fail)
    elif target == 'publish':
        for _ in range(2): enqueue(); flow.run(TEST_SQL, mode='baseline')
        monkeypatch.setattr(flow.learner.repository, 'publish', fail)
    elif target == 'observe': monkeypatch.setattr(flow.learner, 'observe', fail)
    elif target == 'monitor': monkeypatch.setattr(flow.monitor, 'record_execution', fail)
    elif target == 'trace': monkeypatch.setattr(store, 'record_workflow_trace', fail)
    else: monkeypatch.setattr(store, 'record_execution', fail)
    count = len(adapter.calls)
    enqueue()
    result = flow.run(TEST_SQL, mode='baseline')
    assert states(result)[-1] == 'SUCCEEDED'
    assert result.run.execution.final_status == ExecutionStatus.SUCCESS
    assert result.trace.warnings
    assert len(adapter.calls) == count + 1


def test_timeout_no_retry_and_invalid_learning(lab):
    flow, adapter, _, enqueue = lab
    enqueue(status=ExecutionStatus.ERROR, timed_out=True, request_completed=False,
            retry_unsafe_reason=RetryUnsafeReason.TIMEOUT_OR_UNKNOWN_SERVER_STATE)
    result = flow.run(TEST_SQL)
    assert states(result)[-1] == 'TIMED_OUT'
    assert len(adapter.calls) == 1
    assert not result.run.execution.fallback_used
    assert result.feedback[0].sample_status.value == 'INVALID'
    assert result.feedback[0].reward is None


def test_predispatch_cache_failure_falls_back_once(lab):
    flow, adapter, _, enqueue = lab
    enqueue(); flow.run(TEST_SQL)
    enqueue(status=ExecutionStatus.ERROR, request_dispatched=False, request_completed=False)
    enqueue()
    result = flow.run(TEST_SQL)
    assert states(result)[-1] == 'SUCCEEDED'
    assert result.run.execution.fallback_used
    assert result.trace.database_dispatches == 1
    assert len(result.records) == len(result.feedback) == 2
    assert all(f.reward is None for f in result.feedback)
    assert len(adapter.calls) == 3


def test_executor_exception_unknown_dispatch_not_retry(lab):
    flow, adapter, _, _ = lab
    adapter.outcomes.append(RuntimeError('uncertain remote completion'))
    result = flow.run(TEST_SQL)
    assert states(result)[-1] == 'FAILED'
    assert result.trace.database_dispatches is None
    assert len(adapter.calls) == 1
    assert not result.records and not result.feedback


def test_lifecycle_rejects_invalid_transition_and_wrong_ids(lab):
    flow, _, _, enqueue = lab
    enqueue(); result = flow.run(TEST_SQL)
    body = result.trace.model_dump()
    body['events'][1]['state'] = 'LEARNED'
    with pytest.raises(ValidationError): WorkflowTrace.model_validate(body)
    body = result.trace.model_dump()
    body['events'][2]['query_id'] = 'wrong'
    with pytest.raises(ValidationError): WorkflowTrace.model_validate(body)


def test_adaptive_uses_only_qualified_prior_context_and_keeps_model_id(learning_lab, monkeypatch):
    from phase4_support import TEST_ONLY_STRATEGY_ID
    store, repo, learner, context, _, adapter = learning_lab
    for _ in range(3): ingest(learning_lab, latency=30)
    for _ in range(3): ingest(learning_lab, TEST_ONLY_STRATEGY_ID, latency=2)
    monitor = PerformanceMonitoringAgent(registry=learner.registry)
    monkeypatch.setattr(monitor, 'sample_once', sample)
    flow = Orchestrator(adapter=adapter, store=store, registry=learner.registry,
                        learner=learner, monitor=monitor, context=context)
    adapter.outcomes.append(lambda req, strategy: execution_result(
        query_id=req.query_id, strategy_id=strategy, sql=req.sql, client_wall_ms=2))
    result = flow.run(TEST_SQL, mode='adaptive')
    assert states(result)[-1] == 'SUCCEEDED'
    assert result.run.decision.selected_strategy_id == TEST_ONLY_STRATEGY_ID
    version = result.run.decision.model_version
    assert version and result.trace.events[3].details['model_version'] == version
    assert repo.get(result.records[0].execution_id).decision_model_version == version
    assert len(adapter.calls) == 7  # six historical, one current; never execute alternatives


def test_learning_retrieval_failure_still_native(lab, monkeypatch):
    flow, adapter, _, enqueue = lab
    def fail(*args, **kwargs): raise sqlite3.OperationalError('TEST ONLY retrieval failure')
    monkeypatch.setattr(flow.learner, 'recommend', fail)
    enqueue()
    result = flow.run(TEST_SQL, mode='adaptive')
    assert states(result)[-1] == 'SUCCEEDED'
    assert any(w.startswith('RETRIEVAL_FAILED') for w in result.trace.warnings)
    assert result.run.decision.selected_strategy_id == 'S0_NATIVE'
    assert len(adapter.calls) == 1


def test_snapshot_expiring_during_retrieval_forces_baseline(lab, monkeypatch):
    flow, _, _, enqueue = lab
    before = sample()
    monkeypatch.setattr(flow.monitor, 'sample_once', lambda: before)
    original = flow.learner.recommend
    def expire(*args, **kwargs):
        advice = original(*args, **kwargs)
        before.client_host.captured_at = utc_now()-timedelta(days=1)
        return advice
    monkeypatch.setattr(flow.learner, 'recommend', expire)
    enqueue()
    result = flow.run(TEST_SQL, mode='adaptive')
    assert result.trace.effective_mode == 'baseline'
    assert result.feedback[0].reward is None


def test_v7_migration_retains_existing_queries_and_adds_trace(tmp_path):
    store = SQLiteStore(tmp_path/'old.sqlite')
    store.initialize()
    from tsdb_optimizer.schemas import QueryRequest
    from tsdb_optimizer.sql_guard import sql_sha256
    request = QueryRequest(sql=TEST_SQL)
    store.record_query(request, sql_sha256(TEST_SQL))
    with store._connect() as connection:
        connection.execute('DROP TABLE workflow_traces')
        connection.execute('PRAGMA user_version = 7')
    store.initialize()
    assert store.count('queries') == 1
    assert store.get_workflow_trace(request.query_id) is None
    with store._connect() as connection:
        assert connection.execute('PRAGMA user_version').fetchone()[0] == 8


@pytest.mark.parametrize('source', ['client_host', 'db_host'])
def test_high_resource_snapshot_vetoes_repeat_cache(lab, monkeypatch, source):
    flow, _, _, enqueue = lab
    enqueue(); flow.run(TEST_SQL)
    before = sample()
    setattr(getattr(before, source), 'cpu_percent', 99)
    monkeypatch.setattr(flow.monitor, 'sample_once', lambda: before)
    enqueue()
    result = flow.run(TEST_SQL)
    assert result.run.decision.selected_strategy_id == 'S0_NATIVE'
    assert any('RESOURCE_THRESHOLD_EXCEEDED' in w for w in result.run.decision.warnings)
    assert result.feedback[0].reward is None


def test_stale_db_snapshot_degrades_even_with_fresh_client(lab, monkeypatch):
    flow, _, _, enqueue = lab
    enqueue(); flow.run(TEST_SQL)
    before = sample()
    before.db_host.captured_at = utc_now()-timedelta(days=1)
    monkeypatch.setattr(flow.monitor, 'sample_once', lambda: before)
    enqueue()
    result = flow.run(TEST_SQL)
    assert result.trace.effective_mode == 'baseline'
    assert 'DB_HOST_STATE_UNAVAILABLE_OR_STALE_NATIVE' in result.trace.warnings


def test_wrong_declared_result_size_cannot_update_rewards(lab):
    flow, _, _, enqueue = lab
    enqueue(row_count=9)
    result = flow.run(TEST_SQL)
    assert states(result)[-1] == 'SUCCEEDED'
    assert result.records[0].rows_returned == 9
    assert result.feedback[0].sample_status.value == 'INVALID'
    assert result.feedback[0].reward is None


def test_snapshot_sampling_failure_is_not_fabricated(lab, monkeypatch):
    flow, adapter, _, enqueue = lab
    def fail(): raise RuntimeError('TEST ONLY unavailable metrics')
    monkeypatch.setattr(flow.monitor, 'sample_once', fail)
    enqueue()
    result = flow.run(TEST_SQL)
    assert result.records[0].snapshot_ids == []
    assert result.feedback[0].sample_status.value == 'LOW_CONFIDENCE'
    assert len(adapter.calls) == 1


def test_real_retrieval_repository_error_visible_in_trace(lab, monkeypatch):
    flow, _, _, enqueue = lab
    def fail(*args, **kwargs): raise sqlite3.OperationalError('TEST ONLY broken model table')
    monkeypatch.setattr(flow.learner.repository, 'current_model', fail)
    enqueue()
    result = flow.run(TEST_SQL, mode='adaptive')
    assert states(result)[-1] == 'SUCCEEDED'
    assert 'RETRIEVAL_FAILED: OperationalError' in result.trace.warnings
