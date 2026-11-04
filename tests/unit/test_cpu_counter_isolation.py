"""Independent raw counter baselines; synthetic cumulative CPU times."""
from collections import namedtuple
from types import SimpleNamespace as N
from tsdb_optimizer.monitoring.system import PsutilSystemSampler
from tsdb_optimizer.schemas.monitoring import MetricUnavailableReason


def test_interleaved_samplers_do_not_consume_each_others_baseline():
    T=namedtuple('Cpu','user idle iowait guest guest_nice')
    values=iter([T(10,90,0,0,0),T(10,90,0,0,0),T(30,170,0,0,0),T(30,170,0,0,0)])
    fake=N(cpu_times=lambda:next(values),cpu_percent=lambda **k:(_ for _ in ()).throw(AssertionError('global baseline forbidden')))
    a=PsutilSystemSampler(hostname='a',psutil_module=fake)
    b=PsutilSystemSampler(hostname='b',psutil_module=fake)
    assert a._sample_cpu({},[]) is None
    assert b._sample_cpu({},[]) is None
    assert a._sample_cpu({},[])==20
    assert b._sample_cpu({},[])==20


def test_guest_and_iowait_not_double_counted_and_reset_is_unknown():
    T=namedtuple('Cpu','user idle iowait guest guest_nice')
    values=iter([T(0,0,0,0,0),T(20,60,20,10,0),T(20,60,20,10,0),T(1,1,1,0,0)])
    s=PsutilSystemSampler(hostname='a',psutil_module=N(cpu_times=lambda:next(values)))
    assert s._sample_cpu({},[]) is None
    assert s._sample_cpu({},[])==20  # total100 (guest already in user), busy20
    missing={};assert s._sample_cpu(missing,[]) is None
    assert missing['cpu_percent']==MetricUnavailableReason.BASELINE_NOT_ESTABLISHED
    missing={};assert s._sample_cpu(missing,[]) is None
    assert missing['cpu_percent']==MetricUnavailableReason.COUNTER_RESET
