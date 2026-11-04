"""Historical-fixture protocol checks; modified timings are NOT live evidence."""
import json
from datetime import timedelta
from pathlib import Path

import pytest

from tsdb_optimizer.bandit.completed_request import CompletedRequest, E2E_REWARD_VERSION
from tsdb_optimizer.bandit.context import partition_key
from tsdb_optimizer.bandit.model import Config, OfflineLinUCB
from tsdb_optimizer.experiments.controlled_policy import ARMS, train
from tsdb_optimizer.orchestration.models import WorkflowResult
from tsdb_optimizer.schemas.learning import Experience
from tsdb_optimizer.strategies import build_default_registry


@pytest.fixture
def evidence():
    raw=json.loads(Path('tests/fixtures/completed_request_historical.json').read_text())
    e=Experience.model_validate(raw['experience']);w=WorkflowResult.model_validate(raw['workflow'])
    elapsed=max(1000.,raw['e2e_ms']+100)
    start=w.trace.events[0].at-timedelta(milliseconds=1)
    r=CompletedRequest.capture(e,w,started_at=start,finished_at=start+timedelta(milliseconds=elapsed),elapsed_ns=int(elapsed*1e6))
    registry=build_default_registry(cache_epoch=e.context.dataset_epoch)
    return r,e,w,registry


def model(r,e,**kwargs):
    return OfflineLinUCB(ARMS,baseline_arm=ARMS[0],partition='completed-request-v1:'+partition_key(e),
        training_until=r.finished_at,config=Config(reward_version=E2E_REWARD_VERSION),**kwargs)


def test_exact_mean_objective_and_duplicate_feedback(evidence):
    r,e,w,registry=evidence;m=model(r,e)
    assert m.update_completed_request(r,e,w,registry)
    state=m.snapshot();arm=state.arms[e.strategy_id+'@'+e.strategy_version]
    assert arm.b[0]==pytest.approx(-r.end_to_end_ms/1000)
    assert not m.update_completed_request(r,e,w,registry)
    assert arm.n==1 and m.online_advice() is None
    with pytest.raises(ValueError):m.update_experience(e,registry)
    old=OfflineLinUCB(ARMS,baseline_arm=ARMS[0],partition=partition_key(e),training_until=r.finished_at)
    with pytest.raises(ValueError):old.update_completed_request(r,e,w,registry)


@pytest.mark.parametrize('mutation',['timer','digest','identity','late_start','early_end','clock','warning','invalid','heldout','partition'])
def test_incomplete_untrusted_or_heldout_feedback_rejected(evidence,mutation):
    r,e,w,registry=evidence;m=model(r,e)
    if mutation=='timer':r=r.model_copy(update={'elapsed_ns':1})
    elif mutation=='digest':r=r.model_copy(update={'workflow_sha256':'wrong'})
    elif mutation=='identity':r=r.model_copy(update={'execution_id':'wrong'})
    elif mutation=='late_start':r=r.model_copy(update={'started_at':e.record.started_at+timedelta(seconds=1)})
    elif mutation=='early_end':r=r.model_copy(update={'finished_at':e.record.started_at})
    elif mutation=='clock':r=r.model_copy(update={'finished_at':r.finished_at+timedelta(seconds=1)})
    elif mutation=='warning':w.trace.warnings.append('cleanup failed')
    elif mutation=='invalid':e=e.model_copy(update={'sample_status':type(e.sample_status).INVALID,'reward':None})
    elif mutation=='heldout':m._state.training_until=r.finished_at-timedelta(microseconds=1)
    else:m._state.partition='wrong'
    with pytest.raises(ValueError):m.update_completed_request(r,e,w,registry)
    assert all(a.n==0 for a in m.snapshot().arms.values())


def test_training_receipts_complete_and_new_reward_isolation(evidence):
    r,e,w,registry=evidence
    rows=[('EMPTY',e)]
    new=train(rows,registry,completed={e.execution_id:(r,w)})['EMPTY'].snapshot()
    old=train(rows,registry)['EMPTY'].snapshot()
    assert new.config.reward_version==E2E_REWARD_VERSION
    assert new.partition!=old.partition and new.arms!=old.arms
    with pytest.raises(ValueError):train(rows,registry,completed={})


def test_rehashed_bad_quality_and_mutated_feedback_still_rejected(evidence):
    from tsdb_optimizer.schemas.learning import digest
    r,e,w,registry=evidence
    w.trace.warnings.append('STORE_SESSION_CLOSE_FAILED')
    r=r.model_copy(update={'workflow_sha256':digest(w.model_dump(mode='json',round_trip=True))})
    with pytest.raises(ValueError,match='unqualified'):model(r,e).update_completed_request(r,e,w,registry)


def test_conflicting_same_id_and_snapshot_restore_keep_reward_version(evidence):
    from tsdb_optimizer.bandit.model import Snapshot
    r,e,w,registry=evidence;m=model(r,e);m.update_completed_request(r,e,w,registry)
    restored=OfflineLinUCB.restore(Snapshot.model_validate_json(m.snapshot().model_dump_json()))
    assert restored.snapshot().config.reward_version==E2E_REWARD_VERSION
    assert not restored.update_completed_request(r,e,w,registry)
    r2=r.model_copy(update={'elapsed_ns':r.elapsed_ns-1_000_000,'finished_at':r.finished_at-timedelta(milliseconds=1)})
    with pytest.raises(ValueError,match='conflicting'):restored.update_completed_request(r2,e,w,registry)


def test_full_request_reward_changes_preference_when_overhead_reverses_adapter_ranking(evidence):
    from tsdb_optimizer.bandit.context import encode
    r0,e0,w0,registry=evidence
    raw=json.loads(Path('tests/fixtures/completed_request_s1_historical.json').read_text())
    e1=Experience.model_validate(raw['experience']);w1=WorkflowResult.model_validate(raw['workflow'])
    experiences=[e0,e1];workflows=[w0,w1]
    # Give the faster historical Adapter arm the slower SYNTHETIC whole request.
    fast=min(range(2),key=lambda i:experiences[i].record.client_wall_ms)
    receipts=[]
    for i,(e,w) in enumerate(zip(experiences,workflows)):
        start=w.trace.events[0].at-timedelta(milliseconds=1)
        ms=2000 if i==fast else 1000
        receipts.append(CompletedRequest.capture(e,w,started_at=start,finished_at=start+timedelta(milliseconds=ms),elapsed_ns=ms*1_000_000))
    rows=[('EMPTY',e) for e in experiences]
    old=train(rows,registry)['EMPTY']
    new=train(rows,registry,completed={e.execution_id:(r,w) for e,r,w in zip(experiences,receipts,workflows)})['EMPTY']
    x=encode(e0.features,e0.before,at=e0.record.started_at)
    best=lambda m:max(ARMS,key=lambda a:m.predict(a,x,ucb=False))
    assert best(old)==experiences[fast].strategy_id+'@'+experiences[fast].strategy_version
    assert best(new)!=best(old)
