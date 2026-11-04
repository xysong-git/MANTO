"""Tamper probes on a copy of a real journal; never modifies original evidence."""
import json
from pathlib import Path
import runpy
import pytest

SOURCE=Path('data/phase7_e2e_v3_repeat_qualified_20261102/journal.jsonl')
ANALYZE=runpy.run_path('scripts/phase7_analyze_controlled_e2e.py')['analyze']

@pytest.mark.parametrize('fault',['missing_terminal','wrong_seed','duplicate_first_visit','changed_actual_arm','changed_model'])
def test_reject_corrupted_journal(tmp_path,fault):
    events=[json.loads(l) for l in SOURCE.read_text().splitlines()]
    if fault=='missing_terminal':events.pop()
    elif fault=='wrong_seed':events[0]['body']['seed']+=1
    elif fault=='duplicate_first_visit':
        i=next(i for i,e in enumerate(events) if e['kind']=='first_occurrence');events.insert(i+1,events[i])
    elif fault=='changed_actual_arm':
        e=next(e for e in events if e['kind']=='trial' and e['body']['phase']=='test')
        e['body']['experience']['strategy_id']='UNREGISTERED'
    elif fault=='changed_model':
        e=next(e for e in events if e['kind']=='frozen_models');e['body']['fingerprint']='fabricated'
    p=tmp_path/'journal.jsonl';p.write_text('\n'.join(json.dumps(e) for e in events)+'\n')
    with pytest.raises(ValueError):ANALYZE(p)
