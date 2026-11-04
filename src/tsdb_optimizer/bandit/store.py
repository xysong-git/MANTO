"""Separate offline artifact SQLite store: immutable snapshots + atomic CAS head."""
import json
import sqlite3
from contextlib import contextmanager
from pathlib import Path

from ..schemas.learning import digest
from .model import Snapshot


def model_key(snapshot):
    return digest([snapshot.domain,snapshot.partition,snapshot.baseline_arm,
                   sorted(snapshot.arms),snapshot.config.model_dump(mode='json')])


class OfflineModelStore:
    def __init__(self, path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.connection() as c:
            c.executescript('''
              CREATE TABLE IF NOT EXISTS bandit_models(
                version TEXT PRIMARY KEY, model_key TEXT NOT NULL, checksum TEXT NOT NULL, body TEXT NOT NULL);
              CREATE TABLE IF NOT EXISTS bandit_heads(
                model_key TEXT PRIMARY KEY, version TEXT NOT NULL REFERENCES bandit_models(version));
            ''')

    @contextmanager
    def connection(self):
        c = sqlite3.connect(self.path, timeout=5)
        c.execute('PRAGMA foreign_keys=ON')
        try:
            yield c
            c.commit()
        except BaseException:
            c.rollback()
            raise
        finally:
            c.close()

    def publish(self, snapshot, *, expected_head=None):
        snapshot = Snapshot.model_validate(snapshot.model_dump())
        body = snapshot.model_dump(mode='json')
        key = model_key(snapshot)
        with self.connection() as c:
            c.execute('BEGIN IMMEDIATE')
            head = c.execute('SELECT version FROM bandit_heads WHERE model_key=?',(key,)).fetchone()
            if (head[0] if head else None) != expected_head:
                raise ValueError('stale head; publication refused')
            c.execute('INSERT INTO bandit_models VALUES(?,?,?,?)',
                      (snapshot.model_version,key,digest(body),json.dumps(body,allow_nan=False)))
            c.execute('INSERT INTO bandit_heads VALUES(?,?) ON CONFLICT(model_key) DO UPDATE SET version=excluded.version',
                      (key,snapshot.model_version))
        return key

    @staticmethod
    def _load(c, version):
        row = c.execute('SELECT model_key,checksum,body FROM bandit_models WHERE version=?',(version,)).fetchone()
        if row is None: raise KeyError(version)
        body = json.loads(row[2])
        if digest(body) != row[1]: raise ValueError('model checksum mismatch')
        snapshot = Snapshot.model_validate(body)
        if model_key(snapshot) != row[0]: raise ValueError('model identity mismatch')
        return snapshot

    def current(self, key):
        with self.connection() as c:
            c.execute('BEGIN')
            row = c.execute('SELECT version FROM bandit_heads WHERE model_key=?',(key,)).fetchone()
            return self._load(c,row[0]) if row else None

    def rollback(self, key, version, *, expected_head):
        with self.connection() as c:
            c.execute('BEGIN IMMEDIATE')
            target = self._load(c,version)
            if model_key(target) != key: raise ValueError('rollback target is a different model')
            cursor = c.execute('UPDATE bandit_heads SET version=? WHERE model_key=? AND version=?',
                               (version,key,expected_head))
            if cursor.rowcount != 1: raise ValueError('stale head; rollback refused')
