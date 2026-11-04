#!/usr/bin/env python
"""Explicit read-only randomized experiment; never called by online requests."""
import argparse
import hashlib
from concurrent.futures import ThreadPoolExecutor
import json
from pathlib import Path
import random
import threading
import time

from tsdb_optimizer.adapters.iotdb.tree_adapter import IoTDBTreeAdapter
from tsdb_optimizer.agents import PerformanceMonitoringAgent, QueryAnalysisAgent
from tsdb_optimizer.agents.learning_feedback import LearningFeedbackAgent
from tsdb_optimizer.bandit.workload_audit import audit_source_reference, verify_import_certificate
from tsdb_optimizer.bandit.exploration import Trial, draw, summarize
from tsdb_optimizer.config import load_settings
from tsdb_optimizer.experiments.supervisor import supervise
from tsdb_optimizer.monitoring.system import SystemMonitor
from tsdb_optimizer.monitoring.local_db import VerifiedLocalDbHostSampler
from tsdb_optimizer.execution import DecisionExecutionService
from tsdb_optimizer.learning.repository import ExperienceRepository
from tsdb_optimizer.schemas.common import QueryRequest
from tsdb_optimizer.schemas.learning import LearningContext, load_identity
from tsdb_optimizer.storage import SQLiteStore
from tsdb_optimizer.strategies import build_default_registry
from tsdb_optimizer.sql_guard import validate_readonly_sql, sql_sha256

ROOT = Path(__file__).resolve().parents[1]


def select_workload(path, line_number):
    """Select an exact physical line; reject invalid input before opening a DB."""
    path = Path(path).resolve()
    raw = path.read_bytes()
    lines = raw.decode('utf-8').splitlines()
    if line_number < 1 or line_number > len(lines):
        raise ValueError('SQL line number outside file')
    sql = validate_readonly_sql(lines[line_number-1].strip())
    return sql, {'path':str(path), 'line':line_number,
                 'file_sha256':hashlib.sha256(raw).hexdigest(), 'sql_sha256':sql_sha256(sql)}


class Background:
    def __init__(self, settings, sql, workers, deadline):
        self.settings, self.sql, self.workers, self.deadline = settings, sql, workers, deadline
        self.stop = threading.Event()
        self.lock = threading.Lock()
        self.started = self.completed = self.inflight = 0
        self.errors = []
        self.pool = None
        self.futures = []

    def run(self):
        adapter = IoTDBTreeAdapter(self.settings)
        try:
            while not self.stop.is_set() and time.monotonic() < self.deadline-30:
                with self.lock:
                    self.started += 1
                    self.inflight += 1
                try:
                    result = adapter.execute_readonly(QueryRequest(sql=self.sql,
                        timeout_ms=30000, source='phase7-background'), preview_rows=1)
                    with self.lock:
                        self.completed += 1
                    if result.status.value != 'SUCCESS':
                        raise RuntimeError('background query failed: '+str(result.error_type))
                finally:
                    with self.lock:
                        self.inflight -= 1
                self.stop.wait(.02)
        except Exception as exc:
            with self.lock:
                self.errors.append(type(exc).__name__)
            self.stop.set()
        finally:
            adapter.close()

    def __enter__(self):
        if self.workers:
            self.pool = ThreadPoolExecutor(max_workers=self.workers)
            self.futures = [self.pool.submit(self.run) for _ in range(self.workers)]
        return self

    def counts(self):
        with self.lock:
            if self.errors:
                raise RuntimeError('background workload failed: '+','.join(self.errors))
            return self.started, self.completed, self.inflight

    def __exit__(self, *args):
        self.stop.set()
        if self.pool:
            self.pool.shutdown(wait=True)
        for future in self.futures:
            future.result()


def run(args, *, _signal=None):
    if not args.immutable_epoch_confirmed:
        raise ValueError('operator immutable dataset guarantee required')
    rounds_per_cell = getattr(args, 'rounds_per_cell', 1)
    if not 1 <= rounds_per_cell <= 8:
        raise ValueError('rounds per cell must be 1..8')
    if args.trials < 2 or args.trials > 256:
        raise ValueError('trials per cell must be 2..256')
    # An explicit isolated-instance file must not inherit the original process endpoint.
    settings = load_settings(env_file=args.env_file, environ={} if args.env_file else None)
    endpoint = f'{settings.iotdb_host}:{settings.iotdb_port}'
    if args.env_file and not args.expect_endpoint:
        raise ValueError('--env-file requires --expect-endpoint to guard target identity')
    if args.expect_endpoint and args.expect_endpoint != endpoint:
        raise ValueError('configured endpoint differs from explicitly expected endpoint')
    sql, workload = select_workload(getattr(args, 'sql_file', ROOT/'query/queries_dbm/q4.sql'),
                                    getattr(args, 'sql_line', 1))
    background_line = getattr(args, 'background_sql_line', None)
    background_sql, background_workload = select_workload(
        getattr(args, 'background_sql_file', None) or workload['path'],
        workload['line'] if background_line is None else background_line)
    db_sampler = None
    if getattr(args, 'local_db_pid', None):
        db_sampler = VerifiedLocalDbHostSampler(host=settings.iotdb_host,
            port=settings.iotdb_port,pid=args.local_db_pid)
    load_protocol = getattr(args, 'load_protocol', 'resource-max-v1')
    if load_protocol not in ('resource-max-v1', 'cpu-activity-v2','cpu-activity-v3'):
        raise ValueError('unknown load protocol')
    load_source = 'db_host' if db_sampler else 'client_host'
    args.output.mkdir(parents=True, exist_ok=False)
    journal = (args.output/'journal.jsonl').open('x')
    def emit(kind, body):
        if _signal and kind == 'round_start':
            _signal(kind)
        journal.write(json.dumps({'kind':kind,'body':body}, ensure_ascii=False, allow_nan=False)+'\n')
        journal.flush()  # assignment persisted before dispatch; interrupted runs remain inspectable
        if _signal and kind == 'round_complete':
            _signal(kind)
    adapter = IoTDBTreeAdapter(settings)
    registry = build_default_registry(cache_epoch=args.epoch)
    monitor = PerformanceMonitoringAgent(registry=registry,
        monitor=SystemMonitor(db_host_sampler=db_sampler))
    store = SQLiteStore(args.output/'experiences.sqlite')
    store.initialize()
    repo = ExperienceRepository(store)
    learner = LearningFeedbackAgent(repo, registry)
    trials = []
    monitor.sample_once()
    try:
        doctor = adapter.doctor()
        if not doctor.ok or doctor.server.version != '2.0.6' or doctor.server.model != 'tree':
            raise RuntimeError('verified IoTDB 2.0.6 tree required')
        emit('protocol', {'epoch':args.epoch, 'operator_immutable_guarantee':True,
            'sql_sha256':sql_sha256(sql), 'seed':args.seed, 'trials_per_round':args.trials,
            'rounds_per_cell':rounds_per_cell,
            'background_workers_planned':[0,3] if args.background else [0],
            'server_endpoint':endpoint,'max_connections':4, 'round_limit_s':60, 'dispatch_window_s':30,
            'timeout_ms':30000, 'db_host_metrics':'VERIFIED_LOCAL_HOST' if db_sampler else 'UNKNOWN',
            'local_db_identity':db_sampler.identity.model_dump() if db_sampler else None,
            'load_source':load_source, 'load_protocol':load_protocol, 'sql_file':workload['path'],
            'preaction_cpu_window_s':0.2 if load_protocol in ('cpu-activity-v2','cpu-activity-v3') else None,
            'workload':workload, 'background_workload':background_workload,
            'protocol':'randomized-cache-state-v1'})
        service = DecisionExecutionService(adapter=adapter, store=store, registry=registry,
            server_version='2.0.6', database_model='tree', preview_rows=1000)
        def execute(strategy, source, snapshot=None):
            run = service.run(sql, mode='baseline' if strategy=='S0_NATIVE' else 'rule_based',
                timeout_ms=30000, source=source, system_state=snapshot)
            r = run.execution.attempts[-1].execution
            if (r.status.value != 'SUCCESS' or run.execution.fallback_used or
                    r.strategy_id != strategy or r.preview_truncated or run.execution.warnings):
                emit('failed_execution', run.model_dump(mode='json'))
                raise RuntimeError('execution failed, truncated, warned, or did not take intended strategy')
            return run, r
        _, ref = execute('S0_NATIVE', 'phase7-reference')
        emit('reference',ref.model_dump(mode='json'))
        heavy_root = (ROOT/'query/heavy_scale').resolve()
        selected_path = Path(workload['path'])
        if selected_path.is_relative_to(heavy_root):
            scale = selected_path.relative_to(heavy_root).parts[0]
            manifest = json.loads((heavy_root/'manifest.json').read_text())
            source = Path(manifest['datasets'][scale]['source_directory'])
            source_audit = audit_source_reference(scale,source,sql,ref.model_dump(mode='json'))
            emit('source_reference_audit',source_audit)
            if source_audit['status'] != 'PASS':
                raise RuntimeError('SOURCE_DATABASE_MISMATCH: no learning samples admitted')
            certificate=verify_import_certificate(ROOT,manifest.get('import_verification'),endpoint,args.epoch)
            if certificate:
                emit('dataset_import_certification',certificate)
        def matches(result):
            return (result.columns==ref.columns and result.row_count==ref.row_count
                    and result.rows_preview==ref.rows_preview and not result.preview_truncated)
        for phase_index, phase in enumerate(('train','holdout')):
            # Separate seeds and complete training collection before any holdout dispatch.
            for workers in (0,3) if args.background else (0,):
                for cache_state in ('EMPTY','READY'):
                    seed = args.seed + phase_index*100 + workers*10 + (cache_state=='READY')
                    rng = random.Random(seed)
                    next_index = 0
                    for round_index in range(rounds_per_cell):
                        started = time.monotonic()
                        emit('round_start', {'phase':phase,'cache':cache_state,'workers':workers,'round_index':round_index})
                        deadline = started+60
                        with Background(settings,background_sql,workers,deadline) as background:
                            for index in range(args.trials):
                                if time.monotonic() >= deadline-30:
                                    emit('round_stopped', {'phase':phase, 'cache':cache_state,
                                        'workers':workers,'completed_trials':index,'reason':'DISPATCH_WINDOW_EXHAUSTED'})
                                    break
                                adapter.result_cache.clear()
                                if cache_state=='READY':
                                    _, prepared = execute('S1_REPEAT_CACHE','phase7-cache-preparation')
                                    emit('cache_preparation',prepared.model_dump(mode='json'))
                                    if prepared.served_from_cache or not matches(prepared):
                                        raise RuntimeError('cache preparation mismatch')
                                if time.monotonic() >= deadline-30:
                                    emit('round_stopped', {'phase':phase, 'cache':cache_state,
                                        'workers':workers,'completed_trials':index,'reason':'PREPARATION_USED_DISPATCH_WINDOW'})
                                    break
                                if db_sampler:
                                    db_sampler.check_identity()
                                if load_protocol in ('cpu-activity-v2','cpu-activity-v3'):
                                    # Prime on this thread, then measure a declared window.
                                    # A near-zero interval can produce spurious CPU spikes.
                                    monitor.sample_once()
                                    time.sleep(0.2)
                                before = monitor.sample_once()
                                if db_sampler and not before.db_host.valid:
                                    raise RuntimeError('verified DB host sample unavailable; stop experiment')
                                if time.monotonic() >= deadline-30:
                                    emit('round_stopped', {'phase':phase,'cache':cache_state,
                                        'workers':workers,'completed_trials':index,
                                        'reason':'SAMPLING_USED_DISPATCH_WINDOW'})
                                    break
                                started_count, completed_count, inflight = background.counts()
                                assignment = draw(rng,phase=phase,seed=seed,index=next_index,
                                    workload_sha256=sql_sha256(sql),cache_state_before=cache_state,
                                    background_workers=workers)
                                emit('assignment',assignment.model_dump(mode='json'))
                                next_index += 1
                                begin = time.perf_counter()
                                run_result, result = execute(assignment.strategy_id,'phase7-randomized-trial',before)
                                service_ms = (time.perf_counter()-begin)*1000
                                record = monitor.record_execution(result,
                                    decision_id=run_result.decision.decision_id,snapshots=before)
                                record = record.model_copy(update={'runtime_result_check':'MATCHED' if matches(result) else 'MISMATCHED'})
                                host,bucket = load_identity(before,load_source,result.started_at,protocol=load_protocol)
                                context = LearningContext(server_id=f'{settings.iotdb_host}:{settings.iotdb_port}',
                                    server_version='2.0.6',database_model='tree',dataset_epoch=args.epoch,
                                    result_rows=result.row_count,cache_condition='controlled-'+cache_state.lower(),
                                    result_source='CACHE_HIT' if result.served_from_cache else 'SERVER',
                                    load_source=load_source,load_host=host,load_bucket=bucket,load_protocol=load_protocol)
                                feedback = learner.observe(run_result,context,record=record,before=before,
                                    after=monitor.sample_once())
                                emit('feedback',feedback.model_dump(mode='json'))
                                if feedback.warnings or feedback.experience_id is None:
                                    raise RuntimeError('experience persistence/learning failure')
                                trial = Trial(assignment=assignment,experience=repo.get(result.execution_id),
                                    service_wall_ms=service_ms,background_started=started_count,
                                    background_completed=completed_count,background_inflight_before=inflight)
                                emit('trial',trial.model_dump(mode='json',round_trip=True))
                                trials.append(trial)
                                if not matches(result):
                                    raise RuntimeError('result mismatch; strategy quarantined')
                            counts = background.counts()
                        final_counts = background.counts()  # final failures must not disappear on shutdown
                        elapsed = time.monotonic()-started
                        emit('round_complete', {'phase':phase,'cache':cache_state,'workers':workers,
                            'round_index':round_index,'elapsed_s':elapsed,'background_counts':counts,'background_counts_after_stop':final_counts})
                        if elapsed > 60:
                            raise RuntimeError('round exceeded wall limit; server cancellation UNKNOWN; stop experiment')
                        print(json.dumps({'phase':phase,'cache':cache_state,'workers':workers,
                            'total_trials':len(trials),'elapsed_s':round(elapsed,2)}),flush=True)
        summary = summarize(trials)
        summary['epoch'] = args.epoch
        (args.output/'summary.json').write_text(json.dumps(summary,ensure_ascii=False,indent=2,allow_nan=False)+'\n')
        emit('summary',summary)
        return summary
    except BaseException as exc:
        emit('aborted',{'error_type':type(exc).__name__,'completed_trials':len(trials)})
        raise
    finally:
        adapter.close()
        journal.close()


if __name__=='__main__':
    p=argparse.ArgumentParser()
    p.add_argument('--output',type=Path,required=True)
    p.add_argument('--epoch',required=True)
    p.add_argument('--env-file',type=Path)
    p.add_argument('--expect-endpoint')
    p.add_argument('--immutable-epoch-confirmed',action='store_true')
    p.add_argument('--local-db-pid',type=int)
    p.add_argument('--sql-file',type=Path,default=ROOT/'query/queries_dbm/q4.sql')
    p.add_argument('--sql-line',type=int,default=1)
    p.add_argument('--background-sql-file',type=Path)
    p.add_argument('--background-sql-line',type=int)
    p.add_argument('--load-protocol', choices=['resource-max-v1','cpu-activity-v2','cpu-activity-v3'], default='resource-max-v1')
    p.add_argument('--background',action='store_true')
    p.add_argument('--rounds-per-cell',type=int,default=1)
    p.add_argument('--trials',type=int,default=128)
    p.add_argument('--seed',type=int,default=20261102)
    args=p.parse_args()
    supervise(run, args)
