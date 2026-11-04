"""Synthetic SDK/host fixtures only. Never contact IoTDB or claim performance."""
from contextlib import contextmanager
from datetime import timedelta
import json
from pathlib import Path
import shutil

import pytest
from pydantic import ValidationError

from tsdb_optimizer.adapters.iotdb.tree_adapter import IoTDBTreeAdapter
from tsdb_optimizer.benchmark.analysis import analyze, export, interval, quantile, summarize, throughput_interval, plot
from tsdb_optimizer.benchmark.journal import Journal, read_events
from tsdb_optimizer.benchmark.policy import PolicyBank, masked_vector
from tsdb_optimizer.benchmark.protocol import BenchmarkConfig, Query, checksum, schedule
from tsdb_optimizer.benchmark.results import RequestRecord, result_matches
from tsdb_optimizer.benchmark.runner import execute
from tsdb_optimizer.bandit.model import AdmissionError
from tsdb_optimizer.config import Settings
from tsdb_optimizer.schemas.common import QueryRequest, utc_now
from tsdb_optimizer.sql_guard import sql_sha256
from tsdb_optimizer.agents import QueryAnalysisAgent
from test_tree_adapter import FakeDataset, FakeRow, FakeSession
from test_learning import sample

ROOT = Path(__file__).resolve().parents[2]


def config(**updates):
    body = json.loads((ROOT/'benchmarks/workloads/small_count.json').read_text())
    body.update({'train_blocks': 1, 'test_blocks': 2, 'warmup': 1, 'requests_per_lane': 3, **updates})
    body['analysis'].update(bootstrap_replicates=200)
    return BenchmarkConfig.model_validate(body)


class SyntheticMonitor:
    is_running = True

    def latest_sample(self):
        s = sample()
        for source in ('client_host', 'db_host'):
            item = getattr(s, source)
            item.stale_after_ms = 500
            item.cpu_measurement = 'cpu-times-instance-v1'
        return s


class SyntheticEnvironment:
    evidence_domain = 'SYNTHETIC_TEST_ONLY'
    certificate = {'status': 'SYNTHETIC_TEST_ONLY'}
    identity = {'source': 'SYNTHETIC_TEST_ONLY'}

    def __init__(self, config, *, fail=False):
        self.config = config
        self.settings = Settings(iotdb_password='test-only')
        self.monitor = SyntheticMonitor()
        self.sessions = []
        self.fail = fail

    def start(self):
        self.monitor.is_running = True

    def close(self):
        self.monitor.is_running = False

    def check(self, emit, budget):
        # No synthetic occupancy is mislabeled as a real Linux observation.
        emit('instance_occupancy', {'evidence_domain': 'SYNTHETIC_TEST_ONLY', 'passed': True})

    @contextmanager
    def load(self, target, emit):
        class NoLoad:
            def check(self):
                return None
        yield NoLoad()

    def adapter_factory(self, settings):
        queries = [q.resolved() for w in self.config.workloads for q in (w.train, w.holdout)]
        session = FakeSession({sql: lambda: FakeDataset(['count(root.test.d.s)'], ['2'], [FakeRow(0, [123])]) for sql in queries})
        self.sessions.append(session)
        if self.fail and len(self.sessions) > 2:
            session.execute_error = RuntimeError('synthetic query failure')
        return IoTDBTreeAdapter(settings, session_factory=lambda _: session)

    def reference(self, query, output, emit):
        adapter = self.adapter_factory(self.settings)
        try:
            return adapter.execute_readonly(QueryRequest(sql=query.resolved()), preview_rows=1000)
        finally:
            adapter.close()


@pytest.fixture(scope='module')
def completed(tmp_path_factory):
    root = tmp_path_factory.mktemp('phase8_synthetic')
    cfg = config()
    env = SyntheticEnvironment(cfg)
    execute(cfg, root/'run', env)
    return root/'run', cfg, env


def rewrite(source, target, mutate):
    shutil.copytree(source, target)
    events = list(read_events(target/'journal.jsonl'))
    mutate(events)
    (target/'journal.jsonl').unlink()
    journal = Journal(target/'journal.jsonl')
    for event in events:
        journal.emit(event['kind'], event['body'])
    journal.close()


def test_same_config_same_plan_and_randomized_complete_blocks():
    cfg = config()
    assert schedule(cfg) == schedule(BenchmarkConfig.model_validate_json(cfg.model_dump_json()))
    rows = schedule(cfg)
    assert len({r.allocation_id for r in rows}) == len(rows)
    assert all(r.phase == 'TRAIN' for r in rows[:4])
    assert {r.group for r in rows[4:]} == set(cfg.groups)
    other = BenchmarkConfig.model_validate({**cfg.model_dump(), 'seed': cfg.seed+1})
    assert schedule(other) != rows


def test_abba_is_explicit_pairwise_crossover():
    with pytest.raises(ValidationError, match='two-group'):
        config(order='ABBA')
    cfg = config(groups=['E0', 'E4'], order='ABBA')
    holdout = [r for r in schedule(cfg) if r.phase == 'HOLDOUT']
    for i in range(0, len(holdout), 4):
        group = holdout[i:i+4]
        assert group[0].group == group[3].group
        assert group[1].group == group[2].group
        assert group[0].group != group[1].group
        assert sorted(r.repeat for r in group) == [0, 0, 1, 1]


@pytest.mark.parametrize('change', [
    {'concurrency': [0]}, {'concurrency': [1, 1]}, {'groups': []}, {'loads': ['MEDIUM']},
    {'cache_conditions': ['COLD']}, {'min_samples': 2}, {'requests_per_lane': 0},
])
def test_protocol_rejects_mislabeling_and_unsafe_bounds(change):
    with pytest.raises(ValidationError):
        config(**change)


def test_pinned_sql_and_train_test_isolation():
    cfg = config()
    q = cfg.workloads[0].train.model_dump()
    q['parameters']['start_ms'] += 1
    with pytest.raises(ValidationError, match='checksum'):
        Query.model_validate(q)
    body = cfg.model_dump()
    body['workloads'][0]['holdout'] = body['workloads'][0]['train']
    with pytest.raises(ValidationError, match='disjoint'):
        BenchmarkConfig.model_validate(body)
    with pytest.raises((ValidationError, ValueError)):
        Query(sql='DELETE FROM root.x', resolved_sha256=sql_sha256('DELETE FROM root.x'), expected_rows=0)


def test_all_groups_complete_and_no_hidden_double_execution(completed):
    directory, cfg, env = completed
    report, rows = analyze(directory)
    assert report['execution_complete']
    assert not report['analysis_qualified']  # Small synthetic test cannot certify results.
    assert report['evidence_domain'] == 'SYNTHETIC_TEST_ONLY'
    assert report['counts']['training_applied'] > 0
    assert report['server_execution_ms'] is None
    assert all(row.correctness == 'MATCHED' for row in rows)
    # Exactly the reference requests plus the recorded physical dispatches.
    dispatches = sum(sum(a.execution.request_dispatched for a in r.run.execution.attempts) for r in rows)
    assert sum(len(s.executed) for s in env.sessions) == 2+dispatches
    assert all(s.close_called for s in env.sessions)
    assert {r.allocation.group for r in rows if r.role == 'MEASURED'} >= set(cfg.groups)
    assert all(not f.model_updated for r in rows if r.workflow for f in r.workflow.feedback)
    assert any(r.run.execution.attempts[-1].execution.served_from_cache for r in rows)
    assert all(r.inference is None for r in rows if r.allocation.group in ('E3', 'A2'))


def test_same_config_rerun_has_identical_artifact_structure(completed, tmp_path):
    directory, cfg, _ = completed
    execute(cfg, tmp_path/'second', SyntheticEnvironment(cfg))
    one = export(directory, tmp_path/'analysis_one')
    two = export(tmp_path/'second', tmp_path/'analysis_two')
    assert one['config_sha256'] == two['config_sha256']
    assert one['counts'] == two['counts']
    assert (directory/'plan.json').read_bytes() == (tmp_path/'second/plan.json').read_bytes()
    assert (tmp_path/'analysis_one/structure.json').read_bytes() == (tmp_path/'analysis_two/structure.json').read_bytes()
    assert sorted(p.name for p in (tmp_path/'analysis_one').iterdir()) == sorted(p.name for p in (tmp_path/'analysis_two').iterdir())
    assert (tmp_path/'analysis_two/latency.png').stat().st_size > 1000
    assert all(g['metrics']['end_to_end_ms']['mean_ci'] is None for g in two['groups'])


def test_concurrent_lanes_have_independent_sessions_caches_and_stores(tmp_path):
    cfg = config(concurrency=[2], groups=['E0', 'E1'], cache_conditions=['APPLICATION_WARM'])
    env = SyntheticEnvironment(cfg)
    execute(cfg, tmp_path/'run', env)
    report, rows = analyze(tmp_path/'run')
    assert report['execution_complete']
    assert {r.lane for r in rows} == {0, 1}
    assert len(list((tmp_path/'run/lanes').glob('*.sqlite'))) == len(schedule(cfg))*2
    assert all(s.close_called for s in env.sessions)
    assert all(g['throughput']['successful_requests_per_second'] > 0 for g in report['groups'])


@pytest.mark.parametrize('attack', ['duplicate', 'missing', 'sql', 'correctness', 'update', 'freeze',
                                  'load_label', 'missing_inference', 'post_terminal'])
def test_independent_analyzer_rejects_rechained_tampering(completed, tmp_path, attack):
    source, _, _ = completed
    def mutate(events):
        i = next(i for i, e in enumerate(events) if e['kind'] == 'request' and e['body']['role'] == 'MEASURED')
        if attack == 'duplicate':
            events.insert(i, events[i])
        elif attack == 'missing':
            del events[i]
        elif attack == 'sql':
            events[i]['body']['sql_sha256'] = 'a'*64
        elif attack == 'correctness':
            events[i]['body']['run']['execution']['attempts'][-1]['execution']['rows_preview'] = [[999]]
        elif attack == 'update':
            next(e for e in events if e['kind'] == 'training_update')['body']['model_fingerprint'] = 'a'*64
        elif attack == 'freeze':
            next(e for e in events if e['kind'] == 'frozen_model')['body']['fingerprint'] = 'a'*64
        elif attack == 'load_label':
            events[i]['body']['observed_load'] = 'MEDIUM'
        elif attack == 'missing_inference':
            next(e for e in events if e['kind'] == 'request' and e['body']['inference'] is not None)['body']['inference'] = None
        else:
            events.append({'kind': 'instance_occupancy', 'body': {}})
    rewrite(source, tmp_path/'bad', mutate)
    with pytest.raises((ValueError, KeyError)):
        analyze(tmp_path/'bad')


def test_failed_run_is_retained_and_partial_analysis_never_certifies(tmp_path):
    cfg = config()
    env = SyntheticEnvironment(cfg, fail=True)
    with pytest.raises(RuntimeError):
        execute(cfg, tmp_path/'failed', env)
    report, rows = analyze(tmp_path/'failed')
    assert not report['execution_complete'] and not report['analysis_qualified']
    assert list(read_events(tmp_path/'failed/journal.jsonl'))[-1]['kind'] == 'aborted'
    assert any(not r.success for r in rows)
    assert all(s.close_called for s in env.sessions)


def test_no_overwrite_of_prior_run(completed):
    directory, cfg, _ = completed
    before = (directory/'journal.jsonl').read_bytes()
    with pytest.raises(FileExistsError):
        execute(cfg, directory, SyntheticEnvironment(cfg))
    assert (directory/'journal.jsonl').read_bytes() == before


def test_a1_masks_only_model_state_and_model_cannot_update_after_freeze(completed):
    directory, _, _ = completed
    _, rows = analyze(directory)
    row = next(r for r in rows if r.experiences)
    e = row.experiences[0]
    normal = masked_vector(e.features, e.before, e.record.started_at, remove_state=False)
    masked = masked_vector(e.features, e.before, e.record.started_at, remove_state=True)
    assert normal[:5] == masked[:5] and normal[9:13] == masked[9:13]
    assert masked[5:9] == (0.,)*4 and masked[13:] == (1.,)*4
    bank = PolicyBank.restore(json.loads((directory/'models.json').read_text())['bank'])
    before = bank.fingerprint
    with pytest.raises(AdmissionError, match='FROZEN'):
        bank.update(row.allocation, e, row.workflow, started_at=row.started_at,
            finished_at=row.finished_at, elapsed_ns=row.elapsed_ns, registry=None)
    assert bank.fingerprint == before


def test_statistical_formulas_and_minimum_sample_gate():
    analysis = config().analysis
    assert quantile([0, 10, 20, 30], .5) == 15
    assert quantile([0, 10, 20, 30], .95) == pytest.approx(28.5)
    assert interval([1]*29, analysis) is None
    assert interval([2]*30, analysis) == [2, 2]
    assert interval(list(range(30)), analysis) == interval(list(range(30)), analysis)
    assert throughput_interval([(2, 1)]*30, analysis) == [2, 2]
    assert throughput_interval([(2, 1)]*29, analysis) is None
    small = analysis.model_copy(update={'minimum_blocks': 2})
    # Two variable-duration blocks: ratio-of-totals outcomes are 1/9, 1/5, 1.
    assert throughput_interval([(1, 1), (1, 9)], small) == pytest.approx([1/9, 1])


def test_large_report_plots_paginate_without_omitting_groups(completed, tmp_path):
    report, _ = analyze(completed[0])
    first = report['groups'][0]
    report['groups'] = [{**first, 'workload': f'synthetic_case_{i}'} for i in range(29)]
    plot(report, tmp_path)
    assert (tmp_path/'latency.png').exists() and (tmp_path/'latency-02.png').exists()
    assert (tmp_path/'resources-02.svg').exists()
    assert 'synthetic_case_28' in (tmp_path/'latency-02.svg').read_text()


def test_sufficient_training_drives_frozen_inference_with_identical_evidence(tmp_path):
    cfg = config(train_blocks=10, warmup=0, requests_per_lane=4,
        groups=['E4', 'A1'], cache_conditions=['APPLICATION_WARM'])
    execute(cfg, tmp_path/'trained', SyntheticEnvironment(cfg))
    report, rows = analyze(tmp_path/'trained')
    assert report['gates']['training_min_samples']
    bank = PolicyBank.restore(json.loads((tmp_path/'trained/models.json').read_text())['bank'])
    full = next(m for k, m in bank.models.items() if k.startswith('E4:'))
    masked = next(m for k, m in bank.models.items() if k.startswith('A1:'))
    assert set(full._state.observations) == set(masked._state.observations)
    assert full._state.arm_observations == masked._state.arm_observations
    assert all(state.n >= 30 for state in full._state.arms.values())
    assert any(r.inference['advice']['sample_count'] >= 30 for r in rows if r.inference)
    # Independently reconstruct the masked state's A and b from actual test receipts.
    import math
    arm = masked._state.arms['S1_REPEAT_CACHE@1.0.0']
    expected_b = [0.]*17
    expected_a = [[float(i == j) for j in range(17)] for i in range(17)]
    for row in rows:
        if row.allocation.phase != 'TRAIN' or row.role != 'MEASURED':
            continue
        e = row.experiences[0]
        if e.strategy_id != 'S1_REPEAT_CACHE':
            continue
        x = masked_vector(e.features, e.before, e.record.started_at, remove_state=True)
        reward = -row.elapsed_ns/1e9
        for i in range(17):
            expected_b[i] += reward*x[i]
            for j in range(17):
                expected_a[i][j] += x[i]*x[j]
    assert arm.b == pytest.approx(expected_b)
    for actual, expected in zip(arm.a, expected_a):
        assert actual == pytest.approx(expected)


def test_a1_resource_protection_is_not_ablated(tmp_path):
    cfg = config(groups=['A1'])
    env = SyntheticEnvironment(cfg)
    original = env.monitor.latest_sample
    def unsafe():
        s = original()
        s.db_host.cpu_percent = 81
        return s
    env.monitor.latest_sample = unsafe
    with pytest.raises(RuntimeError, match='resource safety'):
        execute(cfg, tmp_path/'unsafe', env)
    events = list(read_events(tmp_path/'unsafe/journal.jsonl'))
    assert any(e['kind'] == 'resource_rejected' and e['body']['sample']['db_host']['cpu_percent'] == 81 for e in events)
    assert sum(len(s.executed) for s in env.sessions) == 2  # reference only; zero training dispatch
    assert events[-1]['kind'] == 'aborted'


def test_unordered_comparison_preserves_duplicate_multiplicity(completed):
    directory, cfg, _ = completed
    _, rows = analyze(directory)
    original = rows[0].run.execution.attempts[-1].execution
    q = cfg.workloads[0].train.model_copy(update={'expected_rows': 3})
    a = original.model_copy(update={'row_count': 3, 'rows_preview': [[1], [2], [1]]})
    b = original.model_copy(update={'row_count': 3, 'rows_preview': [[2], [1], [1]]})
    assert result_matches(a, b, q, cfg.analysis)
    b = b.model_copy(update={'rows_preview': [[2], [2], [1]]})
    assert not result_matches(a, b, q, cfg.analysis)
