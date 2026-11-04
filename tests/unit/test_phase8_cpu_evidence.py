"""Synthetic counters test evidence identity and unchanged CPU computation."""
from collections import namedtuple
from types import SimpleNamespace as N

import psutil
import pytest

from tsdb_optimizer.benchmark.cpu_evidence import CounterProbe, EvidenceSampler
from tsdb_optimizer.monitoring.system import PsutilSystemSampler
from test_system_monitor import FakePsutil, FakeCounters


def make_sampler():
    module = FakePsutil(counters=[FakeCounters(0, 0)], cpu_values=[10, 99, 20])
    module.Process = lambda pid: N(create_time=lambda: 123., cpu_times=lambda: N(user=2., system=1.))
    probe = CounterProbe({'benchmark': 10, 'datanode': 20}, module)
    return EvidenceSampler(PsutilSystemSampler(psutil_module=probe), probe)


def test_exact_counter_pair_and_snapshot_identity_survive_later_sample():
    sampler = make_sampler()
    first = sampler.sample()
    rejected = sampler.sample()
    sampler.sample()
    evidence = sampler.evidence_for(rejected)
    assert first.cpu_percent is None
    assert rejected.cpu_percent == 99
    assert evidence['snapshot_id'] == rejected.snapshot_id
    assert evidence['previous']['counters'] == {'user': 10., 'idle': 90.}
    assert evidence['current']['counters'] == {'user': 109., 'idle': 91.}
    assert evidence['current']['processes']['datanode']['created_at_s'] == 123
    assert evidence['current']['monotonic_ns'] >= evidence['previous']['monotonic_ns']
    assert sampler.evidence_for(first)['previous'] is None


def test_process_access_failure_is_unknown_not_zero_or_host_substitution():
    sampler = make_sampler()
    def denied(pid):
        raise psutil.AccessDenied(pid)
    sampler.probe.module.Process = denied
    sampler.sample()
    snapshot = sampler.sample()
    assert snapshot.cpu_percent == 99
    process = sampler.evidence_for(snapshot)['current']['processes']['datanode']
    assert process['error'] == 'AccessDenied'
    assert process['user_s'] is None and process['system_s'] is None


def test_host_counter_failure_resets_pair_and_propagates_to_sampler():
    sampler = make_sampler()
    sampler.sample()
    original = sampler.probe.module.cpu_times
    def broken():
        raise OSError('synthetic read error')
    sampler.probe.module.cpu_times = broken
    failed = sampler.sample()
    assert failed.cpu_percent is None and not failed.valid
    assert sampler.evidence_for(failed)['capture_error'] == 'OSError'
    sampler.probe.module.cpu_times = original
    recovered = sampler.sample()
    assert recovered.cpu_percent is None
    assert sampler.evidence_for(recovered)['previous'] is None


def test_history_is_bounded_and_expired_evidence_is_unavailable():
    sampler = make_sampler()
    first = sampler.sample()
    for _ in range(32):
        sampler.sample()
    assert sampler.evidence_for(first) is None
    assert len(sampler._history) == 32


def test_real_095009_rejection_is_not_hidden(monkeypatch):
    import json
    from pathlib import Path
    from datetime import datetime
    from tsdb_optimizer.benchmark import runtime
    from tsdb_optimizer.schemas.monitoring import SystemSample
    body = json.loads(Path('tests/fixtures/monitoring/phase8_resource_rejection_20261104_095009.json').read_text())
    sample = SystemSample.model_validate(body['sample'])
    monkeypatch.setattr(runtime, 'utc_now', lambda: datetime.fromisoformat(body['at']))
    with pytest.raises(runtime.ResourceAdmissionError) as caught:
        runtime.admit(N(sample_once=lambda: sample))
    assert str(caught.value) == body['reason']
    assert [v.model_dump(mode='json') for v in caught.value.violations] == body['violations']


def test_runner_attaches_evidence_for_the_rejected_sample(tmp_path):
    from test_phase8_benchmark import config, SyntheticEnvironment
    from tsdb_optimizer.benchmark import runtime
    from tsdb_optimizer.benchmark.runner import execute
    from tsdb_optimizer.benchmark.journal import read_events
    cfg = config(groups=['A1'])
    env = SyntheticEnvironment(cfg)
    original = env.monitor.latest_sample
    def unsafe():
        sample = original()
        sample.db_host.cpu_percent = 99
        return sample
    env.monitor.latest_sample = unsafe
    seen = []
    def evidence(sample):
        seen.append(sample.db_host.snapshot_id)
        return {'db_host': {'snapshot_id': sample.db_host.snapshot_id, 'TEST_ONLY': True}}
    env.cpu_evidence = evidence
    with pytest.raises(runtime.ResourceAdmissionError):
        execute(cfg, tmp_path/'run', env)
    event, = [e['body'] for e in read_events(tmp_path/'run/journal.jsonl') if e['kind']=='resource_rejected']
    assert seen == [event['sample']['db_host']['snapshot_id']]
    assert event['cpu_counter_evidence']['db_host']['snapshot_id'] == seen[0]
    assert sum(len(s.executed) for s in env.sessions) == 2  # references only
