"""Identity and source-boundary tests use explicit test doubles, not live evidence."""
from types import SimpleNamespace
from pathlib import Path
import pytest
from tsdb_optimizer.monitoring import local_db
from tsdb_optimizer.schemas.monitoring import SystemSourceType


def setup_identity(monkeypatch, *, host_class=True, port=6667, changed_ns=False):
    process=SimpleNamespace(cmdline=lambda: ['org.apache.iotdb.db.service.DataNode'] if host_class else ['proxy'],
        create_time=lambda: 123.0,
        net_connections=lambda **kw: [SimpleNamespace(status='LISTEN',laddr=SimpleNamespace(ip='::',port=port))])
    monkeypatch.setattr(local_db.psutil,'Process',lambda pid: process)
    monkeypatch.setattr(Path,'readlink',lambda p: Path('different' if changed_ns and '/self/' not in str(p) else 'same'))
    return process


@pytest.mark.parametrize('kwargs', [{'host_class':False},{'port':6668},{'changed_ns':True}])
def test_identity_rejects_proxy_wrong_port_or_namespace(monkeypatch, kwargs):
    setup_identity(monkeypatch,**kwargs)
    with pytest.raises(ValueError): local_db.verify_local_db('127.0.0.1',6667,123)


def test_remote_endpoint_rejected():
    with pytest.raises(ValueError,match='loopback'):
        local_db.verify_local_db('192.0.2.1',6667,123)


def test_restart_invalidates_sampler(monkeypatch):
    process=setup_identity(monkeypatch)
    sampler=local_db.VerifiedLocalDbHostSampler(host='127.0.0.1',port=6667,pid=123)
    process.create_time=lambda: 456.0
    with pytest.raises(ValueError,match='identity changed'): sampler.sample()


def test_db_sample_separate_source_and_unknown_query_count(monkeypatch):
    setup_identity(monkeypatch)
    from collections import namedtuple
    counter=iter([namedtuple('CpuTimes','user idle')(23,77),namedtuple('CpuTimes','user idle')(46,154)])
    fake=SimpleNamespace(cpu_times=lambda: next(counter),
        virtual_memory=lambda: SimpleNamespace(percent=24.0),
        disk_usage=lambda p: SimpleNamespace(percent=25.0),
        disk_io_counters=lambda: SimpleNamespace(read_bytes=1,write_bytes=2))
    sampler=local_db.VerifiedLocalDbHostSampler(host='127.0.0.1',port=6667,pid=123,psutil_module=fake)
    assert not sampler.sample().valid
    result=sampler.sample()
    assert result.valid and result.source_type is SystemSourceType.DB_HOST
    assert result.cpu_percent==23.0 and result.active_queries is None
    assert result.unavailable_fields['active_queries'].value=='NO_DB_METRICS_SOURCE'
    assert local_db.PsutilSystemSampler._source_type is SystemSourceType.CLIENT_HOST
