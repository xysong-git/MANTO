"""Predeclared training and held-out comparison, with bounded live execution."""
from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from datetime import timedelta
import hashlib
import importlib.metadata
import json
import os
from pathlib import Path
import threading
import time

from ..adapters.iotdb.tree_adapter import IoTDBTreeAdapter
from ..agents import QueryAnalysisAgent
from ..bandit.model import AdmissionError
from ..bandit.workload_audit import verify_import_certificate
from ..config import load_settings
from ..execution import DecisionExecutionService
from ..experiments.cpu_load import CpuLoad
from ..experiments.exclusive_instance import inspect_clients
from ..monitoring.local_db import VerifiedLocalDbHostSampler
from ..monitoring.system import PsutilSystemSampler, SystemMonitor
from ..schemas.common import utc_now
from ..storage import SQLiteStore
from ..strategies import build_default_registry
from .journal import Journal
from .cpu_evidence import CounterProbe, EvidenceSampler
from .policy import PolicyBank
from .protocol import GROUP_DESCRIPTIONS, checksum, schedule
from .results import result_matches
from .runtime import Lane, ResourceAdmissionError

ROOT = Path(__file__).resolve().parents[3]


def write_json(path, value):
    with Path(path).open('x') as stream:
        json.dump(value, stream, indent=2, sort_keys=True, allow_nan=False)
        stream.write('\n')


class LiveEnvironment:
    evidence_domain = 'REAL_IOTDB'
    adapter_factory = IoTDBTreeAdapter

    def __init__(self, config, *, local_db_pid, env_file=None, cpu_authorized=False):
        if config.load_driver == 'cpu_cotenant' and not cpu_authorized:
            raise ValueError('explicit CPU co-tenant authorization flag required')
        self.config = config
        self.settings = load_settings(env_file=env_file)
        self.pid = local_db_pid
        manifest_path = ROOT/config.dataset.manifest
        raw = manifest_path.read_bytes()
        if hashlib.sha256(raw).hexdigest() != config.dataset.manifest_sha256:
            raise ValueError('dataset manifest differs from pinned configuration')
        self.manifest = json.loads(raw)
        self.certificate = verify_import_certificate(ROOT, self.manifest['import_verification'],
            f'{self.settings.iotdb_host}:{self.settings.iotdb_port}', config.dataset.epoch)
        if self.certificate is None:
            raise ValueError('verified import certificate required')
        processes = {'benchmark': os.getpid(), 'datanode': self.pid}
        db_probe, client_probe = CounterProbe(processes), CounterProbe(processes)
        sampler = VerifiedLocalDbHostSampler(host=self.settings.iotdb_host, port=self.settings.iotdb_port,
            pid=self.pid, stale_after_ms=500, psutil_module=db_probe)
        self.cpu_samplers = {
            'db_host': EvidenceSampler(sampler, db_probe),
            'client_host': EvidenceSampler(PsutilSystemSampler(stale_after_ms=500,
                psutil_module=client_probe), client_probe)}
        self.monitor = SystemMonitor(db_host_sampler=self.cpu_samplers['db_host'], interval_s=.1,
            client_sampler=self.cpu_samplers['client_host'])
        self.identity = sampler.identity.model_dump(mode='json')

    def cpu_evidence(self, sample):
        return {source: sampler.evidence_for(getattr(sample, source))
                for source, sampler in self.cpu_samplers.items()}

    def start(self):
        self.monitor.start()

    def close(self):
        self.monitor.stop()
        if self.monitor.is_running:
            raise RuntimeError('monitor termination unconfirmed')

    def check(self, emit, budget):
        observation = inspect_clients(self.pid, self.settings.iotdb_port)
        emit('instance_occupancy', observation.model_dump(mode='json'))
        if not observation.passed or observation.connection_count > budget:
            raise RuntimeError('exclusive connection budget not satisfied; no further dispatch')

    @contextmanager
    def load(self, target, emit):
        enabled = self.config.load_driver == 'cpu_cotenant' and target == 'MEDIUM'
        stress = None
        try:
            with CpuLoad(enabled, target_percent=55, profile='staggered-spawn-v2') as stress:
                if enabled:
                    time.sleep(3)
                yield stress
        finally:
            # Cleanup completes before serializing the controller's samples.
            # A failed lane must not discard the sample that stopped the load.
            if stress is not None:
                emit('load_control', {'target': target, 'driver': self.config.load_driver,
                    'samples': stress.samples, 'error': stress.error,
                    'scope': 'whole-host co-tenant, not DB query load'})

    def reference(self, query, output, emit):
        adapter = self.adapter_factory(self.settings)
        store = SQLiteStore(output/'references.sqlite', wal_full=True)
        try:
            doctor = adapter.doctor()
            emit('doctor', doctor.model_dump(mode='json'))
            if not doctor.ok or doctor.server.version != self.config.dataset.server_version:
                raise RuntimeError('server capability/version differs from audited configuration')
            service = DecisionExecutionService(adapter=adapter, store=store,
                registry=build_default_registry(), server_version=self.config.dataset.server_version,
                database_model='tree', preview_rows=1000)
            run = service.run(query.resolved(), mode='baseline', timeout_ms=self.config.timeout_ms,
                source='phase8-reference')
            emit('reference_execution', run.model_dump(mode='json', round_trip=True))
            result = run.execution.attempts[-1].execution
            if not result_matches(result, result, query, self.config.analysis):
                raise RuntimeError('reference failed/incomplete or unexpected row count')
            return result
        finally:
            try:
                adapter.close()
            finally:
                store.close()


def execute(config, output, environment, *, signal=None):
    """Injectable environment is used only by marked synthetic offline tests."""
    output = Path(output)
    output.mkdir(parents=True, exist_ok=False)
    (output/'lanes').mkdir()
    journal = Journal(output/'journal.jsonl')
    allocations = schedule(config)
    write_json(output/'config.json', config.model_dump(mode='json'))
    write_json(output/'plan.json', [a.model_dump(mode='json') for a in allocations])
    sources = sorted((ROOT/'src/tsdb_optimizer').rglob('*.py')) + sorted((ROOT/'benchmarks').glob('*.py'))
    sources += [ROOT/'configs/strategies.yaml', ROOT/'configs/resource_policy.yaml']
    implementation = {str(p.relative_to(ROOT)): hashlib.sha256(p.read_bytes()).hexdigest() for p in sources}
    write_json(output/'implementation.json', implementation)
    deadline = utc_now()+timedelta(hours=24)
    bank = PolicyBank(deadline=deadline, min_samples=config.min_samples)
    emit = journal.emit
    active = False

    def start_round(body):
        nonlocal active
        if signal:
            signal('round_start')
        active = True
        emit('round_start', body)

    def end_round(body):
        nonlocal active
        emit('round_complete', body)
        if signal:
            signal('round_complete')
        active = False

    try:
        emit('protocol', {'config': config.model_dump(mode='json'), 'config_sha256': config.checksum,
            'plan_sha256': checksum([a.model_dump(mode='json') for a in allocations]),
            'implementation_sha256': checksum(implementation), 'evidence_domain': environment.evidence_domain,
            'group_definitions': GROUP_DESCRIPTIONS, 'training_deadline': deadline.isoformat(),
            'dataset_certificate': environment.certificate, 'db_host_identity': environment.identity,
            'python_packages': {p: importlib.metadata.version(p) for p in
                ('apache-iotdb', 'pydantic', 'sqlglot', 'psutil', 'matplotlib')},
            'server_execution_latency': 'UNKNOWN: no query-level server timer; no EXPLAIN ANALYZE',
            'production_online_enabled': False, 'phase7_accepted': False,
            'supervised': signal is not None})
        analyzer = QueryAnalysisAgent()
        for case in config.workloads:
            for query in (case.train, case.holdout):
                if analyzer.analyze(query.resolved()).parse_status.value != 'SUCCESS':
                    raise ValueError('benchmark model requires fixture-supported parser features')
                if query.ordering == 'UNORDERED' and (config.analysis.float_abs_tolerance or config.analysis.float_rel_tolerance):
                    raise ValueError('unordered comparison currently requires exact values')
        environment.start()
        references = {}
        for case in config.workloads:
            for phase, query in (('TRAIN', case.train), ('HOLDOUT', case.holdout)):
                identity = {'phase': 'REFERENCE', 'workload': case.name, 'query_phase': phase}
                start_round(identity)
                environment.check(emit, 0)
                result = environment.reference(query, output, emit)
                references[(case.name, phase)] = result
                emit('reference', {'workload': case.name, 'phase': phase, 'result': result.model_dump(mode='json')})
                end_round(identity)

        cases = {case.name: case for case in config.workloads}
        frozen = None
        for allocation in allocations:
            if allocation.phase == 'HOLDOUT' and not bank.frozen:
                snapshot = bank.freeze()
                write_json(output/'models.json', {'evidence_domain': environment.evidence_domain, 'bank': snapshot})
                frozen = bank.fingerprint
                emit('frozen_model', {'fingerprint': frozen, 'counts': bank.counts(),
                    'evidence_count': len(bank.evidence), 'snapshot': snapshot})
            body = allocation.model_dump(mode='json')
            start_round(body)
            begin_round = time.perf_counter_ns()
            environment.check(emit, 0)
            query = cases[allocation.workload].train if allocation.phase == 'TRAIN' else cases[allocation.workload].holdout
            rows = []
            rows_lock = threading.Lock()
            stop = threading.Event()
            barrier = threading.Barrier(allocation.concurrency)
            def record(row):
                emit('request', row.model_dump(mode='json', round_trip=True))
                with rows_lock:
                    rows.append(row)
                if not row.success or row.correctness != 'MATCHED' or row.error_type:
                    stop.set()
                    raise RuntimeError('query failed/mismatched/unknown; stop all lanes, no retry')

            with environment.load(allocation.target_load, emit) as stress:
                def worker(lane_index):
                    lane = None
                    setup_start = time.perf_counter_ns()
                    try:
                        lane = Lane(config=config, allocation=allocation, lane=lane_index, query=query,
                            reference=references[(allocation.workload, allocation.phase)],
                            settings=environment.settings, monitor=environment.monitor,
                            output=output/'lanes', bank=bank, adapter_factory=environment.adapter_factory)
                        emit('lane_setup', {'allocation_id': allocation.allocation_id, 'lane': lane_index,
                            'elapsed_ns': time.perf_counter_ns()-setup_start})
                        def record_lane(row):
                            try:
                                record(row)
                            except RuntimeError as exc:
                                if lane.last_error is not None:
                                    raise exc from lane.last_error
                                raise
                        lane.prepare(record_lane)
                        barrier.wait(timeout=config.round_limit_s-2)
                        for index in range(config.requests_per_lane):
                            if stop.is_set():
                                raise RuntimeError('peer lane failed; no further dispatch')
                            stress.check()
                            if (time.perf_counter_ns()-begin_round)/1e9 > config.round_limit_s-2:
                                raise TimeoutError('round budget exhausted; server completion UNKNOWN')
                            record_lane(lane.request(index))
                            stress.check()
                    except BaseException as exc:
                        if isinstance(exc, ResourceAdmissionError):
                            emit('resource_rejected', {'allocation_id': allocation.allocation_id,
                                'lane': lane_index, 'reason': str(exc), 'at': exc.at.isoformat(),
                                'request_role': exc.request_role, 'request_index': exc.request_index,
                                'violations': [v.model_dump(mode='json') for v in exc.violations],
                                'sample': exc.sample.model_dump(mode='json', round_trip=True),
                                'cpu_counter_evidence': (environment.cpu_evidence(exc.sample)
                                    if hasattr(environment, 'cpu_evidence') else None)})
                        stop.set()
                        barrier.abort()
                        raise
                    finally:
                        if lane is not None:
                            close_start = time.perf_counter_ns()
                            lane.close()
                            emit('lane_close', {'allocation_id': allocation.allocation_id, 'lane': lane_index,
                                'elapsed_ns': time.perf_counter_ns()-close_start})
                # Every lane has an independent connection, registry, cache and SQLite.
                with ThreadPoolExecutor(max_workers=allocation.concurrency) as pool:
                    futures = [pool.submit(worker, i) for i in range(allocation.concurrency)]
                    for future in futures:
                        future.result()
                stress.check()
            environment.check(emit, 0)
            if allocation.phase == 'TRAIN':
                registry = build_default_registry(cache_epoch=config.dataset.epoch)
                for row in sorted(rows, key=lambda r: (r.lane, r.index)):
                    if row.role != 'MEASURED':
                        continue
                    before_update = time.perf_counter_ns()
                    try:
                        if row.workflow is None or len(row.experiences) != 1:
                            raise AdmissionError('MISSING_OR_MULTIPLE_EXPERIENCES')
                        changed = bank.update(allocation, row.experiences[0], row.workflow,
                            started_at=row.started_at, finished_at=row.finished_at, elapsed_ns=row.elapsed_ns,
                            registry=registry)
                        status, reason = ('APPLIED' if changed else 'DUPLICATE'), None
                    except AdmissionError as exc:
                        status, reason = 'REJECTED', exc.reason
                    emit('training_update', {'allocation_id': allocation.allocation_id, 'lane': row.lane,
                        'index': row.index, 'status': status, 'reason': reason,
                        'elapsed_ns': time.perf_counter_ns()-before_update, 'model_fingerprint': bank.fingerprint})
            elif bank.fingerprint != frozen:
                raise RuntimeError('held-out model was mutated')
            end_round({**body, 'elapsed_ns': time.perf_counter_ns()-begin_round,
                'measured_requests': sum(row.role == 'MEASURED' for row in rows)})
        # Cleanup is part of successful completion, not an event after complete.
        environment.close()
        current = {str(p.relative_to(ROOT)): hashlib.sha256(p.read_bytes()).hexdigest() for p in sources}
        if current != implementation:
            raise RuntimeError('implementation changed during experiment')
        emit('complete', {'allocations': len(allocations), 'model_fingerprint': bank.fingerprint,
            'production_online_enabled': False, 'stage_accepted': False})
    except BaseException as exc:
        try:
            environment.close()
        except Exception as cleanup:
            emit('cleanup_failed', {'error_type': type(cleanup).__name__})
        emit('aborted', {'error_type': type(exc).__name__, 'active_round': active,
            'retry_allowed': False, 'server_cancellation': 'UNKNOWN'})
        raise
    finally:
        journal.close()
