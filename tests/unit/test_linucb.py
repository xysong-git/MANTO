"""Deterministic algorithm tests. Synthetic updates never become real evidence."""
from datetime import datetime, timedelta, timezone
import math
import random
import sqlite3

import pytest
from pydantic import ValidationError

from tsdb_optimizer.bandit import Config, OfflineLinUCB, OfflineModelStore, Snapshot
from tsdb_optimizer.bandit.context import DIMENSIONS, encode, partition_key
from tsdb_optimizer.bandit.evaluation import frozen_evaluate, audit
from tsdb_optimizer.agents import QueryAnalysisAgent
from tsdb_optimizer.schemas.learning import Experience
from tsdb_optimizer.strategies import build_default_registry
from test_learning import sample
from phase4_support import TEST_SQL

T = datetime(2026,1,1,tzinfo=timezone.utc)


def vector(cpu=0):
    x = [0.]*len(DIMENSIONS); x[0]=1.; x[5]=cpu
    return tuple(x)


def model():
    return OfflineLinUCB(['S0@1','S1@1'],baseline_arm='S0@1',partition='TEST',domain='SYNTHETIC',
                        training_until=T+timedelta(days=1), config=Config(min_samples=3))


def update(m, arm='S1@1', execution_id='x', x=None, latency_ms=1000):
    return m.update_synthetic(execution_id=execution_id,arm=arm,x=x or vector(),
                              latency_ms=latency_ms,finished_at=T)


def test_hand_calculation_updates_only_observed_arm():
    m=model(); old=m.snapshot().arms['S0@1'].model_dump()
    update(m)
    state=m.snapshot()
    assert state.arms['S1@1'].a[0][0] == 2
    assert state.arms['S1@1'].b[0] == -math.log(2)
    assert state.arms['S0@1'].model_dump() == old
    assert m.predict('S1@1',vector()) == pytest.approx(-math.log(2)/2+.1/math.sqrt(2))
    assert m.online_advice() is None and state.online_ready is False


def test_seeded_context_preferences_and_reproducibility():
    def train():
        m=model()
        schedule=[(arm,cpu) for arm in ['S0@1','S1@1'] for cpu in [.1,.9] for _ in range(60)]
        random.Random(20261102).shuffle(schedule)
        for i,(arm,cpu) in enumerate(schedule):
            # One virtual execution outcome for the selected arm, not labels for both.
            fast=(arm=='S0@1') == (cpu < .5)
            update(m,arm,str(i),vector(cpu),1 if fast else 1000)
        return m
    a,b=train(),train()
    assert a.snapshot().arms == b.snapshot().arms
    assert a.choose(vector(.1),['S0@1','S1@1']) == 'S0@1'
    assert a.choose(vector(.9),['S0@1','S1@1']) == 'S1@1'
    assert a.choose(vector(.9),['S0@1','S1@1'],explore=True) == 'S1@1'
    assert sum(arm.n for arm in a.snapshot().arms.values()) == 240


def test_min_samples_bootstrap_and_unknown_arm():
    m=model()
    assert m.choose(vector(),['S0@1','S1@1']) == 'S0@1'
    for i in range(6):
        chosen=m.choose(vector(),['S0@1','S1@1'],explore=True)
        update(m,chosen,str(i))
    assert [a.n for a in m.snapshot().arms.values()] == [3,3]
    with pytest.raises(ValueError): m.choose(vector(),['S0@1','arbitrary'])
    with pytest.raises(ValueError): m.choose(vector(),['S1@1'])


def test_dedup_conflict_holdout_and_domain_isolation():
    m=model(); assert update(m); assert not update(m)
    with pytest.raises(ValueError): update(m,latency_ms=2)
    with pytest.raises(ValueError): m.update_synthetic(execution_id='future',arm='S0@1',x=vector(),latency_ms=1,
                                                       finished_at=T+timedelta(days=2))
    real=OfflineLinUCB(['S0@1','S1@1'],baseline_arm='S0@1',partition='x',training_until=T)
    with pytest.raises(ValueError): update(real)
    assert sum(a.n for a in real.snapshot().arms.values()) == 0


@pytest.mark.parametrize('value', [float('nan'),float('inf'),-1])
def test_invalid_latency_is_not_an_update(value):
    m=model()
    with pytest.raises(ValueError): update(m,latency_ms=value)
    assert not m.snapshot().observations


def test_context_scaling_missing_sources_and_no_outcome_feature():
    f=QueryAnalysisAgent().analyze(TEST_SQL).features
    s=sample(); s.db_host.valid=False
    x=encode(f,s,at=s.client_host.captured_at)
    assert len(x)==17 and x[0]==1
    assert x[5]==.1 and x[6]==.2
    assert x[7:9]==(0.,0.) and x[15:17]==(1.,1.)
    assert all('latency' not in name and 'result_rows' not in name for name in DIMENSIONS)
    f.device_count.value=100000
    assert encode(f,None,at=T)[1]==1


def test_atomic_save_reload_cas_rollback_and_corruption(tmp_path):
    store=OfflineModelStore(tmp_path/'offline.sqlite')
    m=model(); first=m.snapshot(); key=store.publish(first)
    update(m); second=m.snapshot(); store.publish(second,expected_head=first.model_version)
    loaded=OfflineLinUCB.restore(store.current(key))
    assert loaded.predict('S1@1',vector())==m.predict('S1@1',vector())
    assert not update(loaded)  # dedup ledger survived reload
    with pytest.raises(ValueError): store.publish(m.snapshot(),expected_head=first.model_version)
    store.rollback(key,first.model_version,expected_head=second.model_version)
    assert store.current(key).model_version==first.model_version
    with store.connection() as c:
        c.execute("CREATE TRIGGER fail_head BEFORE UPDATE ON bandit_heads BEGIN SELECT RAISE(ABORT, 'test'); END")
    failed=m.snapshot()
    with pytest.raises(sqlite3.IntegrityError): store.publish(failed,expected_head=first.model_version)
    with store.connection() as c:
        assert c.execute('SELECT count(*) FROM bandit_models WHERE version=?',(failed.model_version,)).fetchone()[0]==0
        c.execute('UPDATE bandit_models SET checksum=? WHERE version=?',('bad',first.model_version))
    with pytest.raises(ValueError): store.current(key)


def test_snapshot_rejects_bad_dimensions_spd_and_deployment_flag():
    body=model().snapshot().model_dump()
    body['online_ready']=True
    with pytest.raises(ValidationError): Snapshot.model_validate(body)
    body['online_ready']=False; body['arms']['S1@1']['a'][0][0]=-1
    with pytest.raises(ValidationError): Snapshot.model_validate(body)


def test_real_artifact_admission_gate_and_time_isolation():
    import json
    from pathlib import Path
    records=[Experience.model_validate(e) for e in json.loads(Path(
        'docs/evidence/phase5_learning_fixes_2026-11-02.json').read_text())['experiences']]
    registry=build_default_registry()
    report=audit(records,registry)
    assert report['online_ready'] is False
    assert report['sample_status_counts']=={'VALID':12,'LOW_CONFIDENCE':12}
    valid=next(e for e in records if e.sample_status.value=='VALID' and e.strategy_id=='S0_NATIVE')
    m=OfflineLinUCB(['S0_NATIVE@0.1.0','S1_REPEAT_CACHE@1.0.0'],baseline_arm='S0_NATIVE@0.1.0',
        partition=partition_key(valid),training_until=valid.record.finished_at)
    assert m.update_experience(valid,registry)
    with pytest.raises(ValueError): frozen_evaluate(m,[valid],registry)
    bad=next(e for e in records if e.sample_status.value!='VALID')
    with pytest.raises(ValueError): m.update_experience(bad,registry)
    with pytest.raises(ValueError): m.update_experience(valid.model_copy(update={'resource_policy_hash':'wrong'}),registry)


def test_experiment_config_cannot_enable_deployment(tmp_path):
    from tsdb_optimizer.bandit.settings import load_settings
    assert load_settings().online_enabled is False
    bad=tmp_path/'bad.yaml'; bad.write_text('online_enabled: true\n')
    with pytest.raises(ValidationError): load_settings(bad)


def test_independent_benchmark_artifact_has_no_leakage_or_invented_rewards():
    import json
    from pathlib import Path
    evidence=json.loads(Path('docs/evidence/phase7_offline_2026-11-02.json').read_text())
    train=json.loads(Path('docs/evidence/phase5_learning_fixes_2026-11-02.json').read_text())
    real=evidence['independent_benchmark']
    assert real['independent_ids_and_future_window']
    assert not ({e['execution_id'] for e in train['experiences']} &
                {e['execution_id'] for e in real['raw']['experiences']})
    assert all(e['record']['started_at'] > real['training_cutoff'] for e in real['raw']['experiences'])
    for evaluation in real['evaluation'].values():
        assert evaluation['policy_value'] is None and evaluation['frozen_model_unchanged']
        rows=evaluation['test_rows']
        assert rows
        expected=sum((r['observed_reward']-r['predicted_reward'])**2 for r in rows)/len(rows)
        assert evaluation['factual_reward_mse']==pytest.approx(expected)
        for row in rows:
            if row['chosen_offline_arm'] != row['observed_arm']:
                assert row['chosen_reward'] is None
    assert evidence['online_enabled'] is False


def test_real_shadow_selection_filters_quarantined_or_changed_versions(monkeypatch):
    m=OfflineLinUCB(['S0_NATIVE@0.1.0','S1_REPEAT_CACHE@1.0.0','S1_REPEAT_CACHE@STALE'],
        baseline_arm='S0_NATIVE@0.1.0',partition='test',training_until=T)
    registry=build_default_registry()
    registry.quarantine_version('S1_REPEAT_CACHE','1.0.0')
    def inspect(x, eligible, **kwargs):
        assert eligible == ['S0_NATIVE@0.1.0']
        return eligible[0]
    monkeypatch.setattr(m,'choose',inspect)
    assert m.choose_registered(vector(),registry)=='S0_NATIVE@0.1.0'
