"""Structured experience retrieval and atomic L1 model versions."""
from __future__ import annotations

from collections import OrderedDict, namedtuple
import hashlib
import sys
from threading import RLock
from typing import NamedTuple
import math
import statistics

from ..schemas.common import utc_now
from ..schemas.learning import Experience, L1Model, SampleStatus, StrategyStatistics
from ..storage import SQLiteStore


class _StatisticsFact(NamedTuple):
    strategy_id: str
    strategy_version: str
    sample_status: SampleStatus
    quality_version: int
    resource_policy_hash: str | None
    client_wall_ms: float
    success: bool
    reward: float | None
    execution_id: str


def _read_fact(body):
    e = Experience.model_validate_json(body)
    return _StatisticsFact(e.strategy_id, e.strategy_version, e.sample_status,
        e.quality_version, e.resource_policy_hash, e.record.client_wall_ms,
        e.record.success, e.reward, e.record.execution_id)


_CacheInfo = namedtuple('CacheInfo', 'hits misses maxsize currsize')


class _FactCache:
    """Content-addressed validated facts, without retaining full JSON keys.

    A full group scan exceeding the old 128-body LRU caused zero hits on the
    next scan. Compact immutable facts allow a larger bounded working set.
    Neither revisions nor execution IDs alone authorize reuse: every body is
    hashed, including quarantine/corrections made outside this process.
    The byte budget accounts for key/fact values plus container allowance; it
    is not a claim about allocator RSS. Oversized facts are never retained.
    """
    def __init__(self, maxsize=4096, max_bytes=4*1024*1024):
        if maxsize < 1 or max_bytes < 1:
            raise ValueError('positive cache bounds required')
        self.maxsize = maxsize
        self.max_bytes = max_bytes
        self._entries = OrderedDict()
        self._lock = RLock()
        self._bytes = self._hits = self._misses = 0

    def __call__(self, body):
        key = hashlib.sha256(body.encode('utf-8')).digest()
        with self._lock:
            existing = self._entries.get(key)
            if existing is not None:
                self._entries.move_to_end(key)
                self._hits += 1
                return existing[0]
            self._misses += 1
        # Do not cache exceptions or hold the lock during Pydantic validation.
        fact = _read_fact(body)
        size = sys.getsizeof(key)+sys.getsizeof(fact)+sum(sys.getsizeof(v) for v in fact)+256
        if size > self.max_bytes:
            return fact
        with self._lock:
            previous = self._entries.pop(key, None)
            if previous is not None:
                self._bytes -= previous[1]
            self._entries[key] = (fact, size)
            self._bytes += size
            while len(self._entries) > self.maxsize or self._bytes > self.max_bytes:
                _, (_, removed) = self._entries.popitem(last=False)
                self._bytes -= removed
        return fact

    def cache_clear(self):
        with self._lock:
            self._entries.clear()
            self._bytes = self._hits = self._misses = 0

    def cache_info(self):
        with self._lock:
            return _CacheInfo(self._hits, self._misses, self.maxsize, len(self._entries))

    @property
    def retained_bytes(self):
        with self._lock:
            return self._bytes


_cached_fact = _FactCache()


def _statistics_fact(body):
    return _cached_fact(body) if len(body) <= 262144 else _read_fact(body)


class ExperienceRepository:
    def __init__(self, store: SQLiteStore):
        self.store = store

    @staticmethod
    def _revision(connection, group_key: str) -> int:
        row = connection.execute('SELECT revision FROM learning_group_state WHERE group_key=?',
                                 (group_key,)).fetchone()
        return row[0] if row else 0

    @staticmethod
    def _verify_execution(connection, experience: Experience) -> None:
        row = connection.execute('SELECT * FROM executions WHERE execution_id=?',
                                 (experience.execution_id,)).fetchone()
        decision = connection.execute('SELECT * FROM decisions WHERE decision_id=?',
                                      (experience.decision_id,)).fetchone()
        if row is None or decision is None:
            raise ValueError('experience requires persisted query/decision/execution evidence')
        r = experience.record
        expected = (experience.query_id, experience.decision_id, experience.strategy_id,
                    'SUCCESS' if r.success else 'ERROR', r.client_wall_ms, r.timed_out,
                    experience.is_fallback, r.started_at.isoformat(), r.finished_at.isoformat())
        actual = (row['query_id'], row['decision_id'], row['strategy_id'], row['status'],
                  row['client_wall_ms'], bool(row['timed_out']), bool(row['is_fallback']),
                  row['started_at'], row['finished_at'])
        if actual != expected or decision['query_id'] != experience.query_id:
            raise ValueError('experience differs from persisted execution evidence')
        if decision['model_version'] != experience.decision_model_version:
            raise ValueError('model version differs from persisted decision')
        if row['candidate_sql_sha256'] != experience.query_hash:
            # Current certified strategies do not rewrite. Future rewritten
            # strategies need an independently verified result contract here.
            raise ValueError('candidate/original SQL differs; unsupported learning contract')
        if not experience.is_fallback and (decision['selected_strategy_id'], decision['strategy_version']) != (
                experience.strategy_id, experience.strategy_version):
            raise ValueError('strategy version differs from persisted decision')
        if (row['fetch_mode'] != experience.context.fetch_mode
                or bool(row['served_from_cache']) != (experience.context.result_source == 'CACHE_HIT')):
            raise ValueError('fetch/cache source differs from persisted execution')
        if row['cache_epoch'] is not None and row['cache_epoch'] != experience.context.dataset_epoch:
            raise ValueError('cache epoch differs from learning dataset epoch')
        if (experience.context.result_rows is not None
                and row['row_count'] != experience.context.result_rows):
            if experience.sample_status == SampleStatus.VALID:
                raise ValueError('VALID context differs from actual result size')
        if experience.reward is not None and experience.reward != -math.log1p(r.client_wall_ms / 1000.0):
            raise ValueError('reward does not match measured milliseconds')

    @staticmethod
    def _quarantine(connection, experience: Experience) -> None:
        connection.execute('''INSERT OR IGNORE INTO strategy_quarantine
            VALUES (?,?,?,?,?)''', (experience.strategy_id, experience.strategy_version,
            'RUNTIME_RESULT_MISMATCH', experience.execution_id, utc_now().isoformat()))
        rows = connection.execute('''SELECT experience_id, body_json FROM experiences
             WHERE strategy_id=? AND strategy_version=?''',
             (experience.strategy_id, experience.strategy_version)).fetchall()
        for row in rows:
            item = Experience.model_validate_json(row['body_json'])
            item = item.model_copy(update={'sample_status': SampleStatus.INVALID, 'reward': None,
                'quality_reasons': list(dict.fromkeys([*item.quality_reasons, 'STRATEGY_QUARANTINED']))})
            connection.execute('''UPDATE experiences SET sample_status='INVALID', reward=NULL,
                body_json=? WHERE experience_id=?''', (item.model_dump_json(round_trip=True), row['experience_id']))

    def record(self, experience: Experience) -> bool:
        """Return False for an exact retry; reject conflicting re-use of an ID."""
        with self.store._connect() as connection:
            connection.execute('BEGIN IMMEDIATE')
            self._verify_execution(connection, experience)
            old = connection.execute('SELECT body_json FROM experiences WHERE execution_id=?',
                                     (experience.execution_id,)).fetchone()
            mismatch = experience.record.runtime_result_check == 'MISMATCHED'
            affected_groups = {experience.group_key}
            if mismatch:
                affected_groups.update(row[0] for row in connection.execute(
                    'SELECT DISTINCT group_key FROM experiences WHERE strategy_id=? AND strategy_version=?',
                    (experience.strategy_id, experience.strategy_version)))
                self._quarantine(connection, experience)
            quarantined = connection.execute('''SELECT 1 FROM strategy_quarantine
                WHERE strategy_id=? AND strategy_version=?''',
                (experience.strategy_id, experience.strategy_version)).fetchone()
            if quarantined:
                experience = experience.model_copy(update={
                    'sample_status': SampleStatus.INVALID, 'reward': None,
                    'quality_reasons': list(dict.fromkeys([*experience.quality_reasons, 'STRATEGY_QUARANTINED']))})
            if old is not None:
                previous = Experience.model_validate_json(old[0])
                ignored = {'created_at', 'experience_id'}
                if previous.model_dump(exclude=ignored) == experience.model_dump(exclude=ignored):
                    return False
                if not mismatch:
                    raise ValueError('conflicting experience for the same execution')
                if (previous.query_id, previous.context, previous.strategy_version) != (
                        experience.query_id, experience.context, experience.strategy_version):
                    raise ValueError('mismatch evidence changed execution context')
            if old is not None:
                experience = experience.model_copy(update={
                    'experience_id': previous.experience_id, 'created_at': previous.created_at})
            values = (experience.experience_id, experience.query_id, experience.decision_id,
                      experience.execution_id, experience.strategy_id, experience.strategy_version,
                      experience.template_key, experience.feature_key, experience.context.key,
                      experience.group_key, experience.sample_status.value, experience.reward,
                      experience.model_dump_json(round_trip=True), experience.created_at.isoformat())
            if old is None:
                connection.execute('INSERT INTO experiences VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)', values)
            else:
                # A later independent differential check may revoke a formerly
                # good sample; it must not leave its old reward in any model.
                connection.execute('''UPDATE experiences SET sample_status='INVALID', reward=NULL,
                   body_json=? WHERE execution_id=?''', (experience.model_dump_json(round_trip=True), experience.execution_id))
            for group in affected_groups:
                connection.execute('''INSERT INTO learning_group_state VALUES (?,1)
                    ON CONFLICT(group_key) DO UPDATE SET revision=revision+1''', (group,))
            connection.execute('UPDATE learning_state SET revision=revision+1 WHERE singleton=1')
            return old is None

    def get(self, execution_id: str) -> Experience | None:
        with self.store._connect() as connection:
            row = connection.execute('SELECT body_json FROM experiences WHERE execution_id=?',
                                     (execution_id,)).fetchone()
        return Experience.model_validate_json(row[0]) if row else None

    def retrieve(self, *, template_key: str, feature_key: str, context_key: str,
                 strategy_id: str | None = None, strategy_version: str | None = None) -> list[Experience]:
        sql = 'SELECT body_json FROM experiences WHERE template_key=? AND feature_key=? AND context_key=?'
        args = [template_key, feature_key, context_key]
        for name, value in [('strategy_id', strategy_id), ('strategy_version', strategy_version)]:
            if value is not None:
                sql += f' AND {name}=?'
                args.append(value)
        with self.store._connect() as connection:
            rows = connection.execute(sql + ' ORDER BY created_at,execution_id', args).fetchall()
        return [Experience.model_validate_json(row[0]) for row in rows]

    def is_quarantined(self, strategy_id: str, version: str) -> bool:
        with self.store._connect() as connection:
            return connection.execute('SELECT 1 FROM strategy_quarantine WHERE strategy_id=? AND strategy_version=?',
                                      (strategy_id, version)).fetchone() is not None

    def build_model(self, group_key: str, *, min_samples: int = 3,
                    min_improvement: float = 0.05,
                    resource_policy_hash: str | None = None) -> L1Model:
        if resource_policy_hash is None:
            from ..decision.resource_policy import load_default_resource_policy
            from ..schemas.learning import digest
            resource_policy_hash = digest(load_default_resource_policy().model_dump(mode='json'))
        with self.store._connect() as connection:
            connection.execute('BEGIN')
            revision = self._revision(connection, group_key)
            rows = connection.execute('SELECT body_json FROM experiences WHERE group_key=?', (group_key,)).fetchall()
        groups: dict[tuple[str, str], list[_StatisticsFact]] = {}
        for row in rows:
            item = _statistics_fact(row[0])
            groups.setdefault((item.strategy_id, item.strategy_version), []).append(item)
        result = []
        for (strategy, version), items in sorted(groups.items()):
            valid = [e for e in items if e.sample_status == SampleStatus.VALID
                     and e.quality_version == 2 and e.resource_policy_hash == resource_policy_hash]
            latencies = [e.client_wall_ms for e in valid]
            rate = sum(e.success for e in items) / len(items)
            result.append(StrategyStatistics(
                strategy_id=strategy, strategy_version=version, observed_count=len(items),
                valid_count=len(valid), success_rate=rate,
                mean_latency_ms=statistics.mean(latencies) if valid else None,
                median_latency_ms=statistics.median(latencies) if valid else None,
                variance_ms2=statistics.pvariance(latencies) if valid else None,
                mean_reward=statistics.mean(e.reward for e in valid) if valid else None,
                eligible=len(valid) >= min_samples and rate == 1.0))
        return L1Model(group_key=group_key, data_revision=revision, min_samples=min_samples,
                       quality_version=2, resource_policy_hash=resource_policy_hash,
                       min_improvement=min_improvement, statistics=tuple(result),
                       sample_execution_ids=tuple(sorted(e.execution_id for items in groups.values() for e in items)))

    def publish(self, model: L1Model) -> None:
        if model.quality_version != 2 or model.resource_policy_hash is None:
            raise ValueError('model lacks current quality and resource policy evidence')
        expected = self.build_model(model.group_key, min_samples=model.min_samples,
                                    min_improvement=model.min_improvement,
                                    resource_policy_hash=model.resource_policy_hash)
        if expected.data_revision != model.data_revision or (expected.statistics != model.statistics or expected.sample_execution_ids != model.sample_execution_ids):
            raise ValueError('model statistics differ from stored experience')
        with self.store._connect() as connection:
            connection.execute('BEGIN IMMEDIATE')
            if self._revision(connection, model.group_key) != model.data_revision:
                raise ValueError('experience revision changed; rebuild model before publishing')
            connection.execute('INSERT INTO learning_models VALUES (?,?,?,?,?)',
                (model.model_version, model.group_key, model.data_revision,
                 model.model_dump_json(round_trip=True), model.created_at.isoformat()))
            connection.execute('''INSERT INTO learning_heads VALUES (?,?)
                ON CONFLICT(group_key) DO UPDATE SET model_version=excluded.model_version''',
                (model.group_key, model.model_version))

    def current_model(self, group_key: str) -> L1Model | None:
        with self.store._connect() as connection:
            connection.execute('BEGIN')
            row = connection.execute('''SELECT m.body_json FROM learning_models m
                JOIN learning_heads h ON h.model_version=m.model_version WHERE h.group_key=?''',
                (group_key,)).fetchone()
            if row is None:
                return None
            model = L1Model.model_validate_json(row[0])
            if model.quality_version != 2 or model.resource_policy_hash is None:
                return None
            return model if model.data_revision == self._revision(connection, group_key) else None
