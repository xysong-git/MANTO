from datetime import timedelta
from types import SimpleNamespace
import pytest
from tsdb_optimizer.agents import PerformanceMonitoringAgent
from tsdb_optimizer.monitoring.system import SystemMonitor
from tsdb_optimizer.schemas.common import utc_now
from test_learning import sample


def test_latest_mode_never_resamples_or_relabels_stale_snapshot():
    saved=sample();saved.client_host.captured_at=utc_now()-timedelta(seconds=30)
    def forbidden():raise AssertionError('request must not poll hardware')
    monitor=SimpleNamespace(is_running=True,latest_sample=lambda:saved,sample_once=forbidden)
    agent=PerformanceMonitoringAgent(monitor=monitor,snapshot_mode='latest')
    got=agent.sample_once()
    assert got==saved and got is not saved
    assert not got.client_host.is_fresh()
    got.client_host.cpu_percent=99
    assert saved.client_host.cpu_percent==10
    monitor.is_running=False
    with pytest.raises(RuntimeError):agent.sample_once()


def test_missing_background_snapshot_fails_explicitly():
    monitor=SimpleNamespace(is_running=True,latest_sample=lambda:None)
    with pytest.raises(RuntimeError):PerformanceMonitoringAgent(monitor=monitor,snapshot_mode='latest').sample_once()


def test_timed_out_stop_retains_thread_identity():
    thread=SimpleNamespace(is_alive=lambda:True,join=lambda **kw:None)
    monitor=SystemMonitor();monitor._thread=thread
    monitor.stop(timeout=0)
    assert monitor.is_running and monitor._thread is thread


def test_round_load_stops_sampler_before_fork_and_restarts_after(monkeypatch):
    import runpy
    from contextlib import contextmanager
    scope=runpy.run_path('scripts/phase7_controlled_e2e.py')['round_load']
    calls=[]
    class Monitor:
        is_monitoring=True
        def stop(self):calls.append('stop');self.is_monitoring=False
        def start(self):calls.append('start');self.is_monitoring=True
    monitor=Monitor()
    @contextmanager
    def load(enabled, *, target_percent):
        assert target_percent==60
        assert not monitor.is_monitoring
        calls.append('fork');yield 'stress';calls.append('cleanup')
    monkeypatch.setitem(scope.__wrapped__.__globals__,'CpuLoad',load)
    with scope(True,monitor) as stress:
        assert stress=='stress' and monitor.is_monitoring
    assert calls==['stop','fork','start','cleanup']


def test_round_load_refuses_to_fork_when_sampler_does_not_stop():
    import runpy
    scope=runpy.run_path('scripts/phase7_controlled_e2e.py')['round_load']
    monitor=SimpleNamespace(stop=lambda:None,is_monitoring=True)
    with pytest.raises(RuntimeError,match='cannot fork'):
        with scope(True,monitor):raise AssertionError('unreachable')


def test_preparation_wait_rejects_stale_snapshot_without_query():
    import runpy
    wait=runpy.run_path('scripts/phase7_controlled_e2e.py')['wait_for_monitor']
    saved=sample();saved.client_host.captured_at=utc_now()-timedelta(seconds=5)
    monitor=SimpleNamespace(latest_sample=lambda:saved)
    with pytest.raises(RuntimeError,match='readiness'):wait(monitor,timeout_s=0)
    saved=sample()
    assert wait(monitor,timeout_s=0)['sample']['client_host']['snapshot_id']==saved.client_host.snapshot_id


def test_slow_db_probe_precedes_client_capture():
    calls=[];saved=sample()
    def db():calls.append('db');return saved.db_host
    def client():calls.append('client');return saved.client_host
    monitor=SystemMonitor(db_host_sampler=SimpleNamespace(sample=db),client_sampler=SimpleNamespace(sample=client))
    got=monitor.sample_once()
    assert calls==['db','client']
    assert got.client_host==saved.client_host and got.db_host==saved.db_host


def test_periodic_sampling_accounts_for_capture_duration(monkeypatch):
    from tsdb_optimizer.monitoring import system
    now=[0.];delays=[]
    monkeypatch.setattr(system.time,'monotonic',lambda:now[0])
    monitor=SystemMonitor(interval_s=.2)
    class Stop:
        def wait(self,delay):
            delays.append(delay);now[0]+=delay
            return len(delays)==3
    monitor._stop_event=Stop()
    monitor.sample_once=lambda:now.__setitem__(0,now[0]+.15)
    monitor._run()
    assert delays==pytest.approx([.2,.05,.05])


def test_bounded_latest_wait_does_not_renew_timestamp_or_resample(monkeypatch):
    import tsdb_optimizer.agents.performance_monitor as module
    now=[0.];sleeps=[];stale=sample();fresh=sample()
    stale.db_host.captured_at=utc_now()-timedelta(seconds=1)
    def latest():return stale if not sleeps else fresh
    def sleep(delay):sleeps.append(delay);now[0]+=delay
    monkeypatch.setattr(module.time,'monotonic',lambda:now[0])
    monkeypatch.setattr(module.time,'sleep',sleep)
    agent=PerformanceMonitoringAgent(monitor=SimpleNamespace(is_running=True,latest_sample=latest),
        snapshot_mode='latest',latest_max_age_ms=250,latest_wait_timeout_s=.5)
    got=agent.sample_once()
    assert sleeps==[.01] and got.db_host.captured_at==fresh.db_host.captured_at
    agent.monitor.latest_sample=lambda:stale
    with pytest.raises(RuntimeError,match='deadline'):agent.sample_once()
    assert now[0]==pytest.approx(.51)


@pytest.mark.parametrize('kwargs',[{'latest_max_age_ms':0},{'latest_wait_timeout_s':2},
    {'latest_wait_timeout_s':float('nan')},{'latest_wait_timeout_s':.5}])
def test_latest_wait_configuration_is_explicit_and_bounded(kwargs):
    with pytest.raises(ValueError):PerformanceMonitoringAgent(snapshot_mode='latest',**kwargs)
