"""Durable delayed feedback: atomic queue acknowledgement + model publication."""
from contextlib import contextmanager
import json
from pathlib import Path
import sqlite3
from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

from .episodes import BatchFeedback, BatchPolicy
from .model import AdmissionError
from ..schemas.common import ensure_utc, utc_now
from ..schemas.learning import digest


class InferenceRestoreReceipt(BaseModel):
    """Audit of an offline inference selection, never a training rewind."""
    model_config = ConfigDict(extra='forbid', frozen=True)
    schema_version: Literal[1] = 1
    generation: int = Field(ge=1)
    training_revision: int = Field(ge=0)
    previous_revision: int = Field(ge=0)
    selected_revision: int = Field(ge=0)
    fingerprint: str
    restored_at: datetime

    @field_validator('restored_at')
    @classmethod
    def utc(cls, value):
        return ensure_utc(value)


class DelayedBatchStore:
    def __init__(self, path, initial=None):
        self.path = Path(path)
        with self.connection() as c:
            c.executescript('''
                CREATE TABLE IF NOT EXISTS feedback(
                    position INTEGER PRIMARY KEY AUTOINCREMENT, batch_id TEXT UNIQUE NOT NULL,
                    checksum TEXT NOT NULL, body TEXT NOT NULL, enqueued_at TEXT NOT NULL,
                    status TEXT NOT NULL CHECK(status IN ('PENDING','APPLIED','REJECTED')), reason TEXT);
                CREATE TABLE IF NOT EXISTS feedback_ids(identity TEXT PRIMARY KEY, batch_id TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS versions(revision INTEGER PRIMARY KEY, checksum TEXT NOT NULL, body TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS head(singleton INTEGER PRIMARY KEY CHECK(singleton=1), revision INTEGER NOT NULL, frozen_at TEXT);
                CREATE TABLE IF NOT EXISTS inference_restores(
                    generation INTEGER PRIMARY KEY, body TEXT NOT NULL, checksum TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS inference_head(
                    singleton INTEGER PRIMARY KEY CHECK(singleton=1), generation INTEGER NOT NULL);
            ''')
            if c.execute('SELECT 1 FROM head').fetchone() is None:
                if initial is None: raise ValueError('initial batch model required')
                body = initial.snapshot()
                c.execute('INSERT INTO versions VALUES(0,?,?)', (digest(body), json.dumps(body)))
                c.execute('INSERT INTO head VALUES(1,0,NULL)')
            elif initial is not None:
                raise ValueError('refusing to overwrite existing model store')

    @contextmanager
    def connection(self):
        c = sqlite3.connect(self.path, timeout=5)
        try:
            c.execute('PRAGMA journal_mode=WAL')
            c.execute('PRAGMA synchronous=FULL')
            yield c
            c.commit()
        except BaseException:
            c.rollback()
            raise
        finally:
            c.close()

    @staticmethod
    def _load_version(c, revision):
        row = c.execute('SELECT checksum,body FROM versions WHERE revision=?', (revision,)).fetchone()
        if row is None:
            raise ValueError('unknown batch model revision')
        checksum, raw = row
        body = json.loads(raw)
        if digest(body) != checksum: raise ValueError('batch model checksum mismatch')
        return BatchPolicy.restore(body)

    @staticmethod
    def _load(c):
        revision, frozen = c.execute('SELECT revision,frozen_at FROM head WHERE singleton=1').fetchone()
        return DelayedBatchStore._load_version(c, revision), revision, frozen

    @staticmethod
    def _compatible_history(target, head):
        if target.scope != head.scope:
            raise ValueError('inference restore scope mismatch')
        for length, model in target.models.items():
            candidate, current = model.snapshot(), head.models[length].snapshot()
            if (candidate.config != current.config or candidate.training_until != current.training_until
                    or candidate.baseline_arm != current.baseline_arm):
                raise ValueError('inference restore configuration mismatch')
        for name in ('owners', 'evidence'):
            if any(getattr(head, name).get(k) != v for k, v in getattr(target, name).items()):
                raise ValueError('inference restore is not training history')

    @classmethod
    def _inference_state(cls, c):
        head, revision, frozen = cls._load(c)
        if frozen is None:
            raise ValueError('inference restore requires frozen training')
        if c.execute("SELECT 1 FROM feedback WHERE status='PENDING'").fetchone():
            raise ValueError('pending feedback prevents inference restore')
        row = c.execute('SELECT generation FROM inference_head WHERE singleton=1').fetchone()
        if row is None:
            return head, revision, 0, revision
        generation = row[0]
        saved = c.execute('SELECT body,checksum FROM inference_restores WHERE generation=?',
                          (generation,)).fetchone()
        if saved is None:
            raise ValueError('missing inference restore audit')
        body = json.loads(saved[0])
        if digest(body) != saved[1]:
            raise ValueError('inference restore audit checksum mismatch')
        receipt = InferenceRestoreReceipt.model_validate(body)
        if (receipt.generation != generation or receipt.training_revision != revision
                or receipt.selected_revision > revision):
            raise ValueError('inference restore head mismatch')
        selected = cls._load_version(c, receipt.selected_revision)
        cls._compatible_history(selected, head)
        if selected.fingerprint != receipt.fingerprint:
            raise ValueError('inference model fingerprint mismatch')
        return selected, receipt.selected_revision, generation, revision

    def inference_current(self):
        """Return (model, selected revision, CAS generation, training revision).

        Only explicit offline inference consumers use this pointer. current(),
        freeze() and the study replay continue to refer to the training head.
        """
        with self.connection() as c:
            c.execute('BEGIN')
            return self._inference_state(c)

    def restore_inference(self, revision, *, expected_training_revision, expected_generation):
        """Atomically select a historical model without replaying applied feedback.

        Training must already be frozen and drained. No unfreeze/resume is
        offered: restoring numerical weights is not rewinding the attribution
        ledger. A later call can select the original frozen head again.
        """
        for value in (revision, expected_training_revision, expected_generation):
            if type(value) is not int or value < 0:
                raise ValueError('revision and generation must be nonnegative integers')
        with self.connection() as c:
            c.execute('BEGIN IMMEDIATE')
            _, previous, generation, training_revision = self._inference_state(c)
            if (training_revision, generation) != (expected_training_revision, expected_generation):
                raise ValueError('stale inference restore token')
            if revision > training_revision:
                raise ValueError('cannot restore a future revision')
            head = self._load_version(c, training_revision)
            target = self._load_version(c, revision)
            self._compatible_history(target, head)
            receipt = InferenceRestoreReceipt(generation=generation+1,
                training_revision=training_revision, previous_revision=previous,
                selected_revision=revision, fingerprint=target.fingerprint, restored_at=utc_now())
            body = receipt.model_dump(mode='json')
            self._record_restore(c, receipt.generation, body)
            c.execute('INSERT INTO inference_head VALUES(1,?) ON CONFLICT(singleton) '
                      'DO UPDATE SET generation=excluded.generation', (receipt.generation,))
            return receipt

    @staticmethod
    def _record_restore(c, generation, body):
        c.execute('INSERT INTO inference_restores VALUES(?,?,?)',
                  (generation, json.dumps(body), digest(body)))

    def current(self):
        with self.connection() as c:
            c.execute('BEGIN')
            return self._load(c)

    def enqueue(self, feedback):
        f = BatchFeedback.model_validate(feedback.model_dump(round_trip=True))
        at = utc_now()
        if f.contract.phase != 'TRAIN' or f.closed_at > at:
            raise ValueError('only completed training batches can enqueue')
        body = f.model_dump(mode='json', round_trip=True)
        checksum = digest(body)
        with self.connection() as c:
            c.execute('BEGIN IMMEDIATE')
            model, _, frozen = self._load(c)
            if frozen: raise ValueError('training frozen; feedback refused')
            model._scope(f.contract)
            old = c.execute('SELECT checksum FROM feedback WHERE batch_id=?', (f.contract.batch_id,)).fetchone()
            if old:
                if old[0] != checksum: raise ValueError('conflicting repeated batch')
                return False
            for identifier in f.identities:
                c.execute('INSERT INTO feedback_ids VALUES(?,?)', (identifier, f.contract.batch_id))
            c.execute('INSERT INTO feedback(batch_id,checksum,body,enqueued_at,status) VALUES(?,?,?,?,?)',
                (f.contract.batch_id, checksum, json.dumps(body), at.isoformat(), 'PENDING'))
        return True

    def drain_one(self, registry):
        with self.connection() as c:
            c.execute('BEGIN IMMEDIATE')
            model, revision, frozen = self._load(c)
            row = c.execute("SELECT batch_id,checksum,body FROM feedback WHERE status='PENDING' ORDER BY position LIMIT 1").fetchone()
            if row is None: return None
            if frozen: raise ValueError('pending training after freeze')
            batch_id, checksum, raw = row
            body = json.loads(raw)
            if digest(body) != checksum: raise ValueError('feedback checksum mismatch')
            f = BatchFeedback.model_validate(body)
            try:
                changed = model.update(f, registry)
            except AdmissionError as exc:
                c.execute("UPDATE feedback SET status='REJECTED',reason=? WHERE batch_id=?", (str(exc), batch_id))
                return {'batch_id': batch_id, 'status': 'REJECTED', 'reason': str(exc), 'revision': revision}
            if not changed: raise ValueError('queue/model ledger disagree')
            new = model.snapshot()
            self._publish(c, revision+1, new)
            c.execute('UPDATE head SET revision=? WHERE singleton=1', (revision+1,))
            c.execute("UPDATE feedback SET status='APPLIED' WHERE batch_id=?", (batch_id,))
            return {'batch_id': batch_id, 'status': 'APPLIED', 'revision': revision+1,
                'fingerprint': model.fingerprint, 'reward': f.reward, 'service_ms': f.service_ms}

    @staticmethod
    def _publish(connection, revision, body):
        connection.execute('INSERT INTO versions VALUES(?,?,?)', (revision, digest(body), json.dumps(body)))

    def freeze(self):
        with self.connection() as c:
            c.execute('BEGIN IMMEDIATE')
            model, revision, frozen = self._load(c)
            if c.execute("SELECT 1 FROM feedback WHERE status='PENDING'").fetchone():
                raise ValueError('pending feedback must drain before freeze')
            at = frozen or utc_now().isoformat()
            c.execute('UPDATE head SET frozen_at=? WHERE singleton=1', (at,))
            return {'frozen_at': at, 'revision': revision, 'fingerprint': model.fingerprint,
                'snapshot': model.snapshot()}
