"""Phase-6 F-1/F-2 regressions. All injected timings/metrics are synthetic."""
import json
import logging
from datetime import timedelta

import pytest

from test_orchestrator import lab
from test_learning import sample
from phase4_support import TEST_SQL
from tsdb_optimizer.logging import JsonFormatter
from tsdb_optimizer.schemas.execution import ExecutionStatus, RetryUnsafeReason
from tsdb_optimizer.schemas.monitoring import SystemSourceType
import tsdb_optimizer.execution.executor as executor_module


@pytest.mark.parametrize('source', ['client_host', 'db_host', 'db_metrics'])
@pytest.mark.parametrize('delay_stage', ['decision_save', 'executor_validation'])
def test_expiry_after_decision_uses_one_native_attempt(lab, monkeypatch, source, delay_stage):
    flow, adapter, store, enqueue = lab
    enqueue(); flow.run(TEST_SQL)
    before = sample()
    if source == 'db_metrics':
        before.db_metrics = before.db_host.model_copy(update={'source_type': SystemSourceType.DB_METRICS})
    # Only the chosen source expires. Clock injection avoids flaky wall sleeps.
    for name in ('client_host', 'db_host', 'db_metrics'):
        snap = getattr(before, name)
        if snap is not None:
            snap.stale_after_ms = 5000 if name == source else 60000
    monkeypatch.setattr(flow.monitor, 'sample_once', lambda: before)
    boundary = getattr(before, source).captured_at + timedelta(milliseconds=5000)
    if delay_stage == 'decision_save':
        save = store.record_decision
        def delayed(decision):
            save(decision)
            monkeypatch.setattr(executor_module, 'utc_now', lambda: boundary)
        monkeypatch.setattr(store, 'record_decision', delayed)
    else:
        validate = flow.executor._preflight_selected
        def delayed(*args, **kwargs):
            result = validate(*args, **kwargs)
            monkeypatch.setattr(executor_module, 'utc_now', lambda: boundary)
            return result
        monkeypatch.setattr(flow.executor, '_preflight_selected', delayed)
    enqueue()
    result = flow.run(TEST_SQL)
    assert result.trace.events[-1].state.value == 'SUCCEEDED'
    assert result.run.decision.selected_strategy_id == 'S1_REPEAT_CACHE'
    assert result.run.decision.query_occurrence == 2
    report = result.run.execution
    assert report.fallback_used and len(report.attempts) == 1
    assert report.fallback_trigger.value == 'CANDIDATE_VALIDATION_FAILED'
    assert source in report.fallback_reason
    assert 'RESOURCE_STATE_UNUSABLE_AT_DISPATCH' in report.fallback_reason
    assert adapter.calls[-1][2] == 'S0_NATIVE' and len(adapter.calls) == 2
    assert result.trace.database_dispatches == 1
    assert len(result.records) == len(result.feedback) == 1
    assert result.feedback[0].reward is None
    assert store.get_decision(result.run.decision.decision_id)['selected_strategy_id'] == 'S1_REPEAT_CACHE'
    assert store.get_workflow_trace(result.trace.query_id).events[6].details['fallback_reason'] == report.fallback_reason


def test_fresh_state_does_not_veto_selected_cache(lab):
    flow, adapter, _, enqueue = lab
    enqueue(); flow.run(TEST_SQL)
    enqueue(); result = flow.run(TEST_SQL)
    assert adapter.calls[-1][2] == 'S1_REPEAT_CACHE'
    assert not result.run.execution.fallback_used


@pytest.mark.parametrize('outcome', ['success', 'rejected', 'timeout', 'fallback', 'exception'])
def test_formatted_transition_logs_keep_state_and_ids(lab, caplog, outcome):
    flow, _, _, enqueue = lab
    sql = TEST_SQL
    if outcome == 'fallback':
        enqueue(); flow.run(sql)
        enqueue(status=ExecutionStatus.ERROR, request_dispatched=False, request_completed=False)
        enqueue()
    elif outcome == 'rejected':
        sql = 'DELETE FROM root.test'
    elif outcome == 'timeout':
        enqueue(status=ExecutionStatus.ERROR, timed_out=True, request_completed=False,
                retry_unsafe_reason=RetryUnsafeReason.TIMEOUT_OR_UNKNOWN_SERVER_STATE)
    elif outcome == 'exception':
        flow.executor.adapter.outcomes.append(RuntimeError('TEST ONLY unknown completion'))
    else:
        enqueue()
    caplog.clear()
    with caplog.at_level(logging.INFO):
        result = flow.run(sql)
    records = [r for r in caplog.records if getattr(r, 'event', '') == 'workflow_transition']
    wire = [json.loads(JsonFormatter().format(r)) for r in records]
    assert len(wire) == len(result.trace.events)
    for log, event in zip(wire, result.trace.events):
        assert log['state'] == event.state.value
        assert log['event_index'] == event.index
        assert log['query_id'] == event.query_id
        assert log.get('decision_id') == event.decision_id
        assert log['execution_ids'] == event.execution_ids
        assert log['elapsed_ms'] == event.elapsed_ms

    executed_logs = [entry for entry in wire if entry['state'] == 'EXECUTED']
    if executed_logs:
        assert executed_logs[0]['fallback_used'] == (outcome == 'fallback')
        assert executed_logs[0]['fallback_trigger'] == result.run.execution.fallback_trigger.value
        assert executed_logs[0]['final_status'] == result.run.execution.final_status.value
