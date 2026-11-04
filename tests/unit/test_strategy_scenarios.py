"""Synthetic difference/gate checks, not performance evidence."""
from types import SimpleNamespace as N
import pytest
from tsdb_optimizer.experiments.scenarios import PLAN, scenario_report


def blocks():
    def row(value,load,hit):return N(e2e_ms=value,experience=N(context=N(load_bucket=load,result_source='CACHE_HIT' if hit else 'SERVER')))
    return {(load,cache,b):{p:row(v,load,cache=='READY' and p!='S0_NATIVE')
        for p,v in [('S0_NATIVE',100),('S1_REPEAT_CACHE',120 if cache=='EMPTY' else 80),('ADAPTIVE',90)]}
        for load in ('LOW','MEDIUM') for cache in ('EMPTY','READY') for b in range(36)}


def test_actual_scenario_crossover_and_not_deployment():
    r=scenario_report(blocks(),PLAN)
    assert not r['deployment_ready']
    assert r['cells']['LOW/EMPTY']['conclusion']=='S0_FASTER_WITHIN_SESSION'
    assert r['cells']['MEDIUM/READY']['conclusion']=='S1_FASTER_WITHIN_SESSION'
    assert r['cells']['LOW/READY']['cache_hit_counts']['S1_REPEAT_CACHE']==36


def test_insufficient_actual_load_and_inconsistent_repeats_stay_unknown():
    b=blocks()
    for n in range(7):b[('MEDIUM','READY',n)]['ADAPTIVE'].experience.context.load_bucket='LOW'
    assert not scenario_report(b,PLAN)['cells']['MEDIUM/READY']['sample_gate']
    for n in range(24,36):b[('LOW','EMPTY',n)]['S1_REPEAT_CACHE'].e2e_ms=99
    assert scenario_report(b,PLAN)['cells']['LOW/EMPTY']['conclusion']=='NO_STABLE_DIFFERENCE'
    with pytest.raises(ValueError):scenario_report(b,{**PLAN,'confidence':.95})
