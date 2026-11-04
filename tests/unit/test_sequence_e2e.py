"""Synthetic transformations of historical fixtures, never performance evidence."""
from datetime import timedelta
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from tsdb_optimizer.experiments.sequences import SequenceRequest, schedule, paired_report, validate_sequence
from tsdb_optimizer.orchestration.models import WorkflowResult
from test_orchestrator import lab


@pytest.fixture
def raw():
    source = json.loads(Path('tests/fixtures/completed_request_historical.json').read_text())
    source['workflow']['run']['decision']['query_occurrence'] = 1
    w = WorkflowResult.model_validate(source['workflow'])
    start = w.trace.events[0].at-timedelta(milliseconds=1)
    return dict(block=0, length=1, policy='S0_NATIVE', index=0, cache_before='EMPTY',
        cache_entries_after=0, started_at=start, finished_at=start+timedelta(seconds=2),
        elapsed_ns=2_000_000_000, workflow=source['workflow'], experiences=[source['experience']])


def test_all_request_costs_including_low_confidence_are_accounted(raw):
    raw['experiences'][0].update(sample_status='LOW_CONFIDENCE', reward=None, quality_reasons=['TEST_ONLY'])
    row = SequenceRequest.model_validate(raw)
    assert row.e2e_ms == 2000
    ref = row.workflow.run.execution.attempts[-1].execution
    validate_sequence([row], ref, sql_hash=row.workflow.trace.sql_sha256, epoch='TEST_ONLY')


@pytest.mark.parametrize('change', ['first_visit', 'timer', 'clock', 'missing_experience', 'duplicate_experience', 'warm_first', 'nan'])
def test_invalid_request_evidence_rejected(raw, change):
    if change == 'first_visit': raw['workflow']['run']['decision']['query_occurrence'] = 2
    elif change == 'timer': raw['elapsed_ns'] = 1
    elif change == 'clock': raw['finished_at'] += timedelta(seconds=1)
    elif change == 'missing_experience': raw['experiences'] = []
    elif change == 'duplicate_experience': raw['experiences'] *= 2
    elif change == 'warm_first': raw['cache_before'] = 'READY'
    elif change == 'nan': raw['elapsed_ns'] = float('nan')
    with pytest.raises(ValueError): SequenceRequest.model_validate(raw)


@pytest.mark.parametrize('timing', [
    dict(store_open_ms=0, body_ms=3000, store_close_ms=0, total_ms=3000),
    dict(store_open_ms=1, body_ms=0, store_close_ms=1, total_ms=2),
    dict(store_open_ms=1, body_ms=1000, store_close_ms=1, total_ms=1000),
])
def test_session_timing_must_enclose_trace_and_fit_outer_timer(raw, timing):
    raw['workflow']['request_timing'] = timing
    with pytest.raises(ValueError):
        SequenceRequest.model_validate(raw)


def test_unmeasured_cache_fill_and_incomplete_sequence_rejected(raw):
    raw['cache_entries_after'] = 1
    row = SequenceRequest.model_validate(raw)
    ref = row.workflow.run.execution.attempts[-1].execution
    with pytest.raises(ValueError, match='unmeasured cache fill'):
        validate_sequence([row], ref, sql_hash=row.workflow.trace.sql_sha256, epoch='TEST_ONLY')
    raw['length'] = 2; raw['cache_entries_after'] = 0
    with pytest.raises(ValueError, match='incomplete sequence'):
        validate_sequence([SequenceRequest.model_validate(raw)], ref,
            sql_hash=row.workflow.trace.sql_sha256, epoch='TEST_ONLY')


def test_fallback_with_learning_reward_is_rejected(raw):
    raw['workflow']['run']['execution']['fallback_used'] = True
    with pytest.raises(ValueError): SequenceRequest.model_validate(raw)


def test_schedule_and_pairing_require_all_predeclared_cells():
    plan = list(schedule())
    assert len(plan) == len(set(plan)) == 270
    assert sum(n for _, n, _ in plan) == 990
    assert plan == list(schedule())
    with pytest.raises(ValueError, match='incomplete'): paired_report({})


def test_sequence_statistics_include_first_fill_and_fallback_costs():
    # Every policy has exactly the same TOTAL, although its last hit is faster.
    def row(ms, policy, fallback=False):
        execution = SimpleNamespace(fallback_used=fallback, attempts=[SimpleNamespace(
            execution=SimpleNamespace(strategy_id='S0_NATIVE', served_from_cache=False))])
        return SimpleNamespace(e2e_ms=ms, policy=policy, workflow=SimpleNamespace(run=SimpleNamespace(execution=execution)),
            experiences=[SimpleNamespace(sample_status=SimpleNamespace(value='LOW_CONFIDENCE' if fallback else 'VALID'))])
    seq = {}
    for b, n, p in schedule():
        values = [100]*n if p == 'S0_NATIVE' or n == 1 else [100*n-(n-1)*10]+[10]*(n-1)
        seq[(b,n,p)] = [row(v, p, i == 0) for i,v in enumerate(values)]
    result = paired_report(seq)
    assert result['8']['fallback_requests'] == 90
    assert result['8']['sample_quality']['LOW_CONFIDENCE'] == 90
    for item in result.values():
        for contrast in item['contrasts'].values():
            assert contrast['mean_ms'] == 0
            assert not contrast['faster_within_session']


def test_changed_result_is_rejected(raw):
    row = SequenceRequest.model_validate(raw)
    ref = row.workflow.run.execution.attempts[-1].execution.model_copy(deep=True)
    ref.rows_preview = []
    with pytest.raises(ValueError, match='mismatched'):
        validate_sequence([row], ref, sql_hash=row.workflow.trace.sql_sha256, epoch='TEST_ONLY')


def test_real_workflow_injected_fallback_is_accounted_but_cannot_train(lab):
    from phase4_support import TEST_SQL
    from tsdb_optimizer.schemas.execution import ExecutionStatus
    flow, adapter, _, enqueue = lab
    enqueue(); flow.run(TEST_SQL)
    enqueue(status=ExecutionStatus.ERROR, request_dispatched=False, request_completed=False)
    enqueue()
    w = flow.run(TEST_SQL)
    assert w.run.execution.fallback_used and w.trace.database_dispatches == 1
    start = min(w.trace.events[0].at, *(a.execution.started_at for a in w.run.execution.attempts))-timedelta(milliseconds=1)
    row = SequenceRequest(block=0, length=2, policy='S1_REPEAT_CACHE', index=1,
        cache_before='EMPTY', cache_entries_after=0, started_at=start,
        finished_at=start+timedelta(seconds=2), elapsed_ns=2_000_000_000, workflow=w,
        experiences=[flow.learner.repository.get(a.execution.execution_id) for a in w.run.execution.attempts])
    assert row.e2e_ms == 2000
    assert len(row.experiences) == 2 and all(e.reward is None for e in row.experiences)
    assert len(adapter.calls) == 3  # First visit, pre-dispatch failure, one safe Native.


def test_admission_uses_bounded_refresh_not_unconditionally_rejecting_old_latest(raw, monkeypatch):
    monkeypatch.syspath_prepend('scripts')
    from phase7_sequence_e2e import admit
    from tsdb_optimizer.schemas.common import utc_now
    from tsdb_optimizer.schemas.monitoring import SystemSample
    sample = SystemSample.model_validate(raw['experiences'][0]['before'])
    for name in ('client_host', 'db_host'):
        setattr(sample, name, getattr(sample, name).model_copy(update={
            'captured_at': utc_now(), 'stale_after_ms': 500, 'cpu_percent': 10}))
    class Monitor:
        def sample_once(self): return sample
        def latest_sample(self): raise AssertionError('must use existing bounded refresh')
    fresh, at = admit(Monitor())
    assert fresh is sample and at >= sample.db_host.captured_at
    sample.db_host.captured_at -= timedelta(seconds=1)
    with pytest.raises(RuntimeError, match='fresh'): admit(Monitor())


def test_resource_admission_does_not_relax_cpu_guard(raw, monkeypatch):
    monkeypatch.syspath_prepend('scripts')
    from phase7_sequence_e2e import admit
    from tsdb_optimizer.schemas.common import utc_now
    from tsdb_optimizer.schemas.monitoring import SystemSample
    sample = SystemSample.model_validate(raw['experiences'][0]['before'])
    for name in ('client_host', 'db_host'):
        getattr(sample, name).captured_at = utc_now()
    sample.db_host.cpu_percent = 80.01
    with pytest.raises(RuntimeError, match='unsafe'): admit(SimpleNamespace(sample_once=lambda: sample))
