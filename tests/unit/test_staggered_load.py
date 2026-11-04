import time
import os
import errno
import pytest
from tsdb_optimizer.experiments.cpu_load import CpuLoad, pulse


def _probe_parent_descriptor(sender, descriptor, identity):
    try:
        stat=os.fstat(descriptor)
        inherited=(stat.st_dev,stat.st_ino)==identity
    except OSError as exc:
        if exc.errno!=errno.EBADF:raise
        inherited=False
    sender.send(inherited)
    sender.close()


def test_uniform_phases_bound_simultaneous_workers():
    for duty in (0,.1,.5,.93,1):
        for tick in range(200):
            active=sum(pulse(tick*.00037,0,i,38,duty)[0] for i in range(38))
            assert abs(active-38*duty)<=1
            assert all(0<pulse(tick*.00037,0,i,38,duty)[1]<=.02 for i in range(38))


def test_new_profile_caps_workers_and_keeps_legacy_default(monkeypatch):
    import tsdb_optimizer.experiments.cpu_load as module
    monkeypatch.setattr(module.psutil,'cpu_count',lambda:64)
    monkeypatch.setattr(module.psutil.Process,'cpu_affinity',lambda self:list(range(64)))
    assert CpuLoad(False).count==48
    assert CpuLoad(False,target_percent=55,profile='staggered-v1').count==38
    with pytest.raises(ValueError,match='budget'):CpuLoad(False,target_percent=65,profile='staggered-v1')
    with pytest.raises(ValueError,match='profile'):CpuLoad(False,profile='unknown')


@pytest.mark.parametrize('profile',['staggered-v1','staggered-spawn-v2'])
def test_staggered_workers_expire_and_join(profile):
    load=CpuLoad(True,duration_s=.15,target_percent=55,profile=profile)
    load.count=1  # bounded lifecycle test; not evidence of target load
    with load:time.sleep(.2)
    assert all(not p.is_alive() for p in load.processes)
    with pytest.raises(RuntimeError,match='expired'):load.check()


def test_spawn_profile_does_not_inherit_parent_database_handle(tmp_path,monkeypatch):
    import tsdb_optimizer.experiments.cpu_load as module
    monkeypatch.setattr(module.psutil,'cpu_count',lambda:64)
    monkeypatch.setattr(module.psutil.Process,'cpu_affinity',lambda self:list(range(64)))
    load=CpuLoad(False,target_percent=55,profile='staggered-spawn-v2')
    # A real open file stands in for a parent DB descriptor: fork would retain
    # it even when non-inheritable; spawn must retain only explicitly passed FDs.
    with (tmp_path/'parent-database-handle').open('wb') as parent:
        receiver,sender=load.ctx.Pipe(duplex=False)
        stat=os.fstat(parent.fileno())
        child=load.ctx.Process(target=_probe_parent_descriptor,
            args=(sender,parent.fileno(),(stat.st_dev,stat.st_ino)))
        child.start();sender.close()
        try:
            assert receiver.poll(10), 'worker did not report descriptor isolation'
            assert receiver.recv() is False
        finally:
            receiver.close();child.join(timeout=2)
            if child.is_alive():child.kill();child.join(timeout=2)
        assert child.exitcode==0
