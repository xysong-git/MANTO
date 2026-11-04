"""Local SQLite only; cache schema readiness, never execution results."""
import sqlite3

import pytest

from tsdb_optimizer.storage import SQLiteStore
from tsdb_optimizer.errors import TSDBOptimizerError


def trace_connections(store, monkeypatch):
    statements = []
    original = store._open_connection
    def opened():
        connection = original()
        connection.set_trace_callback(statements.append)
        return connection
    monkeypatch.setattr(store, '_open_connection', opened)
    return statements


def test_repeated_initialization_checks_versions_without_migration(tmp_path, monkeypatch):
    store = SQLiteStore(tmp_path/'db.sqlite', wal_full=True)
    store.initialize()
    statements = trace_connections(store, monkeypatch)
    store.initialize()
    assert [s.upper() for s in statements] == ['PRAGMA USER_VERSION', 'PRAGMA SCHEMA_VERSION']


def test_external_schema_change_repairs_missing_table(tmp_path):
    store = SQLiteStore(tmp_path/'db.sqlite')
    store.initialize()
    with sqlite3.connect(store.path) as c:
        c.execute('DROP TABLE workflow_traces')
    store.initialize()
    with sqlite3.connect(store.path) as c:
        assert c.execute('SELECT count(*) FROM workflow_traces').fetchone()[0] == 0


def test_file_replacement_does_not_inherit_ready_state(tmp_path, monkeypatch):
    path = tmp_path/'db.sqlite'
    store = SQLiteStore(path)
    store.initialize()
    replacement = tmp_path/'replacement.sqlite'
    other = SQLiteStore(replacement)
    other.initialize()
    replacement.replace(path)
    statements = trace_connections(store, monkeypatch)
    store.initialize()
    assert any('CREATE TABLE' in s.upper() for s in statements)


def test_memory_close_and_fresh_owner_reinitialize(tmp_path):
    store = SQLiteStore(':memory:')
    store.initialize(); store.initialize(); store.close()
    store.initialize()
    assert store.count('queries') == 0
    store.close()


def test_failed_initialization_does_not_mark_ready(tmp_path, monkeypatch):
    store = SQLiteStore(tmp_path/'db.sqlite')
    original = store._schema_identity
    calls = []
    def fail_after_migration(connection):
        calls.append(1)
        if len(calls) == 2:
            raise sqlite3.OperationalError('TEST_ONLY post-migration failure')
        return original(connection)
    monkeypatch.setattr(store, '_schema_identity', fail_after_migration)
    with pytest.raises(sqlite3.OperationalError):store.initialize()
    assert store._initialized_schema is None
    monkeypatch.setattr(store, '_schema_identity', original)
    store.initialize()
    assert store.count('queries') == 0


def test_user_version_change_is_still_rejected(tmp_path):
    store = SQLiteStore(tmp_path/'db.sqlite');store.initialize()
    with sqlite3.connect(store.path) as c:c.execute('PRAGMA user_version=99')
    with pytest.raises(TSDBOptimizerError):store.initialize()
