"""Explicit bounded sequence experiment and independent evidence replay; no deployment."""
import argparse
from collections import Counter, defaultdict
import gc
import hashlib
import json
from pathlib import Path
import time
import statistics

from phase7_controlled_e2e import CheckedMonitor, ExperimentalEngine, equal, wait_for_monitor
from phase7_analyze_controlled_e2e import analyze as audit_training
from tsdb_optimizer.adapters.iotdb.tree_adapter import IoTDBTreeAdapter
from tsdb_optimizer.bandit.context import encode
from tsdb_optimizer.bandit.model import OfflineLinUCB, Snapshot
from tsdb_optimizer.bandit.workload_audit import audit_source_reference, verify_import_certificate
from tsdb_optimizer.config import load_settings
from tsdb_optimizer.execution import DecisionExecutionService
from tsdb_optimizer.experiments.controlled_policy import fingerprint
from tsdb_optimizer.experiments.sequences import SequenceRequest, schedule, validate_sequence, paired_report
from tsdb_optimizer.experiments.supervisor import supervise
from tsdb_optimizer.monitoring.local_db import VerifiedLocalDbHostSampler
from tsdb_optimizer.monitoring.system import SystemMonitor, PsutilSystemSampler
from tsdb_optimizer.orchestration.orchestrator import Orchestrator
from tsdb_optimizer.schemas.common import utc_now
from tsdb_optimizer.schemas.execution import ExecutionResult
from tsdb_optimizer.schemas.learning import LearningContext
from tsdb_optimizer.sql_guard import validate_readonly_sql, sql_sha256
from tsdb_optimizer.storage import SQLiteStore
from tsdb_optimizer.strategies import build_default_registry

ROOT = Path(__file__).resolve().parents[1]


def admit(monitor):
    """Called inside the request timer; existing bounded fresh wait, never new I/O."""
    sample = monitor.sample_once()
    at = utc_now()
    if any(not getattr(sample, name).is_usable(now=at, max_age_ms=250)
           for name in ('client_host', 'db_host')):
        raise RuntimeError('fresh resource admission unavailable')
    if any(getattr(sample.db_host, k) is None or getattr(sample.db_host, k) > limit
           for k, limit in [('cpu_percent', 80), ('memory_percent', 90), ('disk_usage_percent', 90)]):
        raise RuntimeError('unsafe resource observation')
    return sample, at


def sha(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def source_models(path):
    report = audit_training(path)
    if not report.get('ambient_scope_complete'):
        raise ValueError('source ambient experiment not completely qualified')
    protocol = frozen = None
    with path.open() as stream:
        for line in stream:
            event = json.loads(line)
            if event['kind'] == 'protocol': protocol = event['body']
            if event['kind'] == 'frozen_models': frozen = event['body']
    if protocol['workload_scale'] != 'small_scale' or protocol['reward_version'] != 'negative-e2e-seconds-v1':
        raise ValueError('wrong source workload/reward')
    models = {k: OfflineLinUCB.restore(Snapshot.model_validate(v)) for k, v in frozen['models'].items()}
    if fingerprint(models) != frozen['fingerprint'] or set(models) != {'EMPTY', 'READY'}:
        raise ValueError('source model mismatch')
    gc.collect()
    return protocol, models


def run(args, *, _signal=None):
    if not args.immutable_epoch_confirmed:
        raise ValueError('immutable dataset epoch declaration required')
    args.output.mkdir(parents=True, exist_ok=False)
    journal = (args.output/'journal.jsonl').open('x')
    def emit(kind, body):
        if _signal and kind == 'round_start': _signal(kind)
        journal.write(json.dumps({'kind': kind, 'body': body}, allow_nan=False)+'\n')
        journal.flush()
        if _signal and kind == 'round_complete': _signal(kind)
    adapter = monitor = None
    try:
        emit('round_start', {'phase': 'source_audit'})
        source, models = source_models(args.training_journal)
        emit('round_complete', {'phase': 'source_audit'})
        settings = load_settings()
        epoch = source['epoch']
        sampler = VerifiedLocalDbHostSampler(host=settings.iotdb_host, port=settings.iotdb_port,
            pid=args.local_db_pid, stale_after_ms=500)
        path = ROOT/'query/heavy_scale/small_scale/q03_count_aggregation.sql'
        sql = validate_readonly_sql(path.read_text().splitlines()[8])
        if sql in (source['train_sql'], source['test_sql']):
            raise ValueError('holdout SQL overlaps source study')
        manifest = json.loads((ROOT/'query/heavy_scale/manifest.json').read_text())
        cert = verify_import_certificate(ROOT, manifest['import_verification'],
            f'{settings.iotdb_host}:{settings.iotdb_port}', epoch)
        if cert is None: raise ValueError('import certificate required')
        protocol = {'protocol': 'sequence-e2e-v3', 'blocks': 30, 'lengths': [1, 2, 8], 'seed': 20261106,
            'timing_scope': 'bounded-admission-plus-complete-orchestrator', 'query_line': 9,
            'sql': sql, 'epoch': epoch, 'training_path': str(args.training_journal.resolve()),
            'training_sha256': sha(args.training_journal), 'model_fingerprint': fingerprint(models),
            'snapshot_ttl_ms': 500, 'snapshot_max_age_ms': 250, 'snapshot_wait_s': .5,
            'load_protocol': 'cpu-activity-v3', 'cpu_load_enabled': False,
            'process_supervisor': _signal is not None, 'query_timeout_ms': 30000,
            'round_limit_s': 60, 'max_query_connections': 1, 'online_enabled': False,
            'load_identity': sampler.identity.model_dump(), 'import_certificate': cert,
            'query_file_sha256': sha(path)}
        emit('protocol', protocol)
        sources = list((ROOT/'src/tsdb_optimizer').rglob('*.py')) + [Path(__file__).resolve(),
            ROOT/'scripts/phase7_controlled_e2e.py', ROOT/'scripts/phase7_analyze_controlled_e2e.py',
            ROOT/'docs/PHASE7_SEQUENCE_VALIDATION.md']
        (args.output/'implementation.json').write_text(json.dumps({str(p.relative_to(ROOT)): sha(p) for p in sources}, indent=2)+'\n')
        adapter = IoTDBTreeAdapter(settings)
        doctor = adapter.doctor()
        if not doctor.ok or doctor.server.version != '2.0.6': raise ValueError('verified server required')
        store = SQLiteStore(args.output/'study.sqlite', wal_full=True)
        store.initialize()
        registry = build_default_registry(cache_epoch=epoch)
        preparation = DecisionExecutionService(adapter=adapter, store=store, registry=registry,
            server_version='2.0.6', database_model='tree', preview_rows=1000)
        reference = preparation.run(sql, mode='baseline', timeout_ms=30000,
            source='sequence-reference').execution.attempts[-1].execution
        audit = audit_source_reference('small_scale',
            Path(manifest['datasets']['small_scale']['source_directory']), sql, reference.model_dump(mode='json'))
        if audit['status'] != 'PASS': raise ValueError('source reference mismatch')
        emit('reference', {'result': reference.model_dump(mode='json'), 'audit': audit})
        monitor = CheckedMonitor(registry=registry, snapshot_mode='latest', latest_max_age_ms=250,
            latest_wait_timeout_s=.5, monitor=SystemMonitor(db_host_sampler=sampler, interval_s=.2,
                client_sampler=PsutilSystemSampler(stale_after_ms=500)))
        monitor.reference = reference
        monitor.start()
        wait_for_monitor(monitor)
        for block, length, policy in schedule():
            emit('round_start', {'block': block, 'length': length, 'policy': policy})
            adapter.result_cache.clear()
            engine = ExperimentalEngine(registry)
            engine.models = models
            engine.model_fingerprint = protocol['model_fingerprint']
            engine.policy = policy
            context = LearningContext(server_id=f'{settings.iotdb_host}:{settings.iotdb_port}',
                server_version='2.0.6', database_model='tree', dataset_epoch=epoch,
                result_rows=1, cache_condition='sequence-lifecycle-v1',
                load_source='db_host', load_protocol='cpu-activity-v3')
            workflow = Orchestrator(adapter=adapter, store=store, registry=registry, engine=engine,
                monitor=monitor, context=context, preview_rows=1000)
            rows = []
            for index in range(length):
                engine.cache = 'READY' if adapter.result_cache.entries else 'EMPTY'
                emit('assignment', {'block': block, 'length': length, 'policy': policy, 'index': index})
                start = utc_now(); begin = time.perf_counter_ns()
                try:
                    sample, admission_at = admit(monitor)
                except RuntimeError:
                    sample = monitor.latest_sample()
                    emit('resource_rejected', {'observed_at': utc_now().isoformat(),
                        'elapsed_ns': time.perf_counter_ns()-begin,
                        'sample': sample.model_dump(mode='json', round_trip=True) if sample else None})
                    raise
                result = workflow.run(sql, mode='adaptive', timeout_ms=30000, source='sequence-e2e')
                elapsed = time.perf_counter_ns()-begin; finish = utc_now()
                experiences = ([workflow.learner.repository.get(a.execution.execution_id)
                    for a in result.run.execution.attempts] if result.run else [])
                raw = {'block': block, 'length': length, 'policy': policy, 'index': index,
                    'cache_before': engine.cache, 'cache_entries_after': adapter.result_cache.entries,
                    'started_at': start.isoformat(), 'finished_at': finish.isoformat(), 'elapsed_ns': elapsed,
                    'admission_sample': sample.model_dump(mode='json', round_trip=True), 'admission_at': admission_at.isoformat(),
                    'workflow': result.model_dump(mode='json', round_trip=True),
                    'experiences': [e.model_dump(mode='json', round_trip=True) if e else None for e in experiences]}
                # Journal every returned outcome before validation; rejected results remain inspectable.
                emit('request', raw)
                row = SequenceRequest.model_validate(raw)
                if not equal(result.run.execution.attempts[-1].execution, reference):
                    raise RuntimeError('result mismatch; stop without retry')
                if result.trace.events[-1].state.value != 'SUCCEEDED':
                    raise RuntimeError('request failed; server cancellation UNKNOWN; no retry')
                rows.append(row)
            validate_sequence(rows, reference, sql_hash=sql_sha256(sql), epoch=epoch)
            if fingerprint(models) != protocol['model_fingerprint']: raise ValueError('frozen model changed')
            emit('round_complete', {'block': block, 'length': length, 'policy': policy,
                'model_fingerprint': fingerprint(models)})
            if policy == 'ADAPTIVE': print(f'block={block} length={length} complete', flush=True)
        emit('complete', {'online_ready': False, 'stage_accepted': False})
    except BaseException as exc:
        emit('aborted', {'error_type': type(exc).__name__, 'retry_allowed': False})
        raise
    finally:
        if monitor:
            monitor.stop()
            if monitor.is_monitoring: raise RuntimeError('monitor termination unconfirmed')
        if adapter: adapter.close()
        journal.close()


def analyze(path):
    with path.open() as stream:
        events = [json.loads(line) for line in stream]
    if len(events) < 4 or events[0] != {'kind': 'round_start', 'body': {'phase': 'source_audit'}} or events[1] != {'kind': 'round_complete', 'body': {'phase': 'source_audit'}} or events[2]['kind'] != 'protocol':
        raise ValueError('missing protocol/source audit')
    p = events[2]['body']
    if p.get('protocol') not in ('sequence-e2e-v2', 'sequence-e2e-v3'):
        raise ValueError('unknown sequence protocol')
    required = {'blocks': 30, 'lengths': [1, 2, 8], 'seed': 20261106,
        'timing_scope': 'bounded-admission-plus-complete-orchestrator',
        'query_line': 8 if p['protocol'] == 'sequence-e2e-v2' else 9,
        'snapshot_ttl_ms': 500, 'snapshot_max_age_ms': 250, 'snapshot_wait_s': .5,
        'load_protocol': 'cpu-activity-v3', 'cpu_load_enabled': False, 'process_supervisor': True,
        'query_timeout_ms': 30000, 'round_limit_s': 60, 'max_query_connections': 1, 'online_enabled': False}
    if any(p.get(k) != v for k, v in required.items()): raise ValueError('changed sequence protocol')
    query_path = ROOT/'query/heavy_scale/small_scale/q03_count_aggregation.sql'
    if sha(query_path) != p['query_file_sha256'] or validate_readonly_sql(query_path.read_text().splitlines()[p['query_line']-1]) != p['sql']:
        raise ValueError('holdout SQL not bound to predeclared file/line')
    source_path = Path(p['training_path'])
    if sha(source_path) != p['training_sha256']: raise ValueError('training source changed')
    source, models = source_models(source_path)
    if (fingerprint(models) != p['model_fingerprint'] or source['epoch'] != p['epoch']
            or p['sql'] in (source['train_sql'], source['test_sql'])
            or source['import_certificate'] != p['import_certificate']):
        raise ValueError('training/holdout identity differs')
    registry = build_default_registry(cache_epoch=p['epoch'])
    cutoff = max(m.snapshot().training_until for m in models.values())
    train_ids = set().union(*(m.snapshot().observations for m in models.values()))
    plan = list(schedule()); position = 0; active = None; pending = None; rows = []; sequences = {}
    seen = set(train_ids); reference = None; terminal = False; previous_finish = cutoff
    for event in events[3:]:
        kind, b = event['kind'], event['body']
        if terminal: raise ValueError('events after terminal')
        if kind == 'reference':
            if reference is not None or active is not None or position: raise ValueError('misplaced reference')
            reference = ExecutionResult.model_validate(b['result'])
            if b['audit']['status'] != 'PASS' or reference.sql_sha256 != sql_sha256(p['sql']) or reference.status.value != 'SUCCESS' or reference.preview_truncated:
                raise ValueError('invalid reference')
        elif kind == 'round_start':
            if active is not None or reference is None or position >= len(plan): raise ValueError('invalid sequence start')
            active = tuple(b[k] for k in ('block', 'length', 'policy'))
            if active != plan[position]: raise ValueError('randomized schedule differs')
            rows = []
        elif kind == 'assignment':
            if active is None or pending is not None or tuple(b[k] for k in ('block', 'length', 'policy')) != active or b['index'] != len(rows):
                raise ValueError('invalid assignment')
            pending = b
        elif kind == 'request':
            row = SequenceRequest.model_validate(b)
            if row.admission_sample is None: raise ValueError('missing timed admission evidence')
            if pending != {k: getattr(row, k) for k in ('block', 'length', 'policy', 'index')}:
                raise ValueError('request lacks assignment')
            if row.started_at <= previous_finish: raise ValueError('overlapping or pre-training request')
            previous_finish = row.finished_at
            w = row.workflow
            if row.index > 0 and not w.trace.warnings and not w.run.execution.fallback_used:
                selected = row.policy
                if selected == 'ADAPTIVE':
                    e = row.experiences[0]
                    selected = models[row.cache_before].choose_registered(encode(e.features, e.before, at=e.record.started_at), registry).split('@')[0]
                if w.run.decision.selected_strategy_id != selected: raise ValueError('policy differs from assignment/frozen model')
            if any(e.context.dataset_epoch != p['epoch'] or e.context.cache_condition != 'sequence-lifecycle-v1' for e in row.experiences):
                raise ValueError('sequence context differs')
            rows.append(row); pending = None
        elif kind == 'round_complete':
            if active is None or pending is not None or tuple(b[k] for k in ('block', 'length', 'policy')) != active or b['model_fingerprint'] != p['model_fingerprint']:
                raise ValueError('incomplete/changed sequence')
            ids = validate_sequence(rows, reference, sql_hash=sql_sha256(p['sql']), epoch=p['epoch'])
            if ids & seen: raise ValueError('duplicate or training execution/request')
            seen.update(ids); sequences[active] = rows; position += 1; active = None
        elif kind == 'complete':
            if active is not None or pending is not None or position != len(plan) or b != {'online_ready': False, 'stage_accepted': False}:
                raise ValueError('incomplete planned study')
            terminal = True
        else:
            raise ValueError('aborted/unknown event; no selective outcome filtering')
    if not terminal: raise ValueError('missing completion')
    timings = defaultdict(list)
    load_counts = Counter()
    for rows in sequences.values():
        for row in rows:
            previous = 0
            for event in row.workflow.trace.events:
                timings[event.state.value].append(event.elapsed_ms-previous)
                previous = event.elapsed_ms
            load_counts.update(e.context.load_bucket for e in row.experiences)
    return {'protocol': p['protocol'], 'journal_sha256': sha(path), 'sequence_accounting_complete': True,
        'requests': sum(len(v) for v in sequences.values()), 'sequences': len(sequences),
        'actual_load_counts': dict(load_counts),
        'mean_trace_stage_ms': {k: statistics.mean(v) for k,v in timings.items()},
        'results': paired_report(sequences), 'stage_accepted': False, 'online_ready': False,
        'limitations': ['Natural host load only; cannot clear multi-load gate.',
            'Frozen historical single-request model; no Bandit updates on holdout.',
            'All completed request costs retained, including fallback/low confidence.',
            'One session, pointwise 99% intervals; serial dependence and multiplicity remain.',
            'Setup/reference/external audit serialization excluded; OS/IoTDB caches uncontrolled.']}


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--analyze', type=Path)
    parser.add_argument('--training-journal', type=Path)
    parser.add_argument('--local-db-pid', type=int)
    parser.add_argument('--immutable-epoch-confirmed', action='store_true')
    args = parser.parse_args()
    if args.analyze:
        report = analyze(args.analyze)
        with args.output.open('x') as out: out.write(json.dumps(report, indent=2)+'\n')
        print(json.dumps(report, indent=2))
    else:
        if args.training_journal is None or args.local_db_pid is None: parser.error('training journal and local DB PID required')
        supervise(run, args)
