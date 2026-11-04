"""Explicit staged delayed-feedback experiment. No production policy activation."""
import argparse
from collections import Counter
from datetime import timedelta
import gc
import hashlib
import json
from pathlib import Path
import time

from phase7_controlled_e2e import CheckedMonitor, ExperimentalEngine, equal, round_load, wait_for_monitor
from tsdb_optimizer.adapters.iotdb.tree_adapter import IoTDBTreeAdapter
from tsdb_optimizer.agents import QueryAnalysisAgent
from tsdb_optimizer.agents.learning_feedback import LearningFeedbackAgent
from tsdb_optimizer.bandit.episodes import BatchContract, BatchFeedback, BatchPolicy, batch_family
from tsdb_optimizer.bandit.delayed_store import DelayedBatchStore
from tsdb_optimizer.bandit.workload_audit import audit_source_reference, verify_import_certificate
from tsdb_optimizer.config import load_settings
from tsdb_optimizer.execution import DecisionExecutionService
from tsdb_optimizer.experiments.batches import plan, LOADS, SIZES, POLICIES, SelectionTiming
from tsdb_optimizer.experiments.batch_statistics import BatchStatistics
from tsdb_optimizer.experiments.sequences import SequenceRequest
from tsdb_optimizer.experiments.supervisor import supervise
from tsdb_optimizer.experiments.exclusive_instance import inspect_clients
from tsdb_optimizer.learning.repository import ExperienceRepository
from tsdb_optimizer.monitoring.local_db import VerifiedLocalDbHostSampler
from tsdb_optimizer.monitoring.system import SystemMonitor, PsutilSystemSampler
from tsdb_optimizer.orchestration.orchestrator import Orchestrator
from tsdb_optimizer.schemas.common import new_id, utc_now
from tsdb_optimizer.schemas.learning import LearningContext, digest
from tsdb_optimizer.sql_guard import validate_readonly_sql, sql_sha256
from tsdb_optimizer.storage import SQLiteStore
from tsdb_optimizer.strategies import build_default_registry

ROOT = Path(__file__).resolve().parents[1]


def admit(monitor, emit):
    """Keep the 250ms admission limit; preserve rejected evidence before abort."""
    sample = monitor.sample_once()
    at = utc_now()
    fresh = all(getattr(sample, name).is_usable(now=at, max_age_ms=250)
                for name in ('client_host', 'db_host'))
    safe = all(getattr(sample.db_host, name) is not None and
               getattr(sample.db_host, name) <= limit for name, limit in
               [('cpu_percent', 80), ('memory_percent', 90), ('disk_usage_percent', 90)])
    if not fresh or not safe:
        emit('resource_rejected', {'sample': sample.model_dump(mode='json', round_trip=True),
            'observed_at': at.isoformat(), 'fresh': fresh, 'safe': safe})
        raise RuntimeError('fresh/safe resource admission unavailable')
    return sample, at


def run(args, *, _signal=None):
    if not args.immutable_epoch_confirmed or not args.cpu_cotenant_authorized:
        raise ValueError('immutable epoch and bounded co-tenant authorization required')
    args.output.mkdir(parents=True, exist_ok=False)
    stream = (args.output/'journal.jsonl').open('x')
    def emit(kind, body):
        if _signal and kind == 'round_start': _signal(kind)
        stream.write(json.dumps({'kind':kind,'body':body},allow_nan=False)+'\n'); stream.flush()
        if _signal and kind == 'round_complete': _signal(kind)
        if kind in ('round_complete','training_gate','complete'):
            print(json.dumps({'kind':kind,**{k:v for k,v in body.items() if k != 'cpu_control'}}),flush=True)
    monitor = adapter = None
    try:
        settings = load_settings()
        sampler = VerifiedLocalDbHostSampler(host=settings.iotdb_host,port=settings.iotdb_port,
            pid=args.local_db_pid,stale_after_ms=500)
        def exclusive():
            observation=inspect_clients(args.local_db_pid,settings.iotdb_port)
            emit('instance_occupancy',observation.model_dump(mode='json'))
            if not observation.passed:
                raise RuntimeError('shared IoTDB instance has foreign/unknown clients; no dispatch')
            if observation.connection_count>1:
                raise RuntimeError('study connection budget exceeded; no dispatch')
        file = ROOT/'query/heavy_scale/small_scale/q03_count_aggregation.sql'
        lines = file.read_text().splitlines()
        queries = {'TRAIN':validate_readonly_sql(lines[13]), 'HOLDOUT':validate_readonly_sql(lines[14])}
        analyzer = QueryAnalysisAgent()
        families = {batch_family(analyzer.analyze(sql).features) for sql in queries.values()}
        if len(families) != 1 or queries['TRAIN'] == queries['HOLDOUT']: raise ValueError('invalid independent workload')
        family = families.pop()
        manifest = json.loads((ROOT/'query/heavy_scale/manifest.json').read_text())
        server = f'{settings.iotdb_host}:{settings.iotdb_port}'
        certificate = verify_import_certificate(ROOT,manifest['import_verification'],server,args.epoch)
        if certificate is None: raise ValueError('import certificate required')
        deadline = utc_now()+timedelta(hours=2)
        rounds=45
        protocol = {'protocol':'delayed-batch-v7','epoch':args.epoch,'server_id':server,
            'family_key':family,'queries':queries,'query_lines':{'TRAIN':14,'HOLDOUT':15},
            'l1_comparator':'batch-mean-v1','selection_timing':'selection-segments-v1',
            'request_timing':'store-session-v1','parser_memo':'request-analysis-features-v1',
            'query_file_sha256':hashlib.sha256(file.read_bytes()).hexdigest(),'import_certificate':certificate,
            'training_deadline':deadline.isoformat(),'rounds':rounds,'sizes':[2,8],'loads':list(LOADS),
            'train_seed':20261107,'test_seed':20261108,'analysis_seed':20261109,
            'minimum_per_arm':30,'bootstrap_replicates':4000,'confidence':.99,
            'cpu_target':55,'cpu_stop':80,'memory_stop':90,'disk_stop':90,'cpu_duration_s':30,
            'query_timeout_ms':30000,'round_limit_s':60,'max_query_connections':1,
            'snapshot_ttl_ms':500,'snapshot_max_age_ms':250,'snapshot_wait_s':.5,
            'snapshot_return_max_age_ms':200,'monitor_interval_s':.1,
            'cpu_profile':'staggered-spawn-v2','cpu_start_method':'spawn',
            'cpu_worker_fraction_cap':.60,'cpu_pulse_period_s':.02,
            'foreign_client_guard':'linux-tcp-boundary-v1',
            'statistics_mode':'record_only','reward_version':'negative-batch-service-seconds-v1',
            'load_protocol':'cpu-activity-v3','process_supervisor':_signal is not None,
            'production_online_enabled':False,'load_identity':sampler.identity.model_dump()}
        emit('protocol',protocol)
        exclusive()
        sources = list((ROOT/'src/tsdb_optimizer').rglob('*.py')) + [Path(__file__).resolve(),
            ROOT/'scripts/phase7_analyze_delayed_batch.py',ROOT/'scripts/phase7_controlled_e2e.py',
            ROOT/'scripts/phase7_sequence_e2e.py',ROOT/'docs/PHASE7_DELAYED_BATCH_FEEDBACK.md',
            ROOT/'docs/PHASE7_STAGGERED_VALIDATION.md',ROOT/'docs/PHASE7_F2_F3_V7.md']
        (args.output/'implementation.json').write_text(json.dumps({str(p.relative_to(ROOT)):
            hashlib.sha256(p.read_bytes()).hexdigest() for p in sources},indent=2)+'\n')
        sampler.sample();time.sleep(.2)
        for repeat in range(2):
            for load in LOADS:
                emit('round_start',{'phase':'PILOT','repeat':repeat,'load':load})
                exclusive()
                samples=[]
                with round_load(load=='MEDIUM',target_percent=55,profile='staggered-spawn-v2') as stress:
                    time.sleep(3)
                    for _ in range(20):
                        time.sleep(.2);stress.check();s=sampler.sample()
                        if not s.is_usable() or s.cpu_percent>80 or s.memory_percent>90 or s.disk_usage_percent>90:
                            emit('resource_rejected',s.model_dump(mode='json',round_trip=True))
                            raise RuntimeError('unsafe pilot')
                        samples.append(s.model_dump(mode='json',round_trip=True))
                    passed=sum(s['cpu_percent']<50 if load=='LOW' else 50<=s['cpu_percent']<=70 for s in samples)>=16
                emit('pilot',{'load':load,'repeat':repeat,'samples':samples,'passed':passed,'cpu_control':stress.samples})
                emit('round_complete',{'phase':'PILOT','repeat':repeat,'load':load})
                if not passed: raise RuntimeError('unstable pilot; no training')
        adapter=IoTDBTreeAdapter(settings)
        doctor=adapter.doctor()
        if not doctor.ok or doctor.server.version!='2.0.6':raise ValueError('verified IoTDB required')
        store=SQLiteStore(args.output/'executions.sqlite',wal_full=True);store.initialize()
        registry=build_default_registry(cache_epoch=args.epoch)
        reference_service=DecisionExecutionService(adapter=adapter,store=store,registry=registry,
            server_version='2.0.6',database_model='tree',preview_rows=1000)
        references={}
        for phase,sql in queries.items():
            ref=reference_service.run(sql,mode='baseline',timeout_ms=30000,source='batch-reference').execution.attempts[-1].execution
            audit=audit_source_reference('small_scale',Path(manifest['datasets']['small_scale']['source_directory']),sql,ref.model_dump(mode='json'))
            if audit['status']!='PASS':raise ValueError('source reference mismatch')
            references[phase]=ref
            emit('reference',{'phase':phase,'result':ref.model_dump(mode='json',round_trip=True),'audit':audit})
        model=BatchPolicy(deadline=deadline,family_key=family,server_id=server,epoch=args.epoch)
        l1=BatchStatistics(scope=model.scope)
        queue=DelayedBatchStore(args.output/'feedback.sqlite',initial=model)
        emit('initial_model',model.snapshot())
        monitor=CheckedMonitor(registry=registry,snapshot_mode='latest',latest_max_age_ms=200,
            latest_wait_timeout_s=.5,monitor=SystemMonitor(db_host_sampler=sampler,interval_s=.1,
                client_sampler=PsutilSystemSampler(stale_after_ms=500)))
        monitor.start();wait_for_monitor(monitor)
        training_counts=Counter()
        pending_identity=None
        pending_feedback=None

        def consume():
            nonlocal model, pending_identity, pending_feedback
            start=time.perf_counter_ns()
            update=queue.drain_one(registry)
            if update is not None and update['status']=='APPLIED':
                if pending_feedback is None or update['batch_id']!=pending_feedback.contract.batch_id:
                    raise ValueError('L1 feedback does not match committed update')
                if not l1.observe(pending_feedback,registry):
                    raise ValueError('duplicate L1 update')
            elapsed=time.perf_counter_ns()-start
            if update is not None:
                emit('feedback_applied',{**update,'elapsed_ns':elapsed,'at':utc_now().isoformat()})
                if update['status']=='APPLIED' and pending_identity is not None:
                    training_counts[pending_identity]+=1
                model,_,_=queue.current()
                pending_identity=None
                pending_feedback=None

        def execute_batch(round_spec, position, item, stress):
            nonlocal pending_identity, pending_feedback
            phase=round_spec['phase'];n=item['size'];policy=item['policy'];sql=queries[phase]
            batch_id=new_id()
            assignment={**round_spec,'position':position,**item,'batch_id':batch_id}
            assignment.pop('batches')
            emit('batch_assignment',assignment)
            start=utc_now();begin=time.perf_counter_ns()
            adapter.result_cache.clear()
            engine=ExperimentalEngine(registry)
            learner=LearningFeedbackAgent(ExperienceRepository(store),registry,
                resource_policy=engine.resource_policy,statistics_mode='record_only')
            context=LearningContext(server_id=server,server_version='2.0.6',database_model='tree',
                dataset_epoch=args.epoch,result_rows=1,cache_condition='delayed-batch-v1',
                load_source='db_host',load_protocol='cpu-activity-v3')
            flow=Orchestrator(adapter=adapter,store=store,registry=registry,engine=engine,
                learner=learner,monitor=monitor,context=context,preview_rows=1000)
            setup_end=time.perf_counter_ns()
            features=analyzer.analyze(sql).features
            features_end=time.perf_counter_ns()
            sample,at=admit(monitor,emit)
            contract=BatchContract(batch_id=batch_id,phase=phase,size=n,query_hash=sql_sha256(sql),
                family_key=family,server_id=server,epoch=args.epoch,declared_at=start)
            admission_end=time.perf_counter_ns()
            selected=(model.choose(contract,features,sample,at,registry) if policy=='ADAPTIVE' else
                      l1.choose(contract,features,sample,at,registry) if policy=='L1_BATCH' else policy)
            prediction_end=time.perf_counter_ns()
            model_fingerprint=model.fingerprint;l1_fingerprint=l1.fingerprint
            fingerprint_end=time.perf_counter_ns()
            engine.policy=selected
            engine.model_fingerprint=l1_fingerprint if policy=='L1_BATCH' else model_fingerprint
            monitor.reference=references[phase]
            selection_ns=time.perf_counter_ns()-begin;selected_at=utc_now()
            metadata={'contract':contract.model_dump(mode='json'),'allocated_strategy':selected,'policy':policy,
                'features':features.model_dump(mode='json',round_trip=True),
                'selection_sample':sample.model_dump(mode='json',round_trip=True),
                'context_at':at.isoformat(),'selected_at':selected_at.isoformat(),'selection_ns':selection_ns}
            timing=SelectionTiming(setup_ns=setup_end-begin,features_ns=features_end-setup_end,
                admission_ns=admission_end-features_end,prediction_ns=prediction_end-admission_end,
                fingerprint_ns=fingerprint_end-prediction_end,
                finalize_ns=selection_ns-(fingerprint_end-begin),total_ns=selection_ns)
            emit('batch_selected',{**metadata,'model_fingerprint':model_fingerprint,
                'l1_fingerprint':l1_fingerprint,'selection_timing':timing.model_dump()})
            rows=[]
            for index in range(n):
                stress.check()
                cache='READY' if adapter.result_cache.entries else 'EMPTY'
                start=utc_now();begin=time.perf_counter_ns()
                admission,admission_at=admit(monitor,emit)
                result=flow.run(sql,mode='adaptive',timeout_ms=30000,source='delayed-batch:'+phase)
                elapsed=time.perf_counter_ns()-begin;finish=utc_now()
                experiences=[learner.repository.get(a.execution.execution_id) for a in result.run.execution.attempts] if result.run else []
                raw={'block':round_spec['block'],'length':n,'policy':policy,'index':index,
                    'cache_before':cache,'cache_entries_after':adapter.result_cache.entries,
                    'started_at':start.isoformat(),'finished_at':finish.isoformat(),'elapsed_ns':elapsed,
                    'admission_sample':admission.model_dump(mode='json',round_trip=True),'admission_at':admission_at.isoformat(),
                    'workflow':result.model_dump(mode='json',round_trip=True),
                    'experiences':[e.model_dump(mode='json',round_trip=True) if e else None for e in experiences]}
                emit('request',raw)
                row=SequenceRequest.model_validate(raw)
                if not equal(result.run.execution.attempts[-1].execution,references[phase]):
                    raise RuntimeError('query failed/mismatched; no retry')
                rows.append(row);stress.check()
            f=BatchFeedback(**metadata,closed_at=utc_now(),requests=rows)
            emit('batch_closed',{'batch_id':batch_id,'closed_at':f.closed_at.isoformat(),
                'feedback_sha256':digest(f.model_dump(mode='json',round_trip=True)),
                'service_ms':f.service_ms,'reward':f.reward})
            if phase=='TRAIN':
                begin=time.perf_counter_ns()
                if not queue.enqueue(f):raise ValueError('duplicate new training batch')
                emit('feedback_enqueued',{'batch_id':batch_id,'elapsed_ns':time.perf_counter_ns()-begin,'at':utc_now().isoformat()})
                pending_identity=(f.load_bucket,n,selected) if f.load_bucket==round_spec['load'] else None
                pending_feedback=f

        frozen=None
        for phase in ('TRAIN','HOLDOUT'):
            for spec in plan(phase,rounds,include_l1=True):
                emit('round_start',spec)
                exclusive()
                begin=time.monotonic()
                with round_load(spec['load']=='MEDIUM',monitor,target_percent=55,profile='staggered-spawn-v2') as stress:
                    time.sleep(3)
                    for position,item in enumerate(spec['batches']):
                        stress.check()
                        if phase=='TRAIN':consume()
                        exclusive()
                        if time.monotonic()-begin>23:raise RuntimeError('round budget exhausted; no further dispatch')
                        execute_batch(spec,position,item,stress)
                        exclusive()
                    stress.check()
                emit('round_complete',{**spec,'elapsed_s':time.monotonic()-begin,'cpu_control':stress.samples})
                if frozen and model.fingerprint!=frozen['fingerprint']:raise ValueError('frozen model changed')
                if frozen and l1.fingerprint!=frozen['l1_fingerprint']:raise ValueError('frozen L1 changed')
            if phase=='TRAIN':
                consume()
                passed=all(training_counts[(l,n,p)]>=30 for l in LOADS for n in SIZES for p in POLICIES[:2])
                emit('training_gate',{'passed':passed,'counts':{'/'.join(map(str,k)):v for k,v in training_counts.items()}})
                if not passed:raise RuntimeError('insufficient actual qualified batch samples')
                frozen=queue.freeze()
                frozen['l1_snapshot']=l1.freeze().model_dump(mode='json')
                frozen['l1_fingerprint']=l1.fingerprint
                if frozen['l1_snapshot']['evidence']!=model.evidence:
                    raise ValueError('L1 and LinUCB did not use the same admitted batches')
                emit('frozen_model',frozen)
                gc.collect()
        emit('complete',{'training_batches':8*rounds,'heldout_batches':16*rounds,'online_ready':False,'stage_accepted':False})
    except BaseException as exc:
        emit('aborted',{'error_type':type(exc).__name__,'retry_allowed':False,
            'cpu_control':stress.samples if 'stress' in locals() else []})
        raise
    finally:
        if monitor:
            monitor.stop()
            if monitor.is_monitoring:raise RuntimeError('monitor termination unconfirmed')
        if adapter:adapter.close()
        stream.close()


if __name__=='__main__':
    p=argparse.ArgumentParser()
    p.add_argument('--output',type=Path,required=True)
    p.add_argument('--epoch',required=True)
    p.add_argument('--local-db-pid',type=int,required=True)
    p.add_argument('--immutable-epoch-confirmed',action='store_true')
    p.add_argument('--cpu-cotenant-authorized',action='store_true')
    supervise(run,p.parse_args())
