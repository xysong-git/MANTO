"""Synthetic comparator/plan checks, never claimed as real performance evidence."""
from collections import Counter
import json

import pytest

from test_delayed_batch import feedback, model, registry
from tsdb_optimizer.bandit.episodes import BatchFeedback
from tsdb_optimizer.bandit.model import AdmissionError
from tsdb_optimizer.experiments.batch_statistics import BatchStatistics
from tsdb_optimizer.experiments.batches import plan, L1_POLICIES, SelectionTiming, summarize, BatchSummary


def another(f):
    body=f.model_dump(mode='json',round_trip=True)
    raw=json.dumps(body)
    replacements={f.contract.batch_id:f.contract.batch_id+'-copy'}
    for r in f.requests:
        replacements[r.workflow.trace.query_id]=r.workflow.trace.query_id+'-copy'
        replacements[r.workflow.run.decision.decision_id]=r.workflow.run.decision.decision_id+'-copy'
        for e in r.experiences:
            replacements[e.execution_id]=e.execution_id+'-copy'
            replacements[e.experience_id]=e.experience_id+'-copy'
    for old,new in replacements.items():raw=raw.replace('"'+old+'"','"'+new+'"')
    return BatchFeedback.model_validate_json(raw)


def test_l1_uses_complete_service_cost_deduplicates_and_freezes(feedback):
    policy=BatchStatistics(scope=model(feedback).scope,min_samples=2)
    reg=registry(feedback)
    assert policy.observe(feedback,reg)
    assert not policy.observe(feedback,reg)
    assert policy.observe(another(feedback),reg)
    rows=policy.snapshot().statistics
    assert len(rows)==1 and rows[0].count==2
    assert rows[0].total_service_ms==pytest.approx(2*feedback.service_ms)
    policy.freeze();before=policy.fingerprint
    with pytest.raises(AdmissionError,match='FROZEN'):policy.observe(feedback,reg)
    assert before==policy.fingerprint


def test_low_quality_holdout_and_reused_execution_rejected(feedback):
    policy=BatchStatistics(scope=model(feedback).scope)
    reg=registry(feedback)
    body=feedback.model_dump(round_trip=True)
    body['requests'][0]['experiences'][0].update(sample_status='LOW_CONFIDENCE',reward=None,quality_reasons=['TEST_ONLY'])
    with pytest.raises(AdmissionError):policy.observe(BatchFeedback.model_validate(body),reg)
    heldout=feedback.model_copy(update={'contract':feedback.contract.model_copy(update={'phase':'HOLDOUT'})})
    with pytest.raises(AdmissionError):policy.observe(heldout,reg)
    assert policy.snapshot().statistics==[]
    policy.observe(feedback,reg)
    duplicate=feedback.model_copy(update={'contract':feedback.contract.model_copy(update={'batch_id':'another'})})
    with pytest.raises(AdmissionError,match='REUSED'):policy.observe(duplicate,reg)


def test_frozen_choice_uses_load_size_counts_and_registry(feedback):
    reg=registry(feedback);policy=BatchStatistics(scope=model(feedback).scope)
    contract=feedback.contract.model_copy(update={'phase':'HOLDOUT'})
    args=(contract,feedback.features,feedback.selection_sample,feedback.context_at,reg)
    with pytest.raises(AdmissionError,match='FROZEN'):policy.choose(*args)
    # Synthetic sufficient statistics exercise decision mathematics, not training evidence.
    key=(feedback.load_bucket,contract.size)
    s0=(*key,'S0_NATIVE',reg.assert_selectable('S0_NATIVE').spec.version)
    s1=(*key,'S1_REPEAT_CACHE',reg.assert_selectable('S1_REPEAT_CACHE').spec.version)
    policy._totals[s0]=[30,3000.];policy._totals[s1]=[29,1450.]
    policy.freeze()
    assert policy.choose(*args)=='S0_NATIVE'
    policy._totals[s1]=[30,1500.]
    assert policy.choose(*args)=='S1_REPEAT_CACHE'
    other_size=contract.model_copy(update={'size':2 if contract.size==8 else 8})
    assert policy.choose(other_size,*args[1:])=='S0_NATIVE'
    other_load=feedback.selection_sample.model_copy(deep=True)
    other_load.db_host.cpu_percent=60 if feedback.load_bucket=='LOW' else 10
    assert policy.choose(contract,feedback.features,other_load,feedback.context_at,reg)=='S0_NATIVE'
    policy._totals[s1]=[30,3000.]
    assert policy.choose(*args)=='S0_NATIVE'
    policy._totals[s1]=[30,1500.]
    reg.quarantine_version('S1_REPEAT_CACHE',s1[-1])
    assert policy.choose(*args)=='S0_NATIVE'


def test_v7_plan_adds_real_l1_batches_without_changing_legacy():
    assert list(plan('TRAIN',45,include_l1=True))==list(plan('TRAIN',45))
    rows=list(plan('HOLDOUT',45,include_l1=True))
    counts=Counter((r['load'],b['size'],b['policy']) for r in rows for b in r['batches'])
    assert len(counts)==16 and set(counts.values())=={45}
    assert sum(counts.values())==720
    assert all(len(r['batches'])==6 for r in plan('HOLDOUT',45))


def test_selection_segments_cannot_hide_or_add_cost():
    fields=dict(setup_ns=1,features_ns=2,admission_ns=3,prediction_ns=4,fingerprint_ns=5,finalize_ns=6,total_ns=21)
    assert SelectionTiming(**fields).total_ns==21
    with pytest.raises(ValueError):SelectionTiming(**(fields|{'total_ns':20}))


def test_l1_comparison_does_not_replace_dual_baseline_gate():
    training=Counter({(load,size,p):45 for load in ('LOW','MEDIUM') for size in (2,8) for p in L1_POLICIES[:2]})
    outcomes=[(load,block,BatchSummary(size,p,'S0_NATIVE',load,True,
        {'S0_NATIVE':100.,'S1_REPEAT_CACHE':50.,'ADAPTIVE':75.,'L1_BATCH':90.}[p],1.,0))
        for load in ('LOW','MEDIUM') for size in (2,8) for block in range(45) for p in L1_POLICIES]
    report=summarize(outcomes,training,45,include_l1=True)
    assert report['l1_contrast']['within_session_benefit']
    assert not report['within_session_benefit_gate']
    with pytest.raises(ValueError):summarize(outcomes[:-1],training,45,include_l1=True)


def test_replayed_statistics_bind_exact_same_evidence_as_linucb(feedback):
    reg=registry(feedback);bandit=model(feedback)
    first=BatchStatistics(scope=bandit.scope);replay=BatchStatistics(scope=bandit.scope)
    for f in (feedback,another(feedback)):
        assert bandit.update(f,reg)
        assert first.observe(f,reg)
        assert replay.observe(BatchFeedback.model_validate_json(f.model_dump_json(round_trip=True)),reg)
    assert first.freeze().evidence==bandit.evidence
    replay.freeze()
    assert first.fingerprint==replay.fingerprint


def test_training_cannot_claim_l1_allocation(feedback):
    body=feedback.model_dump(round_trip=True)
    body['policy']='L1_BATCH'
    for r in body['requests']:r['policy']='L1_BATCH'
    with pytest.raises(ValueError,match='randomized fixed option'):
        BatchFeedback.model_validate(body)
