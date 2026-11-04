from contextlib import contextmanager
from types import SimpleNamespace

import pytest

from tsdb_optimizer.experiments.cpu_load import CpuLoad
from tsdb_optimizer.benchmark.runner import LiveEnvironment


@pytest.mark.parametrize('cpu,memory,expected', [(81., 60., 'cpu_percent=81.0 > 80'),
    (50., 91., 'memory_percent=91.0 > 90'), (81., 91., 'memory_percent=91.0 > 90')])
def test_safety_stop_exposes_actual_trigger(monkeypatch, cpu, memory, expected):
    import tsdb_optimizer.experiments.cpu_load as module
    load = CpuLoad(False)
    load.start = module.time.monotonic()
    load.end = load.start + 30
    monkeypatch.setattr(module.psutil, 'cpu_percent', lambda _: cpu)
    monkeypatch.setattr(module.psutil, 'virtual_memory', lambda: SimpleNamespace(percent=memory))
    load.control()  # no worker processes or active load
    assert load.stop.is_set()
    assert len(load.samples) == 1
    with pytest.raises(RuntimeError, match=expected):
        load.check()


def test_load_failure_retains_samples_after_cleanup(monkeypatch):
    import tsdb_optimizer.benchmark.runner as module
    events = []
    cleaned = []
    failure = RuntimeError('CO_TENANT_SAFETY_STOP')
    @contextmanager
    def fake_load(*args, **kwargs):
        try:
            yield SimpleNamespace(samples=[{'cpu_percent': 81., 'memory_percent': 60.}],
                                  error=str(failure))
        finally:
            cleaned.append(True)
    monkeypatch.setattr(module, 'CpuLoad', fake_load)
    monkeypatch.setattr(module.time, 'sleep', lambda _: None)
    env = object.__new__(LiveEnvironment)
    env.config = SimpleNamespace(load_driver='cpu_cotenant')
    def emit(kind, body):
        assert cleaned
        events.append((kind, body))
    with pytest.raises(RuntimeError) as caught:
        with env.load('MEDIUM', emit):
            raise failure
    assert caught.value is failure
    assert len(events) == 1
    assert events[0][0] == 'load_control'
    assert events[0][1]['samples'][0]['cpu_percent'] == 81.
    assert events[0][1]['error'] == str(failure)
