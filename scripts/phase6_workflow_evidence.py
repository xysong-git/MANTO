"""Read-only real single-query evidence. No passwords, SQL literals or rows exported."""
import argparse
import json
from pathlib import Path

from tsdb_optimizer.adapters import IoTDBTreeAdapter
from tsdb_optimizer.config import load_settings
from tsdb_optimizer.orchestration import Orchestrator, State
from tsdb_optimizer.schemas.learning import LearningContext
from tsdb_optimizer.storage import SQLiteStore


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--sqlite', type=Path, required=True)
    args = parser.parse_args()
    adapter = IoTDBTreeAdapter(load_settings())
    store = SQLiteStore(args.sqlite)
    try:
        server = adapter.doctor().server
        if server.version != '2.0.6':
            raise RuntimeError('evidence requires verified server version 2.0.6')
        adapter.connect()
        calls = []
        execute = adapter._session.execute_query_statement
        def counted(sql, *a, **kw):
            from tsdb_optimizer.sql_guard import sql_sha256
            calls.append(sql_sha256(sql))
            return execute(sql, *a, **kw)
        adapter._session.execute_query_statement = counted
        flow = Orchestrator(adapter=adapter, store=store, context=LearningContext(
            server_id='redacted-local-iotdb', server_version=server.version, database_model='tree',
            dataset_epoch='UNKNOWN', cache_condition='UNKNOWN', load_source='client_host'))
        flow.monitor.sample_once()
        sql = 'SELECT s_1666, s_1766 FROM root.db800.g_0.d_0, root.db800.g_0.d_1 WHERE time = 1640966450000'
        result = flow.run(sql, mode='baseline', timeout_ms=30000, source='phase6:real-evidence')
        assert result.trace.events[-1].state == State.SUCCEEDED, result.trace.model_dump_json()
        assert result.trace.database_dispatches == len(calls) == 1
        assert calls == [result.trace.sql_sha256]
        assert len(result.records) == len(result.feedback) == 1
        assert result.records[0].runtime_result_check == 'NOT_CHECKED'
        assert store.get_workflow_trace(result.trace.query_id) == result.trace
        record = result.records[0]
        evidence = {'evidence_type': 'REAL_IOTDB_READONLY', 'server_version': server.version,
            'database_model': 'tree', 'probe_scope': 'doctor completed before SDK counter attached',
            'sdk_query_calls_in_workflow': len(calls), 'sql_hashes': calls,
            'trace': result.trace.model_dump(mode='json'),
            'execution': {'query_id': record.query_id, 'decision_id': record.decision_id,
                'execution_id': record.execution_id, 'success': record.success,
                'client_wall_ms': record.client_wall_ms, 'rows_returned': record.rows_returned,
                'runtime_result_check': record.runtime_result_check},
            'note': 'No SQL text, result values, credentials, host addresses or snapshot metric values exported. '
                    'Unknown epoch/cache condition intentionally prevents reward/model update.'}
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(json.dumps(evidence, ensure_ascii=False, indent=2)+'\n')
        print(json.dumps({'output': str(args.output), 'query_id': result.trace.query_id,
            'sdk_query_calls': len(calls), 'state': result.trace.events[-1].state.value}))
    finally:
        adapter.close()
        store.close()


if __name__ == '__main__':
    main()
