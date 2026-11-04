"""Independent Phase-6 acceptance probes; synthetic only, no DB requests."""
import json
import logging
import time
from datetime import timedelta

from test_orchestrator import lab
from test_learning import sample
from phase4_support import TEST_SQL
from tsdb_optimizer.schemas.common import utc_now
from tsdb_optimizer.logging import JsonFormatter


def test_expiry_during_decision_persistence_cannot_dispatch_non_native(lab, monkeypatch):
    flow, adapter, store, enqueue = lab
    enqueue()
    assert flow.run(TEST_SQL).run.decision.selected_strategy_id == 'S0_NATIVE'
    before = sample()
    before.client_host.stale_after_ms = 100
    monkeypatch.setattr(flow.monitor, 'sample_once', lambda: before)
    save = store.record_decision
    observed = {}
    def delayed_save(decision):
        observed['fresh_at_save_entry'] = before.client_host.is_usable(now=utc_now())
        save(decision)
        # Simulate an audit fsync / lock delay; no mutation of captured timestamps.
        until = before.client_host.captured_at + timedelta(milliseconds=120)
        time.sleep(max(0, (until-utc_now()).total_seconds()))
        observed['fresh_at_save_exit'] = before.client_host.is_usable(now=utc_now())
    monkeypatch.setattr(store, 'record_decision', delayed_save)
    enqueue()
    result = flow.run(TEST_SQL)
    assert observed == {'fresh_at_save_entry': True, 'fresh_at_save_exit': False}
    assert len(adapter.calls) == 2
    assert adapter.calls[-1][2] == 'S0_NATIVE', (
        f'expired snapshot still dispatched {adapter.calls[-1][2]}; '
        f'trace={result.trace.effective_mode}; warnings={result.trace.warnings}')


def test_workflow_transition_log_preserves_state(lab, caplog):
    flow, _, _, enqueue = lab
    enqueue()
    with caplog.at_level(logging.INFO):
        result = flow.run(TEST_SQL)
    records = [r for r in caplog.records if getattr(r, 'event', '') == 'workflow_transition']
    assert len(records) == 10
    wire = [json.loads(JsonFormatter().format(r)) for r in records]
    assert [r.get('state') for r in wire] == [e.state.value for e in result.trace.events]
