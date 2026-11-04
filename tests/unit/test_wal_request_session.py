import sqlite3
from contextlib import contextmanager
from types import SimpleNamespace
import pytest
from tsdb_optimizer.storage import SQLiteStore
from tsdb_optimizer.request_memo import store_session


def test_wal_full_reuses_connection_but_commits_each_operation(tmp_path):
    path=tmp_path/'wal.sqlite';store=SQLiteStore(path,wal_full=True);store.initialize()
    with store.request_session():
        with store._connect() as first:
            assert first.execute('PRAGMA journal_mode').fetchone()[0]=='wal'
            assert first.execute('PRAGMA synchronous').fetchone()[0]==2
            first.execute('CREATE TABLE durable(value INTEGER)')
            first.execute('INSERT INTO durable VALUES(1)')
        with sqlite3.connect(path) as outsider:
            assert outsider.execute('SELECT value FROM durable').fetchone()[0]==1
        with store._connect() as second:assert first is second
        with pytest.raises(RuntimeError):
            with store._connect() as connection:
                connection.execute('INSERT INTO durable VALUES(2)')
                raise RuntimeError('rollback')
        with store._connect() as connection:
            assert connection.execute('SELECT count(*) FROM durable').fetchone()[0]==1
    assert store._request_connection is None
    with pytest.raises(sqlite3.ProgrammingError):first.execute('SELECT 1')
    with sqlite3.connect(path) as connection:
        assert connection.execute('PRAGMA integrity_check').fetchone()[0]=='ok'
        assert connection.execute('SELECT count(*) FROM durable').fetchone()[0]==1


def test_nested_session_rejected_and_exception_releases_connection(tmp_path):
    store=SQLiteStore(tmp_path/'test.sqlite',wal_full=True)
    with pytest.raises(RuntimeError,match='serial-owner'):
        with store.request_session():
            with store.request_session():pass
    assert store._request_connection is None
    with store.request_session():
        with store._connect() as connection:assert connection.execute('SELECT 1').fetchone()[0]==1


def test_cleanup_failure_preserves_successful_result_and_is_explicit():
    @contextmanager
    def session():
        yield
        raise OSError('test cleanup failure')
    result=SimpleNamespace(trace=SimpleNamespace(warnings=[]))
    class Owner:
        store=SimpleNamespace(request_session=session)
        @store_session
        def run(self):return result
    assert Owner().run() is result
    assert result.trace.warnings==['STORE_SESSION_CLOSE_FAILED: OSError']
    assert result.request_timing.store_close_ms >= 0


def test_store_session_timings_cover_open_body_and_close(monkeypatch):
    from tsdb_optimizer import request_memo
    ticks = iter([0, 2_000_000, 7_000_000, 10_000_000])
    monkeypatch.setattr(request_memo.time, 'perf_counter_ns', lambda: next(ticks))
    @contextmanager
    def session():
        yield
    class Owner:
        store = SimpleNamespace(request_session=session)
        @store_session
        def run(self):
            return SimpleNamespace(trace=SimpleNamespace(warnings=[]))
    timing = Owner().run().request_timing
    assert timing.model_dump() == dict(store_open_ms=2, body_ms=5, store_close_ms=3, total_ms=10)


def test_store_session_body_failure_propagates():
    @contextmanager
    def session():
        yield
    class Owner:
        store = SimpleNamespace(request_session=session)
        @store_session
        def run(self):
            raise ValueError('body failed')
    with pytest.raises(ValueError, match='body failed'):
        Owner().run()
