"""Historical fixture playback with TEST-ONLY assignment labels; not live evidence."""
import json
from datetime import timedelta
from pathlib import Path
import random

import pytest

from tsdb_optimizer.bandit.exploration import Assignment, Trial, STRATEGIES
from tsdb_optimizer.bandit.randomized_model import evaluate_randomized, trial_partition
from tsdb_optimizer.schemas.learning import Experience, SampleStatus
from tsdb_optimizer.strategies import build_default_registry


def fixtures():
    train=json.loads(Path('docs/evidence/phase5_learning_fixes_2026-11-02.json').read_text())['experiences']
    heldout=json.loads(Path('docs/evidence/phase7_offline_2026-11-02.json').read_text())['independent_benchmark']['raw']['experiences']
    rows=[]
    for phase,data,strategy in [('train',train,'S0_NATIVE'),('holdout',heldout,'S1_REPEAT_CACHE')]:
        e=next(Experience.model_validate(e) for e in data if e['sample_status']=='VALID'
               and e['strategy_id']==strategy and e['context']['result_source']=='SERVER')
        seed=next(s for s in range(100) if random.Random(s).choice(STRATEGIES)==strategy)
        a=Assignment(phase=phase,seed=seed,index=0,workload_sha256=e.query_hash,
            cache_state_before='EMPTY',background_workers=0,strategy_id=strategy,
            assigned_at=e.record.started_at-timedelta(milliseconds=1))
        rows.append(Trial(assignment=a,experience=e,service_wall_ms=10,
                    background_started=0,background_completed=0,background_inflight_before=0))
    return rows


def test_real_trial_admission_updates_only_training_arm_and_frozen_holdout():
    rows=fixtures()
    r=evaluate_randomized(rows,build_default_registry())
    m=next(iter(r['models'].values()))
    assert m['arms']['S0_NATIVE@0.1.0']['n']==1
    assert m['arms']['S1_REPEAT_CACHE@1.0.0']['n']==0
    h=r['holdout'][0]
    assert h['chosen_reward'] is None
    assert h['ips_adaptive_contribution']==0
    assert h['ips_native_contribution']==0
    assert h['ips_fixed_s1_contribution']==2*h['observed_reward']
    assert r['model_frozen'] and not r['online_ready']
    assert rows[1].experience.execution_id not in m['observations']


def test_randomization_claim_and_quality_must_be_verified():
    rows=fixtures()
    bad=rows[0].model_copy(update={'assignment':rows[0].assignment.model_copy(update={'index':1})})
    with pytest.raises(ValueError,match='transcript'):
        evaluate_randomized([bad,rows[1]],build_default_registry())
    e=rows[0].experience.model_copy(update={'sample_status':SampleStatus.LOW_CONFIDENCE,'reward':None})
    # Use validated enum and schema after test fault injection.
    e=Experience.model_validate(e.model_dump(round_trip=True))
    bad=rows[0].model_copy(update={'experience':e})
    with pytest.raises(ValueError,match='qualified'):
        evaluate_randomized([bad,rows[1]],build_default_registry())


def test_preaction_cache_and_load_are_not_collapsed():
    t=fixtures()[0]
    changed=t.model_copy(update={'assignment':t.assignment.model_copy(update={'cache_state_before':'READY'})})
    assert trial_partition(t)!=trial_partition(changed)
    changed=t.model_copy(update={'assignment':t.assignment.model_copy(update={'background_workers':3})})
    assert trial_partition(t)!=trial_partition(changed)
