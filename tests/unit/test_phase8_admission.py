"""Offline replay of the user's resource rejection; no IoTDB calls."""
from datetime import datetime, timedelta
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from tsdb_optimizer.benchmark import runtime
from tsdb_optimizer.benchmark.journal import read_events
from tsdb_optimizer.benchmark.runner import execute
from tsdb_optimizer.schemas.monitoring import SystemSample
from test_phase8_benchmark import SyntheticEnvironment, SyntheticMonitor, config


@pytest.fixture
def rejection():
    path = Path(__file__).resolve().parents[1]/'fixtures/monitoring/phase8_resource_rejection_20261103.json'
    return json.loads(path.read_text())


def test_real_rejection_reports_both_sources_values_and_limits(monkeypatch, rejection):
    sample = SystemSample.model_validate(rejection['sample'])
    at = datetime.fromisoformat(rejection['at'])
    monkeypatch.setattr(runtime, 'utc_now', lambda: at)
    calls = []
    def once():
        calls.append(True)
        return sample
    with pytest.raises(runtime.ResourceAdmissionError) as caught:
        runtime.admit(SimpleNamespace(sample_once=once))
    error = caught.value
    assert 'client_host.cpu_percent=97.009 > 80' in str(error)
    assert 'db_host.cpu_percent=100 > 80' in str(error)
    assert [v.model_dump() for v in error.violations] == [
        {'source': 'client_host', 'metric': 'cpu_percent', 'observed': 97.009, 'limit': 80., 'reason': 'LIMIT_EXCEEDED'},
        {'source': 'db_host', 'metric': 'cpu_percent', 'observed': 100., 'limit': 80., 'reason': 'LIMIT_EXCEEDED'},
    ]
    assert error.sample is sample and error.at == at
    assert calls == [True]  # no resampling until a passing observation appears


@pytest.mark.parametrize('source', ['client_host', 'db_host'])
@pytest.mark.parametrize('metric,limit', [('cpu_percent', 80), ('memory_percent', 90), ('disk_usage_percent', 90)])
def test_exact_safety_boundaries_unchanged(source, metric, limit):
    sample = SyntheticMonitor().latest_sample()
    setattr(getattr(sample, source), metric, limit)
    monitor = SimpleNamespace(sample_once=lambda: sample)
    assert runtime.admit(monitor)[0] is sample
    setattr(getattr(sample, source), metric, limit+.001)
    with pytest.raises(runtime.ResourceAdmissionError) as caught:
        runtime.admit(monitor)
    violation, = caught.value.violations
    assert (violation.source, violation.metric, violation.limit, violation.reason) == (source, metric, limit, 'LIMIT_EXCEEDED')


@pytest.mark.parametrize('source', ['client_host', 'db_host'])
def test_missing_metric_is_unknown_and_not_a_measured_excess(source):
    sample = SyntheticMonitor().latest_sample()
    getattr(sample, source).cpu_percent = None
    with pytest.raises(runtime.ResourceAdmissionError) as caught:
        runtime.admit(SimpleNamespace(sample_once=lambda: sample))
    violation, = caught.value.violations
    assert violation.reason == 'MISSING_METRIC' and violation.observed is None
    assert f'{source}.cpu_percent=UNKNOWN' in str(caught.value)


@pytest.mark.parametrize('kind', ['stale', 'future', 'invalid', 'wrong_source'])
def test_unusable_snapshot_remains_rejected(kind):
    sample = SyntheticMonitor().latest_sample()
    snapshot = sample.db_host
    if kind == 'stale':
        snapshot.captured_at -= timedelta(seconds=1)
    elif kind == 'future':
        snapshot.captured_at += timedelta(seconds=1)
    elif kind == 'invalid':
        snapshot.valid = False
    else:
        snapshot.source_type = sample.client_host.source_type
    with pytest.raises(runtime.ResourceAdmissionError) as caught:
        runtime.admit(SimpleNamespace(sample_once=lambda: sample))
    violation, = caught.value.violations
    assert (violation.source, violation.metric, violation.reason) == ('db_host', 'snapshot', 'UNUSABLE_SNAPSHOT')


def test_runner_preserves_violation_and_request_position_without_dispatch(tmp_path):
    cfg = config(groups=['A1'])
    env = SyntheticEnvironment(cfg)
    original = env.monitor.latest_sample
    def unsafe():
        sample = original()
        sample.db_host.cpu_percent = 81
        return sample
    env.monitor.latest_sample = unsafe
    with pytest.raises(runtime.ResourceAdmissionError):
        execute(cfg, tmp_path/'rejected', env)
    events = list(read_events(tmp_path/'rejected/journal.jsonl'))
    event, = [e['body'] for e in events if e['kind'] == 'resource_rejected']
    assert event['request_role'] == 'WARMUP' and event['request_index'] == 0
    assert event['violations'] == [{'source': 'db_host', 'metric': 'cpu_percent', 'observed': 81.,
                                    'limit': 80., 'reason': 'LIMIT_EXCEEDED'}]
    assert event['sample']['db_host']['cpu_percent'] == 81
    assert sum(len(session.executed) for session in env.sessions) == 2  # references only
    assert not any(e['kind'] in ('request', 'training_update', 'complete') for e in events)
    assert events[-1]['kind'] == 'aborted'


def test_second_real_rejection_latest_snapshot_stops_before_dispatch(monkeypatch):
    from tsdb_optimizer.agents import PerformanceMonitoringAgent
    from tsdb_optimizer.agents.performance_monitor import agent as performance_monitor

    path = Path(__file__).resolve().parents[1]/'fixtures/monitoring/phase8_resource_rejection_20261103_211545.json'
    rejection = json.loads(path.read_text())
    sample = SystemSample.model_validate(rejection['sample'])
    at = datetime.fromisoformat(rejection['at'])
    monkeypatch.setattr(runtime, 'utc_now', lambda: at)
    monkeypatch.setattr(performance_monitor, 'utc_now', lambda: at)
    reads = []
    def latest():
        reads.append(True)
        return sample
    monitor = PerformanceMonitoringAgent(monitor=SimpleNamespace(
        is_running=True, latest_sample=latest), snapshot_mode='latest', latest_max_age_ms=200)
    # Only fields accessed before admission are provided. Any SQL/service/cache
    # access fails this test instead of silently running a synthetic query.
    lane = object.__new__(runtime.Lane)
    lane.monitor = monitor
    with pytest.raises(runtime.ResourceAdmissionError) as caught:
        lane.request(0, 'WARMUP')
    error = caught.value
    assert str(error) == rejection['reason']
    assert [v.model_dump(mode='json') for v in error.violations] == rejection['violations']
    assert error.sample.model_dump(mode='json', round_trip=True) == rejection['sample']
    assert (error.request_role, error.request_index) == ('WARMUP', 0)
    assert reads == [True]  # never search for a later low-CPU sample to pass
    assert sample.client_host.cpu_percent == 42.718
    assert sample.db_host.cpu_percent == 82.809
