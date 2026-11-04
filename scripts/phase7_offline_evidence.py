"""Offline readiness, simulation, optional independent real read-only benchmark.

Never attaches LinUCB/LLM to the Orchestrator. Real data is collected by the
already audited dedicated benchmark, not by executing alternative online plans.
"""
import argparse
import asyncio
from collections import defaultdict
from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
import random
import statistics

from tsdb_optimizer.bandit import Config, OfflineLinUCB, OfflineModelStore
from tsdb_optimizer.bandit.model import AdmissionError
from tsdb_optimizer.errors import StrategyRegistryError
from tsdb_optimizer.bandit.context import DIMENSIONS, partition_key
from tsdb_optimizer.bandit.evaluation import audit, frozen_evaluate
from tsdb_optimizer.bandit.settings import load_settings as load_experiment_settings
from tsdb_optimizer.config import load_settings
from tsdb_optimizer.decision import DecisionEngine
from tsdb_optimizer.decision.llm_advisor import SafeLLMAdvisor
from tsdb_optimizer.schemas.learning import Experience
from tsdb_optimizer.strategies import build_default_registry

ROOT = Path(__file__).resolve().parents[1]


def simulation(config,seed):
    start=datetime(2026,1,1,tzinfo=timezone.utc)
    model=OfflineLinUCB(['SIM_BASE@1','SIM_ALT@1'],baseline_arm='SIM_BASE@1',partition='SYNTHETIC_ENV_V1',
        domain='SYNTHETIC',training_until=start+timedelta(days=1),config=config)
    schedule=[(a,cpu) for a in ['SIM_BASE@1','SIM_ALT@1'] for cpu in [.1,.9] for _ in range(60)]
    random.Random(seed).shuffle(schedule)
    observations=[]
    for i,(arm,cpu) in enumerate(schedule):
        x=[0.]*len(DIMENSIONS);x[0]=1.;x[5]=cpu
        latency=1. if (arm=='SIM_BASE@1')==(cpu < .5) else 1000.
        model.update_synthetic(execution_id=f'simulation-{i}',arm=arm,x=x,latency_ms=latency,
                               finished_at=start+timedelta(seconds=i))
        observations.append({'execution_id':f'simulation-{i}','arm':arm,'client_cpu_fraction':cpu,
                             'observed_latency_ms':latency})
    choices={}
    for cpu in [.1,.9]:
        x=[0.]*len(DIMENSIONS);x[0]=1.;x[5]=cpu
        choices[str(cpu)]=model.choose(x,model.snapshot().arms)
    return model, {'evidence_type':'SYNTHETIC_ONLY','seed':seed,'observations':observations,
                   'heldout_context_choices':choices,'online_ready':False}


async def llm_comparison(registry):
    sql='SELECT s_1666, s_1766 FROM root.db800.g_0.d_0, root.db800.g_0.d_1 WHERE time = 1640966450000'
    decision=DecisionEngine(registry).decide(query_id='offline-advisor-comparison',original_sql=sql,
        mode='rule_based',server_version='2.0.6',database_model='tree')
    async def scripted(payload): return '{"strategy_id":"S0_NATIVE"}'
    async def invalid(payload): return '{"strategy_id":"UNREGISTERED","sql":"SELECT unsafe"}'
    async def slow(payload): await asyncio.sleep(.1); return '{}'
    results={}
    for name,enabled,transport in [('off',False,None),('on_unconfigured',True,None),
            ('on_scripted',True,scripted),('on_invalid',True,invalid),('on_timeout',True,slow)]:
        advisor=SafeLLMAdvisor(registry,enabled=enabled,transport=transport,timeout_ms=5,provider_version='TEST_ONLY')
        rows=[]
        for _ in range(10):
            r=await advisor.recommend(decision)
            rows.append(r.model_dump(mode='json'))
        advised = [r for r in rows if r['advice'] is not None]
        results[name]={'median_adapter_ms':statistics.median(r['elapsed_ms'] for r in rows),
            'schema_valid_advice_count':len(advised), 'rule_fallback_count':len(rows)-len(advised),
            'rule_agreement_on_advice':(sum(r['advice']['strategy_id']==decision.selected_strategy_id for r in advised)/len(advised)
                                        if advised else None), 'results':rows}
    for scenario in results.values():
        scenario['median_added_ms_vs_off']=scenario['median_adapter_ms']-results['off']['median_adapter_ms']
    return {'evidence_type':'LOCAL_INTERFACE_TEST_NOT_REAL_LLM', 'scenarios':results,
            'real_llm_quality':'BLOCKED_NO_PROVIDER', 'real_llm_latency_ms':None,
            'database_execution_count':0,'production_speedup':None}


def main():
    p=argparse.ArgumentParser()
    p.add_argument('--output',type=Path,required=True)
    p.add_argument('--model-store',type=Path,required=True)
    p.add_argument('--benchmark',action='store_true',help='explicit new read-only controlled benchmark')
    args=p.parse_args()
    settings=load_experiment_settings()
    config=Config.model_validate({k:getattr(settings,k) for k in Config.model_fields})
    historic=json.loads((ROOT/'docs/evidence/phase5_learning_fixes_2026-11-02.json').read_text())
    experiences=[Experience.model_validate(e) for e in historic['experiences']]
    registry=build_default_registry()
    readiness=audit(experiences,registry,config=config)
    readiness['configured_sqlite_exists']=load_settings().sqlite_path.exists()
    readiness['strategy_versions']={e.spec.strategy_id:e.spec.version for e in registry.enabled_entries()}
    sim, sim_report=simulation(config,settings.seed)
    store=OfflineModelStore(args.model_store)
    snap=sim.snapshot()
    from tsdb_optimizer.bandit.store import model_key
    previous=store.current(model_key(snap))
    sim_key=store.publish(snap,expected_head=previous.model_version if previous else None)
    sim_report['model_version']=store.current(sim_key).model_version
    models={}
    groups=defaultdict(list)
    for e in experiences: groups[partition_key(e)].append(e)
    cutoff=max(e.record.finished_at for e in experiences)
    for key, rows in groups.items():
        arms=sorted({f'{e.strategy_id}@{e.strategy_version}' for e in rows})
        if len(arms)<2 or 'S0_NATIVE@0.1.0' not in arms: continue
        m=OfflineLinUCB(arms,baseline_arm='S0_NATIVE@0.1.0',partition=key,
                       training_until=cutoff,config=config)
        for e in rows:
            try: m.update_experience(e,registry)
            except (AdmissionError, StrategyRegistryError):
                continue  # expected rejections already recorded by readiness audit
        snapshot=m.snapshot(); old=store.current(model_key(snapshot))
        store.publish(snapshot,expected_head=old.model_version if old else None)
        models[key]=m
    report={'schema_version':1,'readiness':readiness,'config':settings.model_dump(mode='json'),
        'dimensions':list(DIMENSIONS),'simulation':sim_report,
        'llm_comparison':asyncio.run(llm_comparison(registry)),
        'independent_benchmark':None,'online_enabled':False}
    if args.benchmark:
        from phase5_learning_evidence import benchmark
        # Same caller-declared static dataset epoch; independent execution IDs and future times.
        fresh=benchmark(epoch=historic['dataset_epoch'],repeats=4)
        fresh_rows=[Experience.model_validate(e) for e in fresh['experiences']]
        assert not ({e.execution_id for e in experiences}&{e.execution_id for e in fresh_rows})
        assert min(e.record.started_at for e in fresh_rows)>cutoff
        evaluation={key:frozen_evaluate(m,fresh_rows,registry) for key,m in models.items()}
        report['independent_benchmark']={'training_cutoff':cutoff.isoformat(),'raw':fresh,
            'evaluation':evaluation,'independent_ids_and_future_window':True,
            'note':'Caller declares immutable epoch; no database snapshot isolation claim. '
                   'Factual prediction errors only; no counterfactual reward or causal policy benefit.'}
    args.output.parent.mkdir(parents=True,exist_ok=True)
    args.output.write_text(json.dumps(report,ensure_ascii=False,indent=2,allow_nan=False)+'\n')
    print(json.dumps({'output':str(args.output),'online_ready':False,'simulation_choices':sim_report['heldout_context_choices'],
                      'independent_benchmark':bool(report['independent_benchmark'])}))


if __name__=='__main__': main()
