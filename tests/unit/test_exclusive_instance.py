from types import SimpleNamespace as N
import pytest
from tsdb_optimizer.experiments.exclusive_instance import inspect_clients


def connection(pid, local, remote):
    return N(pid=pid,status='ESTABLISHED',laddr=N(ip=local[0],port=local[1]),raddr=N(ip=remote[0],port=remote[1]))


def probe(rows):
    listener=N(pid=10,status='LISTEN',laddr=N(ip='127.0.0.1',port=6667),raddr=None)
    return inspect_clients(10,6667,own_pid=20,
        psutil_module=N(CONN_ESTABLISHED='ESTABLISHED',CONN_LISTEN='LISTEN',net_connections=lambda **kw:[listener,*rows]))


def test_own_session_allowed_but_idle_foreign_session_blocks():
    server=connection(10,('127.0.0.1',6667),('127.0.0.1',1234))
    own=connection(20,('127.0.0.1',1234),('127.0.0.1',6667))
    assert probe([server,own]).passed
    own.pid=21
    result=probe([server,own])
    assert not result.passed and result.foreign_pids==[21]


def test_remote_and_unattributed_clients_fail_closed():
    for ip in ('127.0.0.1','10.1.2.3'):
        result=probe([connection(10,('127.0.0.1',6667),(ip,1234))])
        assert not result.passed and result.unknown_connections==1


def test_inventory_failure_propagates():
    def fail(**kw):raise PermissionError('TEST_ONLY')
    with pytest.raises(PermissionError):inspect_clients(10,6667,
        psutil_module=N(net_connections=fail))


def test_java_ipv4_mapped_ipv6_matches_python_ipv4_client():
    server=connection(10,('::ffff:127.0.0.1',6667),('::ffff:127.0.0.1',1234))
    own=connection(20,('127.0.0.1',1234),('127.0.0.1',6667))
    assert probe([server,own]).passed
    own.pid=21
    assert probe([server,own]).foreign_pids==[21]
