"""Synthetic statistics and tamper rejection; never live performance evidence."""
import json
from pathlib import Path
import runpy
from types import SimpleNamespace
import pytest

MODULE=runpy.run_path('scripts/phase7_analyze_controlled_e2e.py')


def blocks(adaptive=100):
    return {(load,cache,b):{p:SimpleNamespace(e2e_ms=v) for p,v in
        [('S0_NATIVE',110),('S1_REPEAT_CACHE',90),('ADAPTIVE',adaptive)]}
        for load in ('LOW','MEDIUM') for cache in ('EMPTY','READY') for b in range(30)}


def test_actual_comparators_must_both_improve():
    contrast=MODULE['contrast']
    assert contrast(blocks(),'S0_NATIVE')['within_session_benefit']
    assert not contrast(blocks(),'S1_REPEAT_CACHE')['within_session_benefit']
    assert contrast(blocks(80),'S1_REPEAT_CACHE')['within_session_benefit']
    assert not contrast({},'S0_NATIVE')['qualified']


def test_single_bad_batch_rejects_stable_benefit():
    rows=blocks(80)
    for (load,cache,b),policies in rows.items():
        if b>=20:policies['ADAPTIVE'].e2e_ms=95
    result=MODULE['contrast'](rows,'S1_REPEAT_CACHE')
    assert result['adaptive_minus_comparator_mean_ms']<0
    assert not result['within_session_benefit']


def test_missing_and_unknown_protocol_rejected(tmp_path):
    p=tmp_path/'journal.jsonl';p.write_text('')
    with pytest.raises(ValueError,match='missing protocol'):MODULE['analyze'](p)
    p.write_text(json.dumps({'kind':'protocol','body':{'protocol':'fabricated'}})+'\n')
    with pytest.raises(ValueError,match='unknown protocol'):MODULE['analyze'](p)


def test_background_protocol_cannot_extend_snapshot_freshness(tmp_path):
    p=tmp_path/'journal.jsonl'
    body={'protocol':'controlled-e2e-v3','load_protocol':'cpu-activity-v3',
          'cpu_measurement':'cpu-times-instance-v1','snapshot_mode':'latest',
          'snapshot_ttl_ms':5000,'background_interval_s':.2}
    p.write_text(json.dumps({'kind':'protocol','body':body})+'\n')
    with pytest.raises(ValueError,match='background sampling protocol'):MODULE['analyze'](p)
