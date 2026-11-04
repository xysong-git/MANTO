"""Explicit phase-7 qualification study; no production model deployment.
Co-tenant CPU authorization is mandatory. Single query connection throughout.
"""
from contextlib import contextmanager
import argparse
from collections import Counter
import hashlib
import gc
import json
from pathlib import Path
import random
import time

from tsdb_optimizer.adapters.iotdb.tree_adapter import IoTDBTreeAdapter
from tsdb_optimizer.agents import PerformanceMonitoringAgent
from tsdb_optimizer.bandit.context import encode
from tsdb_optimizer.bandit.completed_request import CompletedRequest, E2E_REWARD_VERSION
from tsdb_optimizer.bandit.workload_audit import audit_source_reference, verify_import_certificate
from tsdb_optimizer.config import load_settings
from tsdb_optimizer.decision import DecisionEngine
from tsdb_optimizer.decision.models import AdaptiveAdvice
from tsdb_optimizer.execution import DecisionExecutionService
from tsdb_optimizer.experiments.controlled_policy import train, fingerprint
from tsdb_optimizer.experiments.cpu_load import CpuLoad
from tsdb_optimizer.experiments.supervisor import supervise
from tsdb_optimizer.monitoring.local_db import VerifiedLocalDbHostSampler
from tsdb_optimizer.monitoring.system import SystemMonitor, PsutilSystemSampler
from tsdb_optimizer.orchestration.orchestrator import Orchestrator
from tsdb_optimizer.schemas.common import utc_now
from tsdb_optimizer.schemas.learning import LearningContext, load_identity
from tsdb_optimizer.storage import SQLiteStore
from tsdb_optimizer.strategies import build_default_registry
from tsdb_optimizer.sql_guard import validate_readonly_sql, sql_sha256

ROOT=Path(__file__).resolve().parents[1]


def equal(a,b):
    return (a.columns==b.columns and a.row_count==b.row_count and a.rows_preview==b.rows_preview
            and not a.preview_truncated and not b.preview_truncated)


class CheckedMonitor(PerformanceMonitoringAgent):
    reference=None
    last_before=None
    def sample_once(self):
        if self.snapshot_mode == "latest":
            return super().sample_once()
        super().sample_once();time.sleep(.2)
        result=super().sample_once();self.last_before=result
        return result
    def record_execution(self, execution, **kwargs):
        r=super().record_execution(execution,**kwargs)
        if self.reference is None:raise ValueError('reference required')
        return r.model_copy(update={'runtime_result_check':'MATCHED' if equal(execution,self.reference) else 'MISMATCHED'})


def wait_for_monitor(monitor, timeout_s=2):
    begin=time.monotonic()
    while True:
        sample=monitor.latest_sample();at=utc_now()
        if sample is not None and all(s.is_usable(now=at) and s.is_fresh(now=at,max_age_ms=250)
                for s in (sample.client_host,sample.db_host)):
            return {'wait_ms':(time.monotonic()-begin)*1000,'observed_at':at.isoformat(),
                    'sample':sample.model_dump(mode='json',round_trip=True)}
        if time.monotonic()-begin>=timeout_s:raise RuntimeError('monitor readiness deadline exceeded')
        time.sleep(.02)


@contextmanager
def round_load(enabled, monitor=None, target_percent=60, *, profile='legacy'):
    # Stop sampling before worker creation (required by the legacy fork path).
    # The explicit spawn profile also isolates live adapter descriptors.
    if monitor is not None:
        monitor.stop()
        if monitor.is_monitoring:raise RuntimeError('cannot fork with live monitor')
    options={} if profile=='legacy' else {'profile':profile}
    with CpuLoad(enabled,target_percent=target_percent,**options) as stress:
        if monitor is not None:monitor.start()
        yield stress


class ExperimentalEngine(DecisionEngine):
    policy='S0_NATIVE'
    models=None
    cache='EMPTY'
    model_fingerprint=None
    chosen=None
    def decide(self,**kwargs):
        if str(kwargs.get('mode'))=='baseline':
            self.chosen='S0_NATIVE'
            return super().decide(**kwargs)  # preserve Orchestrator safety downgrade
        strategy=self.policy
        if strategy=='ADAPTIVE':
            strategy=self.models[self.cache].choose_registered(
                encode(kwargs['analysis'].features,kwargs['system_state'],at=utc_now()),self.registry).split('@')[0]
        self.chosen=strategy
        kwargs['mode']='adaptive'
        kwargs['adaptive_advice']=AdaptiveAdvice(strategy_id=strategy,
            model_version='EXPERIMENT_ONLY:'+str(self.model_fingerprint or 'randomized'),
            sample_count=30 if self.policy=='ADAPTIVE' else 0,
            reason='Predeclared controlled experiment; production deployment remains disabled')
        return super().decide(**kwargs)


def release_training_evidence(training, completed, models):
    """Training is journaled and frozen; do not retain full results in holdout."""
    before=fingerprint(models);start=time.perf_counter()
    counts={'training_rows':len(training),'completed_workflows':len(completed)}
    training.clear();completed.clear()
    collected=gc.collect()
    if fingerprint(models)!=before:raise RuntimeError('release changed frozen model')
    return {**counts,'gc_collected':collected,'elapsed_ms':(time.perf_counter()-start)*1000,
            'model_fingerprint':before}


def run(args,*,_signal=None):
    if not args.immutable_epoch_confirmed or not args.cpu_cotenant_authorized:
        raise ValueError('immutable epoch and CPU co-tenant authorization required')
    if not 1<=args.train_rounds<=8 or not 1<=args.test_blocks<=40:
        raise ValueError('bounded study counts required')
    background=getattr(args,'background_snapshots',False)
    wal=getattr(args,'wal_full',False)
    cpu_target=getattr(args,'cpu_target_percent',60)
    if not 50<=cpu_target<=70:raise ValueError('CPU target outside authorized range')
    if wal and not background:raise ValueError('WAL qualification requires background snapshot protocol')
    objective=getattr(args,'e2e_reward',False)
    fresh_wait=getattr(args,'fresh_snapshot_wait',False)
    ambient=getattr(args,'ambient_only',False)
    release=getattr(args,'release_training_evidence',False)
    if release and not ambient:raise ValueError('release study is ambient-only')
    if ambient and not fresh_wait:raise ValueError('ambient study requires bounded fresh E2E protocol')
    loads=('LOW',) if ambient else ('LOW','MEDIUM')
    if fresh_wait and not objective:raise ValueError('bounded snapshot wait requires E2E reward')
    scale=getattr(args,'scale','small_scale')
    if scale not in ('small_scale','medium_scale','large_scale'):raise ValueError('unknown workload scale')
    if (objective or scale!='small_scale') and not wal:raise ValueError('new objective/scales require WAL background protocol')
    test_line=getattr(args,'test_query_line',2)
    if not 2<=test_line<=1000:raise ValueError('invalid held-out query line')
    settings=load_settings()
    sampler=VerifiedLocalDbHostSampler(host=settings.iotdb_host,port=settings.iotdb_port,pid=args.local_db_pid,stale_after_ms=500 if background else 5000)
    path=ROOT/'query/heavy_scale'/scale/'q03_count_aggregation.sql'
    raw=path.read_bytes();lines=raw.decode().splitlines()
    queries={phase:validate_readonly_sql(lines[index]) for phase,index in [('train',0),('test',test_line-1)]}
    manifest=json.loads((ROOT/'query/heavy_scale/manifest.json').read_text())
    cert=verify_import_certificate(ROOT,manifest['import_verification'],f'{settings.iotdb_host}:{settings.iotdb_port}',args.epoch)
    if cert is None:raise ValueError('dataset import certification required')
    args.output.mkdir(parents=True,exist_ok=False)
    sources=list((ROOT/'src/tsdb_optimizer').rglob('*.py'))+[Path(__file__).resolve(),ROOT/'scripts/phase7_analyze_controlled_e2e.py',ROOT/'docs/PHASE7_E2E_REWARD_SCENARIOS.md']
    (args.output/'implementation.json').write_text(json.dumps({str(p.relative_to(ROOT)):hashlib.sha256(p.read_bytes()).hexdigest() for p in sources},indent=2)+'\n')
    journal=(args.output/'journal.jsonl').open('x')
    def emit(kind,body):
        if _signal and kind=='round_start':_signal(kind)
        journal.write(json.dumps({'kind':kind,'body':body},allow_nan=False)+'\n');journal.flush()
        if _signal and kind=='round_complete':_signal(kind)
        if kind in ('round_complete','training_gate','complete'):
            print(json.dumps({'kind':kind,**{k:v for k,v in body.items() if k!='cpu_control'}}),flush=True)
    emit('protocol',{'protocol':'controlled-e2e-v9' if release else 'controlled-e2e-v8' if ambient else 'controlled-e2e-v7' if fresh_wait else 'controlled-e2e-v6' if objective else ('controlled-e2e-v5' if wal else ('controlled-e2e-v4' if background else 'controlled-e2e-v2')),
        'release_training_evidence':release,
        'study_scope':'AMBIENT_STRATEGY_APPLICABILITY_ONLY' if ambient else 'MULTILOAD_QUALIFICATION',
        'cpu_load_enabled':not ambient,
        'request_snapshot_max_age_ms':250 if fresh_wait else None,'request_snapshot_wait_limit_s':.5 if fresh_wait else 0,
        'reward_version':E2E_REWARD_VERSION if objective else 'negative-log1p-ms-v1',
        'workload_scale':scale,'workload_file':str(path.relative_to(ROOT)),
        'scenario_analysis':{'contrast':'S1 minus S0 per target load/cache','confidence':.99,
            'replicates':4000,'seed':20261105,'minimum_blocks':30,
            'repeats':'three consecutive equal batches; consistent sign required',
            'scope':'conditional request cost; cache preparation separately reported; exploratory across scenarios'},
        'storage_mode':'WAL_FULL_REQUEST_SESSION' if wal else 'LEGACY',
        'sampling_schedule':'fixed-rate-db-first-v1' if background else 'synchronous',
        'snapshot_mode':'latest' if background else 'sample','test_query_line':test_line,
        'background_interval_s':.2 if background else None,'snapshot_ttl_ms':500 if background else 5000,'epoch':args.epoch,'seed':20261102,
        'load_protocol':'cpu-activity-v3','cpu_measurement':'cpu-times-instance-v1','cpu_source':'NATURAL_HOST_ACTIVITY' if ambient else 'EXPLICIT_CO_TENANT_NOT_DATABASE_QUERIES',
        'cpu_setpoint_percent':cpu_target,'cpu_target_percent':[50,70],'cpu_stop_percent':80,'cpu_duration_max_s':30,
        'query_timeout_ms':30000,'round_limit_s':60,'max_query_connections':1,
        'first_occurrence_preparation':'explicit Native Orchestrator request before each phase; excluded from Bandit fitting and E2E comparisons',
        'process_supervisor':_signal is not None,'training_rounds_per_cell':args.train_rounds,
        'training_trial_cap_per_round':20,'test_blocks_per_cell':args.test_blocks,
        'minimum_per_arm_training':30,'minimum_test_blocks':30,
        'benefit_analysis':{'metric':'Orchestrator e2e_ms','contrast':'ADAPTIVE minus actual comparator',
            'bootstrap':'paired blocks, stratified by load/cache','replicates':4000,'confidence':.99,
            'seed':20261104,'required_comparators':['S0_NATIVE','S1_REPEAT_CACHE'],
            'within_session_repeats':'3 consecutive 10-block batches must each have negative mean difference',
            'independent_session_reproduction':False},
        'pilot_repeats':2,'stability_min_samples':20,'stability_fraction':.8,
        'file_sha256':hashlib.sha256(raw).hexdigest(),'train_sql':queries['train'],'test_sql':queries['test'],
        'load_identity':sampler.identity.model_dump(),'import_certificate':cert,
        'end_to_end_definition':'time around Orchestrator.run including parsing, monitoring, decision, full fetch, learning and trace persistence; excludes explicit cache preparation and external audit serialization',
        'production_online_enabled':False})
    adapter=None;monitor=None
    try:
        # Predeclared pilot: independent measured CPU windows, not labels inferred
        # from worker count. A failed pilot stops training and policy claims.
        stable=True
        sampler.sample();time.sleep(.2)
        for repeat in range(2):
            for load in loads:
                emit('round_start',{'phase':'pilot','repeat':repeat,'load':load})
                start=time.monotonic();observed=[]
                with round_load(load=='MEDIUM',monitor if background else None,cpu_target) as stress:
                    time.sleep(3)
                    for _ in range(20):
                        time.sleep(.2);stress.check();s=sampler.sample()
                        if not s.valid or s.memory_percent>90 or s.disk_usage_percent>90 or s.cpu_percent>80:
                            emit('resource_rejected',{'phase':'pilot','sample':s.model_dump(mode='json',round_trip=True)})
                            raise RuntimeError('pilot unsafe/unavailable resource sample')
                        observed.append(s.model_dump(mode='json',round_trip=True))
                    passed=sum((s['cpu_percent']<50 if load=='LOW' else 50<=s['cpu_percent']<=70) for s in observed)>=16
                    stable &= passed
                emit('pilot',{'load':load,'repeat':repeat,'passed':passed,'samples':observed,'cpu_control':stress.samples})
                emit('round_complete',{'phase':'pilot','elapsed_s':time.monotonic()-start})
        emit('stability_gate',{'passed':stable})
        if not stable or args.pilot_only:
            emit('complete',{'qualification':'PILOT_ONLY','stable_loads':stable,'online_ready':False});return
        adapter=IoTDBTreeAdapter(settings)
        doctor=adapter.doctor()
        if not doctor.ok or doctor.server.version!='2.0.6':raise RuntimeError('verified IoTDB required')
        store=SQLiteStore(args.output/'study.sqlite',wal_full=wal);store.initialize()
        with store._connect() as connection:
            emit('storage_configuration',{'sqlite_version':__import__('sqlite3').sqlite_version,
                'journal_mode':connection.execute('PRAGMA journal_mode').fetchone()[0],
                'synchronous':connection.execute('PRAGMA synchronous').fetchone()[0]})
        registry=build_default_registry(cache_epoch=args.epoch)
        engine=ExperimentalEngine(registry)
        monitor=CheckedMonitor(registry=registry,snapshot_mode='latest' if background else 'sample',
            latest_max_age_ms=250 if fresh_wait else None,latest_wait_timeout_s=.5 if fresh_wait else 0,
            monitor=SystemMonitor(db_host_sampler=sampler,interval_s=.2,
                client_sampler=PsutilSystemSampler(stale_after_ms=500 if background else 5000)))
        if background:
            monitor.start()
            # Initialization is explicit setup, not per-request waiting.
            time.sleep(.5)
        base_context=LearningContext(server_id=f'{settings.iotdb_host}:{settings.iotdb_port}',
            server_version='2.0.6',database_model='tree',dataset_epoch=args.epoch,
            result_rows=1,cache_condition='controlled-empty',load_source='db_host',load_protocol='cpu-activity-v3')
        orchestrator=Orchestrator(adapter=adapter,store=store,registry=registry,engine=engine,
            monitor=monitor,context=base_context,preview_rows=1000)
        preparation=DecisionExecutionService(adapter=adapter,store=store,registry=registry,
            server_version='2.0.6',database_model='tree',preview_rows=1000)
        references={}
        for phase,sql in queries.items():
            ref=preparation.run(sql,mode='baseline',timeout_ms=30000,source='study-reference').execution.attempts[-1].execution
            source_audit=audit_source_reference(scale,Path(manifest['datasets'][scale]['source_directory']),sql,ref.model_dump(mode='json'))
            if source_audit['status']!='PASS':raise RuntimeError('source reference mismatch')
            references[phase]=ref;emit('reference',{'phase':phase,'audit':source_audit,'result':ref.model_dump(mode='json',round_trip=True)})
        def prepare(cache,sql,ref):
            adapter.result_cache.clear()
            if cache=='READY':
                run=preparation.run(sql,mode='adaptive',timeout_ms=30000,source='study-cache-preparation',
                    adaptive_advice=AdaptiveAdvice(strategy_id='S1_REPEAT_CACHE',model_version='EXPERIMENT_PREP',sample_count=0,reason='Explicit pre-action cache preparation'))
                r=run.execution.attempts[-1].execution
                emit('cache_preparation',r.model_dump(mode='json',round_trip=True))
                if r.served_from_cache or r.strategy_id!='S1_REPEAT_CACHE' or not equal(r,ref):raise RuntimeError('cache preparation invalid')
        completed_training={}
        def execute(phase,load,cache,policy,assignment):
            sql=queries[phase];ref=references[phase]
            prepare(cache,sql,ref)
            monitor.reference=ref;engine.policy=policy;engine.cache=cache
            orchestrator.context=base_context.model_copy(update={'cache_condition':'controlled-'+cache.lower()})
            emit('assignment',assignment)
            request_start=utc_now();begin_ns=time.perf_counter_ns()
            result=orchestrator.run(sql,mode='adaptive',timeout_ms=30000,source='controlled-e2e:'+phase)
            elapsed_ns=time.perf_counter_ns()-begin_ns;request_finish=utc_now()
            e2e_ms=elapsed_ns/1_000_000
            if result.run is None:raise RuntimeError('workflow has no execution')
            actual=result.run.execution.attempts[-1].execution
            e=orchestrator.learner.repository.get(actual.execution_id)
            receipt=None
            if objective and e is not None:
                try:
                    receipt=CompletedRequest.capture(e,result,started_at=request_start,
                        finished_at=request_finish,elapsed_ns=elapsed_ns)
                except ValueError as exc:
                    emit('rejected_completed_request',{'reason':str(exc),'assignment':assignment,
                        'e2e_ms':e2e_ms,'workflow':result.model_dump(mode='json',round_trip=True),
                        'experience':e.model_dump(mode='json',round_trip=True)})
                    raise
                if phase=='train':completed_training[e.execution_id]=(receipt,result)
            emit('trial',{'phase':phase,'target_load':load,'cache':cache,'policy':policy,'assignment':assignment,
                'e2e_ms':e2e_ms,'workflow':result.model_dump(mode='json',round_trip=True),
                'experience':e.model_dump(mode='json',round_trip=True) if e else None,
                **({'completed_request':receipt.model_dump(mode='json')} if receipt is not None else {})})
            if (result.trace.warnings or result.run.execution.warnings or not e or e.sample_status.value!='VALID'
                or len(result.run.execution.attempts)!=1 or result.run.execution.fallback_used
                or actual.strategy_id!=engine.chosen or not equal(actual,ref)
                or any(f.warnings for f in result.feedback)):
                raise RuntimeError('unqualified execution; no selective filtering or retry')
            return e
        def first_occurrence(phase):
            if background:emit('monitor_ready',{'phase':phase,**wait_for_monitor(monitor)})
            monitor.reference=references[phase];engine.policy='S0_NATIVE'
            result=orchestrator.run(queries[phase],mode='baseline',timeout_ms=30000,source='study-first-occurrence:'+phase)
            emit('first_occurrence',{'phase':phase,'workflow':result.model_dump(mode='json',round_trip=True)})
            if result.run is None or result.trace.warnings or not equal(result.run.execution.attempts[-1].execution,references[phase]):
                raise RuntimeError('first occurrence preparation failed')
            adapter.result_cache.clear()
        first_occurrence('train')
        cells=[(load,cache) for load in loads for cache in ('EMPTY','READY')]
        training=[];counts=Counter();streams={cell:random.Random(20261102+i) for i,cell in enumerate(cells)}
        indexes=Counter();round_rng=random.Random(20261102)
        for repeat in range(args.train_rounds):
            order=cells.copy();round_rng.shuffle(order)
            for load,cache in order:
                emit('round_start',{'phase':'train','repeat':repeat,'load':load,'cache':cache})
                start=time.monotonic()
                with round_load(load=='MEDIUM',monitor if background else None,cpu_target) as stress:
                    time.sleep(3)
                    for _ in range(20):
                        if time.monotonic()-start>=20:break
                        stress.check();idx=indexes[(load,cache)];indexes[(load,cache)]+=1
                        policy=streams[(load,cache)].choice(['S0_NATIVE','S1_REPEAT_CACHE'])
                        a={'phase':'train','load':load,'cache':cache,'index':idx,'policy':policy,'propensity':.5}
                        e=execute('train',load,cache,policy,a);training.append((cache,e))
                        counts[(e.context.load_bucket,cache,e.strategy_id)]+=1
                        stress.check()
                emit('round_complete',{'phase':'train','load':load,'cache':cache,'elapsed_s':time.monotonic()-start,'cpu_control':stress.samples})
        gate=all(counts[(l,c,s)]>=30 for l,c in cells for s in ('S0_NATIVE','S1_REPEAT_CACHE'))
        emit('training_gate',{'passed':gate,'counts':{'/'.join(k):v for k,v in counts.items()}})
        if not gate:
            emit('complete',{'qualification':'INSUFFICIENT_TRAINING','online_ready':False});return
        engine.models=train(training,registry,completed=completed_training if objective else None);engine.model_fingerprint=fingerprint(engine.models)
        emit('frozen_models',{'fingerprint':engine.model_fingerprint,'models':{k:v.snapshot().model_dump(mode='json') for k,v in engine.models.items()}})
        if release:emit('training_memory_released',release_training_evidence(training,completed_training,engine.models))
        # Test on a distinct query, never used for training. Keep all outcomes; no
        # fitting or tuning after this point. L1 logging is part of every policy.
        first_occurrence('test')
        test_rng=random.Random(20261103)
        for block in range(args.test_blocks):
            order=cells.copy();test_rng.shuffle(order)
            for load,cache in order:
                emit('round_start',{'phase':'test','block':block,'load':load,'cache':cache})
                start=time.monotonic()
                with round_load(load=='MEDIUM',monitor if background else None,cpu_target) as stress:
                    time.sleep(3)
                    policies=['S0_NATIVE','S1_REPEAT_CACHE','ADAPTIVE'];test_rng.shuffle(policies)
                    for position,policy in enumerate(policies):
                        stress.check()
                        if time.monotonic()-start>=20:raise RuntimeError('test block budget exhausted')
                        execute('test',load,cache,policy,{'phase':'test','load':load,'cache':cache,'block':block,'position':position,'policy':policy})
                        stress.check()
                if fingerprint(engine.models)!=engine.model_fingerprint:raise RuntimeError('frozen model changed')
                emit('round_complete',{'phase':'test','block':block,'load':load,'cache':cache,'elapsed_s':time.monotonic()-start,'cpu_control':stress.samples})
        emit('complete',{'qualification':'MEASURED_NOT_YET_ACCEPTED','model_frozen':True,'online_ready':False})
    except BaseException as exc:
        emit('aborted',{'error_type':type(exc).__name__,
            'cpu_control':stress.samples if 'stress' in locals() else [],
            'cpu_error':stress.error if 'stress' in locals() else None});raise
    finally:
        if monitor and background:
            monitor.stop()
            if monitor.is_monitoring:raise RuntimeError("background monitor termination unconfirmed")
        if adapter:adapter.close()
        journal.close()


if __name__=='__main__':
    p=argparse.ArgumentParser();p.add_argument('--output',type=Path,required=True)
    p.add_argument('--epoch',required=True);p.add_argument('--local-db-pid',type=int,required=True)
    p.add_argument('--immutable-epoch-confirmed',action='store_true');p.add_argument('--cpu-cotenant-authorized',action='store_true')
    p.add_argument('--pilot-only',action='store_true');p.add_argument('--train-rounds',type=int,default=6)
    p.add_argument('--cpu-target-percent',type=float,default=60)
    p.add_argument('--wal-full',action='store_true')
    p.add_argument('--e2e-reward',action='store_true')
    p.add_argument('--fresh-snapshot-wait',action='store_true')
    p.add_argument('--ambient-only',action='store_true')
    p.add_argument('--release-training-evidence',action='store_true')
    p.add_argument('--scale',choices=['small_scale','medium_scale','large_scale'],default='small_scale')
    p.add_argument('--background-snapshots',action='store_true')
    p.add_argument('--test-query-line',type=int,default=2)
    p.add_argument('--test-blocks',type=int,default=30)
    supervise(run,p.parse_args())
