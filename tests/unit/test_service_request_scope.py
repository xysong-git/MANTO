"""Service infrastructure parity; synthetic SDK, no IoTDB access."""
from contextlib import contextmanager
from unittest.mock import patch
import sqlite3
import pytest
import sqlglot
from tsdb_optimizer import sql_guard
from tsdb_optimizer.request_memo import bucket
from tsdb_optimizer.benchmark.runtime import Lane
from tsdb_optimizer.benchmark.protocol import schedule
from test_phase8_benchmark import config, SyntheticEnvironment


@pytest.fixture
def lane(tmp_path):
    cfg = config(groups=['E1'], cache_conditions=['APPLICATION_WARM'])
    env = SyntheticEnvironment(cfg)
    a = next(a for a in schedule(cfg) if a.phase=='HOLDOUT')
    query = cfg.workloads[0].holdout
    reference = env.reference(query, None, None)
    obj = Lane(config=cfg, allocation=a, lane=0, query=query, reference=reference,
        settings=env.settings, monitor=env.monitor, output=tmp_path, bank=None,
        adapter_factory=env.adapter_factory)
    obj.prepare(lambda row: None)
    yield obj, env
    obj.close()


def test_service_reuses_facts_connection_and_does_not_reuse_between_requests(lane):
    obj, env = lane
    before = sum(len(s.executed) for s in env.sessions)
    with patch.object(sql_guard,'parse',wraps=sql_guard.parse) as guard, \
         patch.object(sqlglot,'parse',wraps=sqlglot.parse) as features, \
         patch.object(obj.store,'_open_connection',wraps=obj.store._open_connection) as opened:
        first = obj.request(0)
        assert (guard.call_count, features.call_count, opened.call_count)==(2,1,1)
        second = obj.request(1)
        assert (guard.call_count, features.call_count, opened.call_count)==(4,2,2)
    assert first.correctness==second.correctness=='MATCHED'
    assert first.run.decision.selected_strategy_id==second.run.decision.selected_strategy_id=='S1_REPEAT_CACHE'
    assert sum(len(s.executed) for s in env.sessions)==before
    assert first.run.decision.query_id!=second.run.decision.query_id
    assert bucket('readonly',obj.query.resolved()) is None
    assert obj.store._request_connection is None
    assert obj.store.get_execution(first.run.execution.final_execution_id)
    assert obj.store.get_execution_report(second.run.decision.decision_id)


def test_service_body_failure_closes_session_and_clears_facts(lane, monkeypatch):
    obj, env = lane
    connections=[]
    original=obj.store._open_connection
    def opened():
        c=original();connections.append(c);return c
    monkeypatch.setattr(obj.store,'_open_connection',opened)
    def failed(*args,**kwargs):raise RuntimeError('synthetic decision failure')
    monkeypatch.setattr(obj.engine,'decide',failed)
    with pytest.raises(RuntimeError,match='synthetic decision failure'):
        obj.service.run(obj.query.resolved())
    assert obj.store._request_connection is None
    assert bucket('readonly',obj.query.resolved()) is None
    for c in connections:
        with pytest.raises(sqlite3.ProgrammingError):c.execute('SELECT 1')


def test_service_cleanup_failure_is_explicit_without_reexecution(lane, monkeypatch):
    obj, env = lane
    original=obj.store.request_session
    @contextmanager
    def failed_close():
        with original():yield
        raise OSError('synthetic cleanup error')
    monkeypatch.setattr(obj.store,'request_session',failed_close)
    before=sum(len(s.executed) for s in env.sessions)
    run=obj.service.run(obj.query.resolved(), mode='adaptive')
    assert run.execution.final_status.value=='SUCCESS'
    assert 'STORE_SESSION_CLOSE_FAILED: OSError' in run.execution.warnings
    assert sum(len(s.executed) for s in env.sessions)==before
    assert obj.store._request_connection is None


def test_outer_request_timer_includes_session_close(lane, monkeypatch):
    import time
    from tsdb_optimizer.schemas.common import utc_now
    obj,_=lane
    original=obj.store.request_session
    closed=[]
    @contextmanager
    def delayed_close():
        with original():yield
        time.sleep(.02)
        closed.append(utc_now())
    monkeypatch.setattr(obj.store,'request_session',delayed_close)
    row=obj.request(0)
    assert closed and row.started_at<closed[0]<=row.finished_at
    assert row.end_to_end_ms>=20


def test_query_and_decision_committed_before_dispatch_in_wal_session(lane, monkeypatch):
    obj,_=lane
    original=obj.service.executor.execute
    seen=[]
    def check_committed(decision, **kwargs):
        with sqlite3.connect(obj.store.path) as reader:
            assert reader.execute('SELECT count(*) FROM queries WHERE query_id=?',
                                  (decision.query_id,)).fetchone()[0]==1
            assert reader.execute('SELECT count(*) FROM decisions WHERE decision_id=?',
                                  (decision.decision_id,)).fetchone()[0]==1
        seen.append(decision.decision_id)
        return original(decision, **kwargs)
    monkeypatch.setattr(obj.service.executor,'execute',check_committed)
    row=obj.request(0)
    assert row.success and seen==[row.run.decision.decision_id]
