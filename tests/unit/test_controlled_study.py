"""Synthetic controlled-study tests. Never label these as live evidence."""
import runpy
import time
import pytest
from tsdb_optimizer.experiments.controlled_policy import train, fingerprint
from tsdb_optimizer.experiments.cpu_load import CpuLoad
from tsdb_optimizer.strategies import build_default_registry
from test_exploration import trial


def experience():
    e=trial().experience
    before=e.before.model_copy(update={'client_host':e.before.client_host.model_copy(update={'cpu_measurement':'cpu-times-instance-v1'})})
    return e.model_copy(update={'before':before,'context':e.context.model_copy(update={
        'load_protocol':'cpu-activity-v3','cache_condition':'controlled-empty'})})


def test_training_uses_real_admission_and_keeps_deployment_locked():
    e=experience();models=train([('EMPTY',e)],build_default_registry())
    assert sum(a.n for a in models['EMPTY'].snapshot().arms.values())==1
    assert models['EMPTY'].online_advice() is None
    assert fingerprint(models)==fingerprint(models)
    with pytest.raises(ValueError):train([('EMPTY',e),('EMPTY',e)],build_default_registry())
    with pytest.raises(ValueError):train([('READY',e)],build_default_registry())
    bad=e.model_copy(update={'sample_status':type(e.sample_status).LOW_CONFIDENCE,'reward':None})
    with pytest.raises(ValueError):train([('EMPTY',bad)],build_default_registry())


def test_cpu_workers_self_expire_and_cleanup():
    load=CpuLoad(True,duration_s=.2);load.count=1
    with load:
        time.sleep(.3)
        with pytest.raises(RuntimeError,match='expired'):load.check()
    assert all(not p.is_alive() for p in load.processes)
    for kwargs in ({'duration_s':31},{'target_percent':90}):
        with pytest.raises(ValueError):CpuLoad(True,**kwargs)


def test_reference_comparison_refuses_incomplete_or_different_results():
    from types import SimpleNamespace as N
    eq=runpy.run_path('scripts/phase7_controlled_e2e.py')['equal']
    def result(**changes):return N(**{'columns':['count(s)'],'row_count':1,'rows_preview':[[10]],'preview_truncated':False,**changes})
    assert eq(result(),result())
    assert not eq(result(preview_truncated=True),result())
    assert not eq(result(rows_preview=[[11]]),result())


def test_policy_choice_still_passes_registry_validation():
    from phase4_support import TEST_SQL
    from tsdb_optimizer.agents import QueryAnalysisAgent
    Engine=runpy.run_path('scripts/phase7_controlled_e2e.py')['ExperimentalEngine']
    registry=build_default_registry(cache_epoch='TEST_ONLY_IMMUTABLE')
    engine=Engine(registry);engine.policy='UNREGISTERED'
    # Existing engine rejects/falls back rather than executing arbitrary advice.
    d=engine.decide(query_id='TEST_ONLY',original_sql=TEST_SQL,mode='adaptive',
        analysis=QueryAnalysisAgent().analyze(TEST_SQL,query_id='TEST_ONLY'),server_version='2.0.6',database_model='tree')
    assert d.selected_strategy_id=='S0_NATIVE'


def test_experimental_engine_preserves_baseline_safety_downgrade():
    from phase4_support import TEST_SQL
    from tsdb_optimizer.agents import QueryAnalysisAgent
    Engine=runpy.run_path('scripts/phase7_controlled_e2e.py')['ExperimentalEngine']
    engine=Engine(build_default_registry(cache_epoch='TEST_ONLY_IMMUTABLE'))
    engine.policy='ADAPTIVE'  # No model: baseline must not even consult it.
    result=engine.decide(query_id='TEST_ONLY',original_sql=TEST_SQL,mode='baseline',
        analysis=QueryAnalysisAgent().analyze(TEST_SQL,query_id='TEST_ONLY'),server_version='2.0.6',database_model='tree')
    assert result.selected_strategy_id==engine.chosen=='S0_NATIVE'


def test_explicit_native_first_visit_makes_repeat_arm_eligible():
    from phase4_support import TEST_SQL
    from tsdb_optimizer.agents import QueryAnalysisAgent
    Engine=runpy.run_path('scripts/phase7_controlled_e2e.py')['ExperimentalEngine']
    engine=Engine(build_default_registry(cache_epoch='TEST_ONLY_IMMUTABLE'))
    engine.policy='S1_REPEAT_CACHE'
    def decide(mode):
        return engine.decide(query_id='TEST_ONLY',original_sql=TEST_SQL,mode=mode,
            analysis=QueryAnalysisAgent().analyze(TEST_SQL,query_id='TEST_ONLY'),server_version='2.0.6',database_model='tree')
    first=decide('baseline')
    second=decide('adaptive')
    assert first.selected_strategy_id=='S0_NATIVE' and first.query_occurrence==1
    assert second.selected_strategy_id=='S1_REPEAT_CACHE' and second.query_occurrence==2


def test_frozen_model_does_not_retain_training_workflows():
    release=runpy.run_path('scripts/phase7_controlled_e2e.py')['release_training_evidence']
    e=experience();rows=[('EMPTY',e)];models=train(rows,build_default_registry())
    digest=fingerprint(models);completed={'synthetic-workflow':object()}
    result=release(rows,completed,models)
    assert not rows and not completed
    assert fingerprint(models)==digest==result['model_fingerprint']
    assert result['training_rows']==result['completed_workflows']==1
    assert result['gc_collected']>=0 and result['elapsed_ms']>=0
