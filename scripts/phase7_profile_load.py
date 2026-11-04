"""Bounded Native-only workload screening. Not learning or strategy-benefit evidence."""
import argparse
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
import hashlib
import json
from pathlib import Path
import random
import threading
import time
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from tsdb_optimizer.adapters.iotdb.tree_adapter import IoTDBTreeAdapter
from tsdb_optimizer.bandit.workload_audit import verify_import_certificate
from tsdb_optimizer.config import load_settings
from tsdb_optimizer.execution import DecisionExecutionService
from tsdb_optimizer.monitoring.local_db import VerifiedLocalDbHostSampler
from tsdb_optimizer.monitoring.activity import ActivityObservation, observation
from tsdb_optimizer.experiments.supervisor import supervise
from tsdb_optimizer.schemas.monitoring import SystemSnapshot
from tsdb_optimizer.storage import SQLiteStore
from tsdb_optimizer.strategies import build_default_registry
from tsdb_optimizer.sql_guard import validate_readonly_sql, sql_sha256

ROOT = Path(__file__).resolve().parents[1]


def execute(settings, sql, database, barrier):
    adapter = IoTDBTreeAdapter(settings)
    store = SQLiteStore(database)
    store.initialize()
    try:
        service = DecisionExecutionService(adapter=adapter, store=store,
            registry=build_default_registry(), server_version='2.0.6', database_model='tree', preview_rows=1)
        barrier.wait(timeout=10)
        run = service.run(sql, mode='baseline', timeout_ms=30000, source='phase7-load-screening')
        result = run.execution.attempts[-1].execution
        return {'execution_id': result.execution_id, 'sql_sha256':sql_sha256(sql), 'status':result.status.value,
                'strategy_id':result.strategy_id, 'row_count':result.row_count,
                'metrics':result.metrics.model_dump(mode='json',round_trip=True),
                'fetch_mode':result.fetch_mode.value, 'started_at':result.started_at.isoformat(),
                'finished_at':result.finished_at.isoformat(), 'error_type':result.error_type,
                'error_message':result.error_message, 'request_completed':result.request_completed,
                'warnings':run.execution.warnings, 'fallback':run.execution.fallback_used}
    finally:
        adapter.close()


def run(args, *, _signal=None):
    if args.repeats not in (1,2,3) or not args.immutable_epoch_confirmed:
        raise ValueError('require immutable data and repeats 1..3')
    settings = load_settings()
    sampler = VerifiedLocalDbHostSampler(host=settings.iotdb_host,port=settings.iotdb_port,pid=args.local_db_pid)
    manifest = json.loads((ROOT/'query/heavy_scale/manifest.json').read_text())
    certificate = verify_import_certificate(ROOT,manifest['import_verification'],
        f'{settings.iotdb_host}:{settings.iotdb_port}',args.epoch)
    if certificate is None: raise ValueError('import certification required')
    cases=[]
    for scale in args.scales:
        for kind in args.types:
            path=ROOT/'query/heavy_scale'/scale/(kind+'.sql')
            raw=path.read_bytes()
            sql=validate_readonly_sql(raw.decode().splitlines()[0])
            cases.append({'scale':scale,'type':kind,'path':str(path),'line':1,
                'file_sha256':hashlib.sha256(raw).hexdigest(),'sql_sha256':sql_sha256(sql),'sql':sql})
    schedule=[]
    rng=random.Random(20261102)
    # Raise concurrency only after the lower-connection stage completes.
    for workers in (1,2,4):
        stage=[(repeat,workers,case) for repeat in range(args.repeats) for case in cases]
        rng.shuffle(stage)
        schedule.extend(stage)
    args.output.mkdir(parents=True,exist_ok=False)
    with (args.output/'journal.jsonl').open('x') as journal:
        def emit(kind,body):
            if _signal and kind == 'round_start':
                _signal(kind)
            journal.write(json.dumps({'kind':kind,'body':body},allow_nan=False)+'\n');journal.flush()
            if _signal and kind == 'round_complete':
                _signal(kind)
        adapter=IoTDBTreeAdapter(settings)
        try:
            doctor=adapter.doctor()
            if not doctor.ok or doctor.server.version!='2.0.6' or doctor.server.model!='tree':
                raise ValueError('verified IoTDB 2.0.6 tree required')
        finally:
            adapter.close()
        emit('protocol',{'protocol':'cpu-activity-v3','purpose':'NATIVE_SCREENING_NOT_LEARNING',
            'epoch':args.epoch,'identity':sampler.identity.model_dump(),'certificate':certificate,
            'cpu_boundaries_percent':[50,90],'sample_interval_s':0.2,'max_connections':4,
            'query_timeout_ms':30000,'round_limit_s':60,'result_identity_version':2,'process_supervisor':_signal is not None,'seed':20261102,'repeats':args.repeats,
            'schedule':[{'repeat':r,'workers':w,'case':c} for r,w,c in schedule],
            'limitations':['whole-host metrics include unrelated processes','memory pressure and disk saturation UNKNOWN',
                          'short queries can finish between samples','no strategy benefit or learning claim']})
        try:
            sampler.sample(); time.sleep(.2)
            for index,(repeat,workers,case) in enumerate(schedule):
                before=sampler.sample()
                if not before.valid or any(getattr(before,k)>90 for k in ('cpu_percent','memory_percent','disk_usage_percent')):
                    emit('safety_stop',before.model_dump(mode='json',round_trip=True))
                    raise RuntimeError('unsafe or missing resource snapshot; no dispatch')
                started=time.monotonic()
                emit('round_start',{'index':index,'repeat':repeat,'workers':workers,'case':case,
                    'before':observation(before,0).model_dump(mode='json',round_trip=True)})
                barrier=threading.Barrier(workers)
                with ThreadPoolExecutor(max_workers=workers) as pool:
                    futures=[pool.submit(execute,settings,case['sql'],args.output/f'worker-{w}.sqlite',barrier)
                             for w in range(workers)]
                    while not all(f.done() for f in futures):
                        time.sleep(.2)
                        emit('sample',{'index':index,**observation(sampler.sample(),time.monotonic()-started).model_dump(mode='json',round_trip=True)})
                    results=[f.result() for f in futures]
                elapsed=time.monotonic()-started
                emit('round_complete',{'index':index,'elapsed_s':elapsed,'results':results})
                if elapsed>60 or any(r['status']!='SUCCESS' or r['warnings'] or r['fallback'] or
                                      r['strategy_id']!='S0_NATIVE' or r['fetch_mode']!='FULL' for r in results):
                    raise RuntimeError('failed or over-budget round; no retries; server cancellation UNKNOWN')
                print(json.dumps({'round':index+1,'total':len(schedule),'scale':case['scale'],
                    'type':case['type'],'connections':workers,'elapsed_s':round(elapsed,3)}),flush=True)
            emit('complete',{'rounds':len(schedule)})
        except BaseException as exc:
            emit('aborted',{'error_type':type(exc).__name__})
            raise


if __name__=='__main__':
    p=argparse.ArgumentParser()
    p.add_argument('--output',type=Path,required=True)
    p.add_argument('--epoch',required=True)
    p.add_argument('--immutable-epoch-confirmed',action='store_true')
    p.add_argument('--local-db-pid',type=int,required=True)
    p.add_argument('--scales', nargs='+', choices=['small_scale','medium_scale','large_scale'], default=['small_scale','medium_scale','large_scale'])
    p.add_argument('--types', nargs='+', choices=['q03_count_aggregation','q04_sum_aggregation','q05_group_by_time','q06_group_by_time_value_filter'], default=['q03_count_aggregation','q04_sum_aggregation','q05_group_by_time','q06_group_by_time_value_filter'])
    p.add_argument('--repeats',type=int,default=2)
    supervise(run, p.parse_args())
