"""Corrupt copies only. Completed evidence may still fail sample coverage."""
import json,runpy
from pathlib import Path
import pytest
SOURCE=Path('data/phase7_e2e_wal55_20261103/journal.jsonl')
ANALYZE=runpy.run_path('scripts/phase7_analyze_controlled_e2e.py')['analyze']


def test_actual_missing_cell_is_not_promoted_to_qualification():
    r=ANALYZE(SOURCE)
    assert r['test_samples']==360 and r['training_samples']==480
    assert r['test_actual_load_counts']['MEDIUM/READY/ADAPTIVE']==29
    assert not r['qualification_complete'] and not r['within_session_benefit_gate']
    assert r['contrasts']=={}


@pytest.mark.parametrize('fault',['ttl','wal','missing_storage','setpoint','model','abort','missing_terminal'])
def test_corrupted_evidence_rejected(tmp_path,fault):
    events=[json.loads(l) for l in SOURCE.read_text().splitlines()]
    if fault=='ttl':
        e=next(e for e in events if e['kind']=='trial');e['body']['experience']['before']['db_host']['stale_after_ms']=5000
    elif fault=='wal':next(e for e in events if e['kind']=='storage_configuration')['body']['synchronous']=1
    elif fault=='missing_storage':events=[e for e in events if e['kind']!='storage_configuration']
    elif fault=='setpoint':events[0]['body']['cpu_setpoint_percent']=80
    elif fault=='model':next(e for e in events if e['kind']=='frozen_models')['body']['fingerprint']='forged'
    elif fault=='abort':events.insert(-1,{'kind':'aborted','body':{'error_type':'test'}})
    elif fault=='missing_terminal':events.pop()
    p=tmp_path/'journal.jsonl';p.write_text('\n'.join(json.dumps(e) for e in events)+'\n')
    with pytest.raises(ValueError):ANALYZE(p)
