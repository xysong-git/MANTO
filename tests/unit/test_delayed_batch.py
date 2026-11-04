"""Synthetic/historical-derived protocol tests, never live learning evidence."""
from datetime import timedelta
import json
from pathlib import Path
import sqlite3

import pytest

from tsdb_optimizer.bandit.episodes import BatchFeedback, BatchPolicy
from tsdb_optimizer.bandit.delayed_store import DelayedBatchStore
from tsdb_optimizer.bandit.model import AdmissionError
from tsdb_optimizer.experiments.batches import plan
from tsdb_optimizer.strategies import build_default_registry


@pytest.fixture
def feedback():
    return BatchFeedback.model_validate(json.loads(Path('tests/fixtures/batch_feedback_historical.json').read_text())['feedback'])


def model(f):
    return BatchPolicy(deadline=f.closed_at+timedelta(days=1),family_key=f.contract.family_key,
        server_id=f.contract.server_id,epoch=f.contract.epoch)


def registry(f): return build_default_registry(cache_epoch=f.contract.epoch)


def test_fill_and_all_hits_receive_one_delayed_option_label(feedback):
    f=feedback;m=model(f)
    assert m.update(f,registry(f))
    state=m.models['8'].snapshot()
    assert state.arms['S0_NATIVE@0.1.0'].n==0
    arm=state.arms['S1_REPEAT_CACHE@1.0.0']
    assert arm.n==1 and arm.b[0]==pytest.approx(-f.service_ms/1000)
    assert f.service_ms==pytest.approx(2+sum(r.e2e_ms for r in f.requests))
    assert f.requests[0].workflow.run.execution.attempts[0].execution.strategy_id=='S0_NATIVE'
    assert sum(r.workflow.run.execution.attempts[-1].execution.served_from_cache for r in f.requests)==6
    assert not m.update(f,registry(f))
    assert BatchPolicy.restore(m.snapshot()).fingerprint==m.fingerprint


@pytest.mark.parametrize('mutation',['partial','foreign_hit','timer','future_context','mixed_epoch','wrong_option','future_size'])
def test_unclosed_or_false_attribution_is_rejected(feedback,mutation):
    body=feedback.model_dump(mode='json',round_trip=True)
    if mutation=='partial':body['requests'].pop()
    elif mutation=='foreign_hit':body['requests'][2]['workflow']['run']['execution']['attempts'][0]['execution']['cache_source_execution_id']='FOREIGN'
    elif mutation=='timer':body['selection_ns']=100000000000
    elif mutation=='future_context':body['contract']['declared_at']=body['closed_at']
    elif mutation=='mixed_epoch':body['requests'][0]['experiences'][0]['context']['dataset_epoch']='other'
    elif mutation=='wrong_option':body['allocated_strategy']='S0_NATIVE'
    else:body['contract']['size']=2
    with pytest.raises(ValueError):BatchFeedback.model_validate(body)


def test_queue_only_updates_after_close_and_drain_and_survives_restart(tmp_path,feedback):
    q=DelayedBatchStore(tmp_path/'queue.sqlite',initial=model(feedback))
    assert q.enqueue(feedback)
    assert not q.enqueue(feedback)
    assert q.current()[1]==0
    with pytest.raises(ValueError,match='pending'):q.freeze()
    restarted=DelayedBatchStore(q.path)
    result=restarted.drain_one(registry(feedback))
    assert result['status']=='APPLIED' and result['revision']==1
    assert restarted.drain_one(registry(feedback)) is None
    frozen=restarted.freeze()
    assert frozen['revision']==1
    with pytest.raises(ValueError,match='frozen'):restarted.enqueue(feedback)


def test_atomic_publication_failure_rolls_back_ack_and_parameters(tmp_path,feedback,monkeypatch):
    q=DelayedBatchStore(tmp_path/'queue.sqlite',initial=model(feedback));q.enqueue(feedback)
    before=q.current()[0].fingerprint
    original=q._publish
    def fail(*args):raise sqlite3.OperationalError('TEST_ONLY publication failure')
    monkeypatch.setattr(q,'_publish',fail)
    with pytest.raises(sqlite3.OperationalError):q.drain_one(registry(feedback))
    assert q.current()[1]==0 and q.current()[0].fingerprint==before
    with q.connection() as c:assert c.execute('SELECT status FROM feedback').fetchone()[0]=='PENDING'
    monkeypatch.setattr(q,'_publish',original)
    assert q.drain_one(registry(feedback))['revision']==1


def test_heldout_conflicting_and_reused_executions_cannot_train(tmp_path,feedback):
    m=model(feedback);m.update(feedback,registry(feedback))
    copy_id=feedback.model_copy(update={'contract':feedback.contract.model_copy(update={'batch_id':'another'})})
    with pytest.raises(AdmissionError,match='ALREADY_ATTRIBUTED'):m.update(copy_id,registry(feedback))
    q=DelayedBatchStore(tmp_path/'queue.sqlite',initial=model(feedback));q.enqueue(feedback)
    with pytest.raises(sqlite3.IntegrityError):q.enqueue(copy_id)
    heldout=feedback.model_copy(update={'contract':feedback.contract.model_copy(update={'phase':'HOLDOUT'})})
    with pytest.raises(ValueError):q.enqueue(heldout)
    with pytest.raises(AdmissionError):m.update(heldout,registry(feedback))
    bad=feedback.model_copy(update={'closed_at':feedback.closed_at+timedelta(seconds=1)})
    with pytest.raises(ValueError,match='conflicting'):q.enqueue(bad)


def test_low_quality_batch_is_rejected_without_parameter_update(tmp_path,feedback):
    body=feedback.model_dump(round_trip=True)
    body['requests'][0]['experiences'][0].update(sample_status='LOW_CONFIDENCE',reward=None,quality_reasons=['TEST_ONLY'])
    bad=BatchFeedback.model_validate(body)
    q=DelayedBatchStore(tmp_path/'queue.sqlite',initial=model(bad));q.enqueue(bad)
    assert q.drain_one(registry(bad))['status']=='REJECTED'
    assert q.current()[1]==0


def test_balanced_plan_counts_and_disjoint_seed():
    from collections import Counter
    for phase,total in [('TRAIN',240),('HOLDOUT',360)]:
        schedule=list(plan(phase));assert len(schedule)==60
        counts=Counter((r['load'],b['size'],b['policy']) for r in schedule for b in r['batches'])
        assert sum(counts.values())==total and set(counts.values())=={30}
        assert schedule==list(plan(phase))


def test_record_only_keeps_quality_and_persistence_but_does_not_publish(monkeypatch, tmp_path):
    from test_learning import lab, ingest
    # Use the existing fixture factory without invoking its pytest wrapper.
    setup=lab.__wrapped__(tmp_path)
    store,repo,agent,_,_,_=setup
    agent.statistics_mode='record_only'
    def forbidden(*args,**kwargs):raise AssertionError('synchronous statistics called')
    monkeypatch.setattr(repo,'build_model',forbidden)
    for _ in range(4):
        result=ingest(setup)[-1]
        assert result.sample_status.value=='VALID' and not result.model_updated and not result.warnings
    with store._connect() as connection:
        assert connection.execute('SELECT count(*) FROM experiences').fetchone()[0]==4


def test_swapped_load_labels_cannot_satisfy_heldout_gate():
    from collections import Counter
    from tsdb_optimizer.experiments.batches import BatchSummary, LOADS, SIZES, POLICIES, summarize
    counts=Counter({(l,n,p):30 for l in LOADS for n in SIZES for p in POLICIES[:2]})
    rows=[(l,b,BatchSummary(n,p,'S0_NATIVE','MEDIUM' if l=='LOW' else 'LOW',True,10,5,0))
          for l in LOADS for n in SIZES for p in POLICIES for b in range(30)]
    result=summarize(rows,counts)
    assert result['training_sample_gate']
    assert not result['holdout_sample_gate']
    assert result['contrasts']=={}


def test_batch_admission_records_stale_evidence_without_query(feedback,monkeypatch):
    import sys
    monkeypatch.syspath_prepend(str(Path('scripts').resolve()))
    import phase7_delayed_batch as runner
    sample=feedback.selection_sample
    class Monitor:
        def sample_once(self):return sample
    events=[]
    monkeypatch.setattr(runner,'utc_now',lambda:sample.db_host.captured_at+timedelta(milliseconds=251))
    with pytest.raises(RuntimeError,match='admission'):
        runner.admit(Monitor(),lambda kind,body:events.append((kind,body)))
    assert events[0][0]=='resource_rejected' and not events[0][1]['fresh']
    assert events[0][1]['sample']['db_host']['snapshot_id']==sample.db_host.snapshot_id


def test_raw_request_cannot_update_batch_reward_model(feedback):
    m=model(feedback);before=m.fingerprint
    with pytest.raises(AdmissionError):
        m.models['8'].update_experience(feedback.requests[0].experiences[0],registry(feedback))
    assert m.fingerprint==before


def test_corrupt_pending_feedback_is_not_acknowledged(tmp_path,feedback):
    q=DelayedBatchStore(tmp_path/'queue.sqlite',initial=model(feedback));q.enqueue(feedback)
    with q.connection() as c:c.execute("UPDATE feedback SET checksum='corrupt'")
    with pytest.raises(ValueError,match='checksum'):q.drain_one(registry(feedback))
    assert q.current()[1]==0
    with q.connection() as c:assert c.execute('SELECT status FROM feedback').fetchone()[0]=='PENDING'


def test_fixed_45_block_plan_is_complete_not_adaptive_top_up():
    from collections import Counter
    for phase,total in [('TRAIN',360),('HOLDOUT',540)]:
        rounds=list(plan(phase,45))
        assert len(rounds)==90
        cells=Counter((r['load'],b['size'],b['policy']) for r in rounds for b in r['batches'])
        assert sum(cells.values())==total and set(cells.values())=={45}
        assert rounds==list(plan(phase,45))
    with pytest.raises(ValueError):list(plan('TRAIN',40))


def test_45_block_analysis_keeps_cross_load_costs_and_final_third():
    from collections import Counter
    from tsdb_optimizer.experiments.batches import BatchSummary, LOADS, SIZES, POLICIES, summarize
    counts=Counter({(l,n,p):45 for l in LOADS for n in SIZES for p in POLICIES[:2]})
    rows=[]
    for l in LOADS:
        for n in SIZES:
            for p in POLICIES:
                for b in range(45):
                    cost=(9 if b<30 else 100) if p=='ADAPTIVE' else 10
                    rows.append((l,b,BatchSummary(n,p,'S0_NATIVE',l,b<40,cost,5,0)))
    report=summarize(rows,counts,45)
    assert report['holdout_sample_gate']
    assert report['contrasts']['S0_NATIVE']['three_batch_means_ms']==[-1,-1,90]
    assert not report['within_session_benefit_gate']
    with pytest.raises(ValueError,match='incomplete'):summarize(rows[:-1],counts,45)
