"""Audit immutable real-study evidence using disposable tampered copies."""
import copy
import json
from pathlib import Path
import runpy

import pytest

ANALYZE=runpy.run_path('scripts/phase7_analyze_controlled_e2e.py')['analyze']
PATH=Path('data/phase7_reward_small_ambient_20261103/journal.jsonl')


@pytest.fixture(scope='module')
def events():
    return [json.loads(line) for line in PATH.read_text().splitlines()]


def test_real_ambient_study_cannot_pass_multiload_acceptance():
    report=ANALYZE(PATH)
    assert report['ambient_scope_complete']
    assert report['training_samples']==240 and report['test_samples']==216
    assert report['reward_objective']['version']=='negative-e2e-seconds-v1'
    assert not report['qualification_complete'] and not report['within_session_benefit_gate']
    assert not report['online_ready'] and not report['stage_accepted']


@pytest.mark.parametrize('case',['reward_version','missing_receipt','timer','identity','experience',
    'model_reward','cpu_load','wait_budget','duplicate_trial','aborted'])
def test_reject_tampered_reward_and_scope_evidence(events,tmp_path,case):
    rows=copy.deepcopy(events)
    t=next(r['body'] for r in rows if r['kind']=='trial')
    if case=='reward_version':rows[0]['body']['reward_version']='negative-log1p-ms-v1'
    elif case=='missing_receipt':t.pop('completed_request')
    elif case=='timer':t['e2e_ms']+=1
    elif case=='identity':t['completed_request']['execution_id']='wrong'
    elif case=='experience':t['experience']['reward']=0
    elif case=='model_reward':
        m=next(r['body'] for r in rows if r['kind']=='frozen_models')
        m['models']['EMPTY']['config']['reward_version']='negative-log1p-ms-v1'
    elif case=='cpu_load':rows[0]['body']['cpu_load_enabled']=True
    elif case=='wait_budget':rows[0]['body']['request_snapshot_wait_limit_s']=5
    elif case=='duplicate_trial':
        i=next(i for i,r in enumerate(rows) if r['kind']=='trial');rows.insert(i+1,copy.deepcopy(rows[i]))
    else:rows.insert(-1,{'kind':'aborted','body':{'error_type':'TEST_TAMPER'}})
    path=tmp_path/'journal.jsonl';path.write_text('\n'.join(json.dumps(r) for r in rows)+'\n')
    with pytest.raises(ValueError):ANALYZE(path)


def test_large_released_study_reproduces_without_deployment():
    path=Path('data/phase7_reward_large_released_20261103/journal.jsonl')
    report=ANALYZE(path)
    assert report['ambient_scope_complete'] and report['model_reproduced_and_frozen']
    assert report['test_samples']==216 and report['test_sample_gate']
    assert not report['qualification_complete'] and not report['stage_accepted']


@pytest.mark.parametrize('case',['missing','fingerprint','count'])
def test_training_memory_release_cannot_be_forged(tmp_path,case):
    path=Path('data/phase7_reward_large_released_20261103/journal.jsonl')
    rows=[json.loads(line) for line in path.read_text().splitlines()]
    i=next(i for i,r in enumerate(rows) if r['kind']=='training_memory_released')
    if case=='missing':rows.pop(i)
    elif case=='fingerprint':rows[i]['body']['model_fingerprint']='wrong'
    else:rows[i]['body']['completed_workflows']-=1
    altered=tmp_path/'journal.jsonl';altered.write_text('\n'.join(json.dumps(r) for r in rows)+'\n')
    with pytest.raises(ValueError):ANALYZE(altered)
