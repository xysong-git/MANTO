"""Real SDK call counter plus complete four-agent trace; no EXPLAIN or comparison."""
import os

import pytest

from tsdb_optimizer.adapters import IoTDBTreeAdapter
from tsdb_optimizer.config import load_settings
from tsdb_optimizer.orchestration import Orchestrator, State
from tsdb_optimizer.schemas.learning import LearningContext
from tsdb_optimizer.storage import SQLiteStore
from tsdb_optimizer.strategies import build_default_registry

pytestmark = pytest.mark.integration


def test_real_four_agent_trace_counts_sdk_dispatches(tmp_path, monkeypatch):
    if os.environ.get('IOTDB_INTEGRATION') != '1':
        pytest.skip('set IOTDB_INTEGRATION=1 for real IoTDB')
    adapter = IoTDBTreeAdapter(load_settings())
    store = SQLiteStore(tmp_path/'workflow.sqlite')
    sql = 'SELECT s_1666, s_1766 FROM root.db800.g_0.d_0, root.db800.g_0.d_1 WHERE time = 1640966450000'
    try:
        server = adapter.doctor().server  # capability probes outside workflow count
        assert server.version == '2.0.6'
        adapter.connect()
        calls = []
        original = adapter._session.execute_query_statement
        def counted(sql, *args, **kwargs):
            calls.append(sql)
            return original(sql, *args, **kwargs)
        monkeypatch.setattr(adapter._session, 'execute_query_statement', counted)
        flow = Orchestrator(adapter=adapter, store=store,
            registry=build_default_registry(cache_epoch='phase6-static-test'),
            context=LearningContext(server_id='integration-server', server_version=server.version,
                database_model='tree', dataset_epoch='phase6-static-test', cache_condition='serial-unknown-warmth',
                load_source='client_host'))
        flow.monitor.sample_once()  # CPU sampling baseline, no DB call
        totals = []
        for _ in range(3):
            count = len(calls)
            result = flow.run(sql, mode='rule_based', timeout_ms=30000)
            assert result.trace.events[-1].state == State.SUCCEEDED
            assert result.trace.database_dispatches == len(calls)-count
            assert len(result.trace.events) == 10
            assert len(result.records) == len(result.feedback) == 1
            assert result.records[0].runtime_result_check == 'NOT_CHECKED'
            assert store.get_workflow_trace(result.trace.query_id) == result.trace
            assert not result.feedback[0].warnings
            assert result.feedback[0].sample_status.value in {'VALID', 'LOW_CONFIDENCE'}
            totals.append(result.trace.database_dispatches)
        # Resource pressure can conservatively veto cache; never force load to be low.
        assert 1 <= sum(totals) <= 3
        assert calls and all(q == sql for q in calls)
        assert store.count('executions') == 3
    finally:
        adapter.close()
        store.close()
