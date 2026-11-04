"""Synthetic accounting checks, not performance evidence."""
import importlib.util
import json
from pathlib import Path

import pytest

spec = importlib.util.spec_from_file_location('cost_diagnosis',
    Path(__file__).resolve().parents[2]/'scripts/phase7_diagnose_batch_costs.py')
diagnosis = importlib.util.module_from_spec(spec)
spec.loader.exec_module(diagnosis)


def test_incomplete_study_is_not_diagnosed_as_complete(tmp_path):
    path = tmp_path/'journal.jsonl'
    path.write_text(json.dumps({'kind':'protocol','body':{}})+'\n')
    with pytest.raises(ValueError, match='completed frozen study'):
        diagnosis.diagnose(path)


def test_stage_accounting_rejects_unaccounted_service_cost(tmp_path):
    events = [dict(kind='batch_assignment', body=dict(batch_id='test', phase='TRAIN',
        load='LOW', size=2, policy='S0_NATIVE', block=0)),
        dict(kind='batch_selected', body=dict(selection_ns=2_000_000))]
    for index in range(2):
        events.append(dict(kind='request', body=dict(index=index, elapsed_ns=10_000_000,
            workflow={'trace':{'events':[{'state':'SUCCEEDED','elapsed_ms':8.}]},
                      'run':{'execution':{'attempts':[{'execution':{'metrics':{'client_wall_ms':3.}}}]}}})))
    events.append(dict(kind='batch_closed', body=dict(batch_id='test', service_ms=21.)))
    path = tmp_path/'journal.jsonl'
    path.write_text(''.join(json.dumps(e)+'\n' for e in events))
    with pytest.raises(ValueError, match='accounting does not reconcile'):
        diagnosis.diagnose(path)


def test_descriptive_tail_does_not_drop_outlier():
    result = diagnosis.describe([1., 2., 3., 100.])
    assert result['mean'] == 26.5
    assert result['median'] == 2.5
    assert result['max'] == 100.
    assert result['count'] == 4
