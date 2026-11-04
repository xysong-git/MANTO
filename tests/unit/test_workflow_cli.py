"""CLI workload contract and real Orchestrator with a TEST ONLY adapter."""
import json

from tsdb_optimizer import cli
from tsdb_optimizer.agents import PerformanceMonitoringAgent
from phase4_support import ScriptedAdapter, TEST_SQL, execution_result
from test_learning import sample


def test_workload_serial_traces_and_rejection_continues(tmp_path, monkeypatch, capsys):
    path = tmp_path/'workload.json'
    path.write_text(json.dumps([TEST_SQL, 'DELETE FROM root.test', TEST_SQL]))
    adapter = ScriptedAdapter([lambda req, strategy: execution_result(
        query_id=req.query_id, strategy_id=strategy, sql=req.sql) for _ in range(2)])
    monkeypatch.setattr(cli, 'IoTDBTreeAdapter', lambda settings: adapter)
    monkeypatch.setattr(PerformanceMonitoringAgent, 'sample_once', lambda self: sample())
    code = cli.main(['--sqlite', str(tmp_path/'cli.sqlite'), '--log-level', 'CRITICAL',
        'workload', '--file', str(path), '--mode', 'baseline'])
    assert code == 1
    lines = [json.loads(line) for line in capsys.readouterr().out.splitlines()]
    assert [r['trace']['events'][-1]['state'] for r in lines] == ['SUCCEEDED', 'REJECTED', 'SUCCEEDED']
    assert len({r['trace']['query_id'] for r in lines}) == 3
    assert len(adapter.calls) == 2


def test_workload_not_naively_split_or_accepted_as_sql(tmp_path, capsys):
    path = tmp_path/'bad.json'
    path.write_text('SELECT x FROM root.test; SELECT y FROM root.test;')
    assert cli.main(['workload', '--file', str(path)]) == 2
    assert 'JSON array' in capsys.readouterr().err


def test_run_emits_terminal_trace_for_unsafe_input(tmp_path, monkeypatch, capsys):
    adapter = ScriptedAdapter([])
    monkeypatch.setattr(cli, 'IoTDBTreeAdapter', lambda settings: adapter)
    assert cli.main(['--sqlite', str(tmp_path/'single.sqlite'), '--log-level', 'CRITICAL',
                     'run', '--sql', 'DELETE FROM root.test']) == 1
    result = json.loads(capsys.readouterr().out)
    assert result['trace']['events'][-1]['state'] == 'REJECTED'
    assert not adapter.calls
