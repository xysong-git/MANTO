"""Independent plan/evidence replay and block-level uncertainty, without IoTDB."""
from __future__ import annotations

from collections import Counter, defaultdict
import csv
from datetime import datetime
import hashlib
import json
from pathlib import Path
import random
import statistics

from ..agents import QueryAnalysisAgent
from ..bandit.model import AdmissionError
from ..schemas.execution import ExecutionResult
from ..schemas.monitoring import SystemSample
from ..strategies import build_default_registry
from .journal import read_events
from .policy import PolicyBank, model_key
from .protocol import Allocation, BenchmarkConfig, checksum, schedule
from .results import RequestRecord, result_matches

CSV_FIELDS = ['allocation_id', 'phase', 'block', 'workload', 'group', 'cache_condition', 'target_load',
    'observed_load', 'concurrency', 'repeat', 'lane', 'index', 'role', 'sql_sha256', 'query_id',
    'decision_id', 'execution_ids', 'selected_strategy', 'actual_strategy', 'cache_before', 'cache_hit',
    'success', 'correctness', 'fallback', 'dispatches', 'end_to_end_ms', 'decision_overhead_ms',
    'client_wall_ms', 'execute_call_ms', 'result_fetch_ms', 'server_execution_ms',
    'client_cpu_percent', 'client_memory_percent', 'db_cpu_percent', 'db_memory_percent',
    'started_at', 'finished_at', 'sample_status', 'error_type']
METRICS = ('end_to_end_ms', 'decision_overhead_ms', 'client_wall_ms', 'execute_call_ms',
           'result_fetch_ms', 'client_cpu_percent', 'client_memory_percent', 'db_cpu_percent', 'db_memory_percent')


def quantile(values, q):
    values = sorted(values)
    if not values:
        return None
    x = (len(values)-1)*q
    lo = int(x)
    return values[lo] + (values[min(lo+1, len(values)-1)]-values[lo])*(x-lo)


def interval(block_values, analysis):
    if len(block_values) < analysis.minimum_blocks:
        return None
    rng = random.Random(analysis.seed)
    values = [statistics.mean(rng.choices(block_values, k=len(block_values)))
              for _ in range(analysis.bootstrap_replicates)]
    tail = (1-analysis.confidence)/2
    return [quantile(values, tail), quantile(values, 1-tail)]


def throughput_interval(block_totals, analysis):
    """Bootstrap the same ratio of totals used by the throughput point estimate."""
    if len(block_totals) < analysis.minimum_blocks:
        return None
    rng = random.Random(analysis.seed)
    values = []
    for _ in range(analysis.bootstrap_replicates):
        chosen = rng.choices(block_totals, k=len(block_totals))
        values.append(sum(n for n, _ in chosen)/sum(seconds for _, seconds in chosen))
    tail = (1-analysis.confidence)/2
    return [quantile(values, tail), quantile(values, 1-tail)]


def flat(row):
    a = row.allocation
    attempts = row.run.execution.attempts if row.run else []
    final = attempts[-1].execution if attempts else None
    d = row.run.decision if row.run else None
    out = {name: getattr(a, name) for name in ('allocation_id', 'phase', 'block', 'workload', 'group',
        'cache_condition', 'target_load', 'concurrency', 'repeat')}
    out.update(observed_load=row.observed_load, lane=row.lane, index=row.index, role=row.role,
        sql_sha256=row.sql_sha256, query_id=d.query_id if d else None,
        decision_id=d.decision_id if d else None,
        execution_ids=';'.join(x.execution.execution_id for x in attempts),
        selected_strategy=d.selected_strategy_id if d else None,
        actual_strategy=final.strategy_id if final else None, cache_before=row.cache_before,
        cache_hit=final.served_from_cache if final else None, success=row.success,
        correctness=row.correctness, fallback=row.run.execution.fallback_used if row.run else None,
        dispatches=sum(x.execution.request_dispatched for x in attempts) if attempts else None,
        end_to_end_ms=row.end_to_end_ms, decision_overhead_ms=d.decision_overhead_ms if d else None,
        server_execution_ms=None, started_at=row.started_at.isoformat(), finished_at=row.finished_at.isoformat(),
        sample_status=';'.join(e.sample_status.value for e in row.experiences), error_type=row.error_type)
    for name in ('client_wall_ms', 'execute_call_ms', 'result_fetch_ms'):
        out[name] = sum(getattr(x.execution.metrics, name) for x in attempts) if attempts else None
    for prefix, source in (('client', 'client_host'), ('db', 'db_host')):
        snapshot = getattr(row.sample, source)
        for field in ('cpu_percent', 'memory_percent'):
            out[prefix+'_'+field] = getattr(snapshot, field) if snapshot.source_type.value == source else None
    return out


def summarize(rows, rounds, config, *, qualified):
    cells = defaultdict(list)
    for row in rows:
        if row.role == 'MEASURED' and row.allocation.phase == 'HOLDOUT':
            a = row.allocation
            cells[(a.workload, a.cache_condition, a.target_load, a.concurrency, a.group)].append(flat(row))
    summaries = []
    for key, records in sorted(cells.items()):
        block_ids = sorted({r['block'] for r in records})
        groups = {b: [r for r in records if r['block'] == b] for b in block_ids}
        metrics = {}
        for name in METRICS:
            values = [r[name] for r in records if r[name] is not None]
            means = [statistics.mean(r[name] for r in group if r[name] is not None)
                     for group in groups.values() if any(r[name] is not None for r in group)]
            metrics[name] = {'n': len(values), 'missing': len(records)-len(values),
                'mean': statistics.mean(values) if values else None,
                'sample_std': statistics.stdev(values) if len(values) > 1 else None,
                'p50': quantile(values, .5), 'p95': quantile(values, .95), 'p99': quantile(values, .99),
                'mean_ci': interval(means, config.analysis) if qualified else None}
        allocation_ids = {r['allocation_id'] for r in records}
        spans = []
        block_totals = defaultdict(lambda: [0, 0.])
        for identifier in allocation_ids:
            group = [r for r in records if r['allocation_id'] == identifier]
            seconds = (max(datetime.fromisoformat(r['finished_at']) for r in group)
                       - min(datetime.fromisoformat(r['started_at']) for r in group)).total_seconds()
            successful = sum(r['success'] for r in group)
            spans.append((seconds, successful))
            block_totals[group[0]['block']][0] += successful
            block_totals[group[0]['block']][1] += seconds
        round_seconds = sum(rounds[i]['elapsed_ns']/1e9 for i in allocation_ids if i in rounds)
        rate_blocks = [v for _, v in sorted(block_totals.items())]
        summaries.append({'workload': key[0], 'cache_condition': key[1], 'target_load': key[2],
            'concurrency': key[3], 'group': key[4], 'requests': len(records), 'blocks': len(block_ids),
            'successes': sum(r['success'] for r in records),
            'correctness': dict(Counter(r['correctness'] for r in records)),
            'observed_loads': dict(Counter(r['observed_load'] for r in records)),
            'selected_strategies': dict(Counter(r['selected_strategy'] or 'UNKNOWN' for r in records)),
            'actual_strategies': dict(Counter(r['actual_strategy'] or 'UNKNOWN' for r in records)),
            'cache_hits': sum(bool(r['cache_hit']) for r in records),
            'fallbacks': sum(bool(r['fallback']) for r in records),
            'metrics': metrics, 'server_execution_ms': None,
            'throughput': {'successful_requests_per_second': sum(n for _, n in spans)/sum(s for s, _ in spans),
                'pooled_rate_ci': throughput_interval(rate_blocks, config.analysis) if qualified else None,
                'including_setup_warmup_cleanup_per_second': sum(n for _, n in spans)/round_seconds if round_seconds else None,
                'definition': 'successful requests / sum of nonoverlapping allocation measurement envelopes; concurrent latency sums are not wall time'}})
    contrasts = []
    for candidate, baseline in (('E1', 'E0'), ('E4', 'E0'), ('E4', 'E1'), ('E4', 'E2'),
                                 ('E4', 'E3'), ('A1', 'E4'), ('A2', 'E4')):
        for key in sorted({k[:4] for k in cells}):
            left, right = cells.get((*key, candidate)), cells.get((*key, baseline))
            if not left or not right:
                continue
            blocks = sorted({r['block'] for r in left} & {r['block'] for r in right})
            delta = [statistics.mean(r['end_to_end_ms'] for r in left if r['block'] == b)
                     - statistics.mean(r['end_to_end_ms'] for r in right if r['block'] == b) for b in blocks]
            contrasts.append({'workload': key[0], 'cache_condition': key[1], 'target_load': key[2],
                'concurrency': key[3], 'candidate': candidate, 'baseline': baseline, 'blocks': len(delta),
                'mean_delta_ms': statistics.mean(delta), 'paired_mean_ci_ms': interval(delta, config.analysis) if qualified else None,
                'scope': 'within-session paired-block exploratory contrast; no multiplicity correction or independent-session certification'})
    return summaries, contrasts


def analyze(directory):
    directory = Path(directory)
    events = list(read_events(directory/'journal.jsonl'))
    if not events or events[0]['kind'] != 'protocol':
        raise ValueError('missing initial protocol')
    protocol = events[0]['body']
    config = BenchmarkConfig.model_validate(protocol['config'])
    if config.checksum != protocol['config_sha256']:
        raise ValueError('configuration checksum mismatch')
    plan = schedule(config)
    if checksum([a.model_dump(mode='json') for a in plan]) != protocol['plan_sha256']:
        raise ValueError('plan differs from deterministic schedule')
    for filename, expected in (('config.json', protocol['config']), ('plan.json', [a.model_dump(mode='json') for a in plan])):
        if json.loads((directory/filename).read_text()) != expected:
            raise ValueError(f'{filename} differs from journal')
    if checksum(json.loads((directory/'implementation.json').read_text())) != protocol['implementation_sha256']:
        raise ValueError('implementation manifest mismatch')
    bank = PolicyBank(deadline=datetime.fromisoformat(protocol['training_deadline']), min_samples=config.min_samples)
    registry = build_default_registry(cache_epoch=config.dataset.epoch)
    expected = {a.allocation_id: a for a in plan}
    cases = {w.name: w for w in config.workloads}
    references, rows, seen, rounds = {}, [], set(), {}
    query_ids, execution_ids = set(), set()
    starts, lane_setup, lane_close = [], set(), set()
    request_map, update_ids = {}, set()
    lane_cache, fills, lane_positions, lane_finished = {}, defaultdict(set), Counter(), {}
    active = None
    frozen = False
    for event in events[1:]:
        kind, body = event['kind'], event['body']
        if kind == 'round_start':
            if active is not None:
                raise ValueError('overlapping round lifecycle')
            active = body
            if body['phase'] != 'REFERENCE':
                allocation = Allocation.model_validate(body)
                if len(starts) >= len(plan) or allocation != plan[len(starts)]:
                    raise ValueError('allocation differs from predeclared order')
                starts.append(allocation.allocation_id)
                if allocation.phase == 'HOLDOUT' and not frozen:
                    raise ValueError('holdout before frozen model')
        elif kind == 'reference':
            key = (body['workload'], body['phase'])
            if key in references or active is None or active['phase'] != 'REFERENCE':
                raise ValueError('duplicate/unscoped reference')
            if key != (active['workload'], active['query_phase']):
                raise ValueError('reference is bound to the wrong workload/phase')
            query = cases[key[0]].train if key[1] == 'TRAIN' else cases[key[0]].holdout
            result = ExecutionResult.model_validate(body['result'])
            if result.sql_sha256 != query.resolved_sha256 or not result_matches(result, result, query, config.analysis):
                raise ValueError('invalid reference result')
            references[key] = result
        elif kind in ('lane_setup', 'lane_close'):
            key = (body['allocation_id'], body['lane'])
            target = lane_setup if kind == 'lane_setup' else lane_close
            if key in target or active is None or active.get('allocation_id') != key[0]:
                raise ValueError('duplicate/unscoped lane lifecycle')
            if kind == 'lane_close' and key not in lane_setup:
                raise ValueError('closing unopened lane')
            target.add(key)
        elif kind == 'request':
            row = RequestRecord.model_validate(body)
            a = row.allocation
            if a != expected.get(a.allocation_id) or active is None or active.get('allocation_id') != a.allocation_id:
                raise ValueError('unplanned request')
            key = (a.allocation_id, row.lane, row.role, row.index)
            if key in seen:
                raise ValueError('duplicate request position')
            seen.add(key)
            lane = (a.allocation_id, row.lane)
            if lane not in lane_setup or lane in lane_close:
                raise ValueError('request outside open lane')
            positions = [(role, i) for role, count in (('WARMUP', config.warmup),
                ('CACHE_PREP', 2 if a.cache_condition == 'APPLICATION_WARM' else 0),
                ('MEASURED', config.requests_per_lane)) for i in range(count)]
            if lane_positions[lane] >= len(positions) or positions[lane_positions[lane]] != (row.role, row.index):
                raise ValueError('lane request order differs from plan')
            lane_positions[lane] += 1
            if lane in lane_finished and row.started_at < lane_finished[lane]:
                raise ValueError('overlapping requests in one serial lane')
            lane_finished[lane] = row.finished_at
            query = cases[a.workload].train if a.phase == 'TRAIN' else cases[a.workload].holdout
            if row.sql_sha256 != query.resolved_sha256:
                raise ValueError('request SQL differs from plan')
            if row.run is not None:
                result = row.run.execution.attempts[-1].execution
                matched = result_matches(result, references[(a.workload, a.phase)], query, config.analysis)
                if row.correctness != ('MATCHED' if matched else 'MISMATCHED'):
                    raise ValueError('false correctness claim')
                if row.run.decision.query_id in query_ids:
                    raise ValueError('reused query identity')
                query_ids.add(row.run.decision.query_id)
                decision = row.run.decision
                occurrence = row.index+1+(2 if row.role == 'MEASURED' and a.cache_condition == 'APPLICATION_WARM' else 0)
                if decision.query_occurrence != occurrence:
                    raise ValueError('unrecorded occurrence history')
                if (row.role == 'WARMUP' or row.role == 'MEASURED' and a.group in ('E0', 'TRAIN_S0')) and decision.selected_strategy_id != 'S0_NATIVE':
                    raise ValueError('Native allocation selected a different strategy')
                if row.role == 'MEASURED' and a.group in ('E2', 'E3', 'A2') and decision.model_version is not None:
                    raise ValueError('no-history group used a model')
                # Warmup -> reset -> optional measured cache fill is explicit in the protocol.
                if row.role != 'WARMUP':
                    state = lane_cache.get(lane, 'EMPTY')
                    if row.cache_before != state:
                        raise ValueError('unrecorded cache preparation/reset')
                    for attempt in row.run.execution.attempts:
                        r = attempt.execution
                        if r.served_from_cache and (r.cache_source_execution_id not in fills[lane] or r.cache_epoch != config.dataset.epoch):
                            raise ValueError('cache source outside measured/preparation history')
                        if r.strategy_id == 'S1_REPEAT_CACHE' and r.status.value == 'SUCCESS' and not r.served_from_cache:
                            fills[lane].add(r.execution_id)
                    if row.cache_entries_after and not fills[lane]:
                        raise ValueError('cache entry without successful fill')
                    lane_cache[lane] = 'READY' if row.cache_entries_after else 'EMPTY'
                for attempt in row.run.execution.attempts:
                    if attempt.execution.execution_id in execution_ids:
                        raise ValueError('reused physical execution')
                    execution_ids.add(attempt.execution.execution_id)
            if (row.role == 'MEASURED' and a.group in ('E4', 'A1') and row.workflow is not None
                    and row.workflow.trace.effective_mode != 'baseline' and row.inference is None):
                raise ValueError('missing frozen inference receipt')
            if row.inference is not None:
                receipt = row.inference
                if a.group not in ('E4', 'A1') or not frozen or receipt['model_fingerprint'] != bank.fingerprint:
                    raise ValueError('unexpected or unfrozen inference')
                advice = bank.advice(a, a.group, QueryAnalysisAgent().analyze(query.resolved()),
                    SystemSample.model_validate(receipt['sample']), datetime.fromisoformat(receipt['at']), registry)
                if (advice.model_dump(mode='json') if advice else None) != receipt['advice']:
                    raise ValueError('inference differs from independently restored model')
            request_map[key] = row
            rows.append(row)
        elif kind == 'training_update':
            key = (body['allocation_id'], body['lane'], 'MEASURED', body['index'])
            if key in update_ids or key not in request_map:
                raise ValueError('duplicate/unbound training update')
            update_ids.add(key)
            row = request_map[key]
            try:
                if row.workflow is None or len(row.experiences) != 1:
                    raise AdmissionError('MISSING_OR_MULTIPLE_EXPERIENCES')
                changed = bank.update(row.allocation, row.experiences[0], row.workflow,
                    started_at=row.started_at, finished_at=row.finished_at, elapsed_ns=row.elapsed_ns, registry=registry)
                status, reason = ('APPLIED' if changed else 'DUPLICATE'), None
            except AdmissionError as exc:
                status, reason = 'REJECTED', exc.reason
            if (body['status'], body['reason'], body['model_fingerprint']) != (status, reason, bank.fingerprint):
                raise ValueError('training update differs from independent replay')
        elif kind == 'frozen_model':
            if frozen or active is not None:
                raise ValueError('duplicate/mid-round freeze')
            if not {a.allocation_id for a in plan if a.phase == 'TRAIN'} <= set(rounds):
                raise ValueError('freeze before planned training completed')
            bank.freeze()
            restored = PolicyBank.restore(body['snapshot'])
            if bank.fingerprint != body['fingerprint'] or bank.fingerprint != restored.fingerprint:
                raise ValueError('frozen parameters differ from real evidence replay')
            artifact = json.loads((directory/'models.json').read_text())
            if artifact['evidence_domain'] != protocol['evidence_domain'] or PolicyBank.restore(artifact['bank']).fingerprint != bank.fingerprint:
                raise ValueError('saved model differs from freeze')
            frozen = True
        elif kind == 'round_complete':
            if active is None:
                raise ValueError('completion without active round')
            if active['phase'] == 'REFERENCE':
                if body != active:
                    raise ValueError('reference round mismatch')
            else:
                identifier = active['allocation_id']
                if any(body.get(k) != v for k, v in active.items()):
                    raise ValueError('round completion changed allocation')
                a = expected[identifier]
                required = {(identifier, lane, role, i) for lane in range(a.concurrency)
                    for role, count in (('WARMUP', config.warmup), ('CACHE_PREP', 2 if a.cache_condition == 'APPLICATION_WARM' else 0),
                                        ('MEASURED', config.requests_per_lane)) for i in range(count)}
                if {key for key in seen if key[0] == identifier} != required:
                    raise ValueError('completed round has missing/extra requests')
                lanes = {(identifier, i) for i in range(a.concurrency)}
                if not lanes <= lane_setup or not lanes <= lane_close:
                    raise ValueError('completed round has unclosed/missing lanes')
                measured = [r for r in rows if r.allocation.allocation_id == identifier and r.role == 'MEASURED']
                if body['measured_requests'] != len(measured):
                    raise ValueError('round result count mismatch')
                if a.phase == 'TRAIN' and not {k for k in required if k[2] == 'MEASURED'} <= update_ids:
                    raise ValueError('training round lacks update/rejection receipts')
                rounds[identifier] = body
            active = None
        elif kind == 'complete':
            if active is not None or starts != [a.allocation_id for a in plan] or set(rounds) != set(expected):
                raise ValueError('complete marker with incomplete planned coverage')
            if body['allocations'] != len(plan) or body['model_fingerprint'] != bank.fingerprint:
                raise ValueError('completion evidence mismatch')
        elif kind not in ('instance_occupancy', 'doctor', 'reference_execution', 'load_control', 'aborted', 'cleanup_failed', 'resource_rejected'):
            raise ValueError('unknown journal event')
    complete = events[-1]['kind'] == 'complete'
    measured = [r for r in rows if r.role == 'MEASURED' and r.allocation.phase == 'HOLDOUT']
    correctness = bool(measured) and all(r.success and r.correctness == 'MATCHED' for r in measured)
    load_counts = defaultdict(lambda: [0, 0])
    for row in measured:
        key = (row.allocation.workload, row.allocation.cache_condition, row.allocation.target_load,
               row.allocation.concurrency, row.allocation.group)
        load_counts[key][0] += row.observed_load == row.allocation.target_load
        load_counts[key][1] += 1
    load_ok = bool(load_counts) and all(ok/total >= .95 for ok, total in load_counts.values())
    required_models = {variant+':'+model_key(a) for a in plan if a.phase == 'HOLDOUT'
                       for variant in ('E4', 'A1') if variant in config.groups}
    counts = bank.counts()
    model_ready = all(key in counts and all(n >= config.min_samples for n in counts[key].values()) for key in required_models)
    real_evidence = protocol['evidence_domain'] == 'REAL_IOTDB'
    qualified = real_evidence and complete and correctness and load_ok and model_ready and config.test_blocks >= config.analysis.minimum_blocks
    summaries, contrasts = summarize(rows, rounds, config, qualified=qualified)
    report = {'schema_version': 1, 'protocol': config.protocol, 'config_sha256': config.checksum,
        'evidence_domain': protocol['evidence_domain'], 'execution_complete': complete,
        'analysis_qualified': qualified, 'stage_accepted': False, 'production_online_enabled': False,
        'gates': {'real_iotdb_evidence': real_evidence, 'planned_coverage': complete, 'correctness': correctness, 'target_load_95_percent': load_ok,
                  'training_min_samples': model_ready, 'minimum_blocks': config.test_blocks >= config.analysis.minimum_blocks},
        'counts': {'raw_requests': len(rows), 'heldout_requests': len(measured), 'completed_allocations': len(rounds),
                   'planned_allocations': len(plan), 'training_applied': len(bank.evidence)},
        'model_counts': counts, 'model_fingerprint': bank.fingerprint,
        'statistics': {'confidence': config.analysis.confidence, 'bootstrap_replicates': config.analysis.bootstrap_replicates,
            'seed': config.analysis.seed, 'mean_ci_method': 'percentile bootstrap of block means (lanes/requests/ABBA repeats clustered)',
            'contrast_ci_method': 'paired block mean differences; percentile bootstrap; candidate minus baseline',
            'throughput_ci_method': 'resample whole blocks; sum successes / sum allocation spans in each bootstrap draw',
            'std': 'sample standard deviation, ddof=1', 'percentiles': 'linear interpolation at (n-1)*q',
            'metric_scope': 'all allocated measured outcomes retained, including failures, fallback and low-confidence costs',
            'cpu_memory_scope': 'pre-request snapshot weighted means/percentiles; not process-exclusive or time-integrated',
            'multiple_comparisons': 'exploratory unadjusted intervals; no automatic benefit certification'},
        'cost_accounting': {'warmup_ms': sum(r.end_to_end_ms for r in rows if r.role == 'WARMUP'),
            'cache_preparation_ms': sum(r.end_to_end_ms for r in rows if r.role == 'CACHE_PREP'),
            'training_update_ms': sum(e['body']['elapsed_ns']/1e6 for e in events if e['kind'] == 'training_update'),
            'lane_setup_ms': sum(e['body']['elapsed_ns']/1e6 for e in events if e['kind'] == 'lane_setup'),
            'lane_close_ms': sum(e['body']['elapsed_ns']/1e6 for e in events if e['kind'] == 'lane_close')},
        'groups': summaries, 'contrasts': contrasts, 'uncontrolled_factors': config.uncontrolled_factors,
        'server_execution_ms': None, 'server_execution_status': 'UNKNOWN',
        'end_to_end_definition': 'outer request timer includes safety snapshot acquisition and full selected service/workflow return; connection/setup, explicit warmup/cache prep, offline fitting and journal serialization reported separately'}
    return report, rows


def export(directory, destination=None):
    directory = Path(directory)
    report, rows = analyze(directory)
    destination = Path(destination) if destination is not None else directory/'analysis'
    destination.mkdir(parents=True, exist_ok=False)
    (destination/'report.json').write_text(json.dumps(report, indent=2, sort_keys=True, allow_nan=False)+'\n')
    with (destination/'requests.csv').open('w', newline='') as stream:
        writer = csv.DictWriter(stream, fieldnames=CSV_FIELDS)
        writer.writeheader()
        writer.writerows(flat(row) for row in rows)
    fields = ['workload', 'cache_condition', 'target_load', 'concurrency', 'group', 'metric',
              'n', 'missing', 'mean', 'sample_std', 'p50', 'p95', 'p99', 'ci_low', 'ci_high']
    with (destination/'summary.csv').open('w', newline='') as stream:
        writer = csv.DictWriter(stream, fieldnames=fields)
        writer.writeheader()
        for group in report['groups']:
            for metric, values in group['metrics'].items():
                ci = values['mean_ci']
                writer.writerow({**{k: group[k] for k in fields[:5]}, 'metric': metric,
                    **{k: v for k, v in values.items() if k != 'mean_ci'},
                    'ci_low': ci[0] if ci else None, 'ci_high': ci[1] if ci else None})
    text = ['# Phase 8 experiment report', '', f"Evidence: **{report['evidence_domain']}**",
        f"Configuration SHA-256: `{report['config_sha256']}`", '',
        f"Execution complete: {report['execution_complete']}; analysis qualified: {report['analysis_qualified']}.",
        '**Stage acceptance and production deployment remain disabled.**', '',
        '| Workload / cache / load / concurrency | Group | Requests | E2E mean ms | P95 ms | Throughput req/s |',
        '|---|---|---:|---:|---:|---:|']
    for group in report['groups']:
        m = group['metrics']['end_to_end_ms']
        label = ' / '.join(str(group[k]) for k in ('workload', 'cache_condition', 'target_load', 'concurrency'))
        text.append(f"| {label} | {group['group']} | {group['requests']} | {m['mean']:.3f} | {m['p95']:.3f} | {group['throughput']['successful_requests_per_second']:.3f} |")
    text.extend(['', 'Confidence intervals use predeclared paired/block percentile bootstrap; missing intervals mean gates are unmet.',
        'Server execution latency: UNKNOWN. Client timings are not substituted for it.', '', '## Limitations', ''])
    text.extend('- '+item for item in report['uncontrolled_factors'])
    text.extend(['', '## Interpretation to complete after independent review', '',
        '- Repeated independent sessions and workload generalization: UNKNOWN.',
        '- Benefit versus Native and fixed S1: inspect paired intervals; no automatic success claim.',
        '- A1 versus E4 and A2 versus E4: report effect and uncertainty, including null/negative results.',
        '- Failed/aborted runs, discrepancies and unavailable measurements: retain and explain.', ''])
    (destination/'report.md').write_text('\n'.join(text))
    plot(report, destination)
    structure = {'schema_version': 1, 'request_csv_columns': CSV_FIELDS, 'summary_csv_columns': fields,
        'report_keys': sorted(report), 'group_metric_keys': sorted(METRICS),
        'files': sorted([p.name for p in destination.iterdir()]+['structure.json'])}
    (destination/'structure.json').write_text(json.dumps(structure, indent=2)+'\n')
    return report


def plot(report, destination):
    groups = report['groups']
    for start in range(0, max(1, len(groups)), 28):
        suffix = '' if start == 0 else f'-{start//28+1:02d}'
        _plot_page(report, destination, groups[start:start+28], suffix)


def _plot_page(report, destination, groups, suffix):
    import matplotlib
    matplotlib.use('Agg')
    import matplotlib.pyplot as plt
    fig, ax = plt.subplots(figsize=(max(9, len(groups)*.5), 5))
    if groups:
        means = [g['metrics']['end_to_end_ms']['mean'] for g in groups]
        ax.bar(range(len(groups)), means, color='#377ba8')
        for i, (group, mean) in enumerate(zip(groups, means)):
            ci = group['metrics']['end_to_end_ms']['mean_ci']
            if ci:
                ax.vlines(i, ci[0], ci[1], color='black', linewidth=1)
        ax.set_xticks(range(len(groups)), [f"{g['group']}\n{g['workload']}\n{g['cache_condition']}\n{g['target_load']}/c{g['concurrency']}" for g in groups], rotation=70, ha='right', fontsize=7)
    else:
        ax.text(.5, .5, 'No completed holdout measurements', ha='center', transform=ax.transAxes)
    ax.set_ylabel('Client end-to-end latency (ms)')
    ax.set_title(f"{report['evidence_domain']} — phase 8; qualified={report['analysis_qualified']}")
    ax.grid(axis='y', alpha=.2)
    fig.tight_layout()
    fig.savefig(destination/f'latency{suffix}.svg')
    fig.savefig(destination/f'latency{suffix}.png', dpi=150)
    plt.close(fig)
    fig, axes = plt.subplots(2, 2, figsize=(max(10, len(groups)*.5), 8))
    specs = [('db_cpu_percent', 'DB host CPU (%)'), ('db_memory_percent', 'DB host memory (%)'),
             ('decision_overhead_ms', 'Decision overhead (ms)'), ('throughput', 'Successful requests / second')]
    labels = [f"{g['group']}\n{g['workload']}\n{g['cache_condition'].removeprefix('APPLICATION_')}\n{g['target_load']}/c{g['concurrency']}" for g in groups]
    for ax, (metric, title) in zip(axes.flat, specs):
        values = [(g['throughput']['successful_requests_per_second'] if metric == 'throughput' else
                   g['metrics'][metric]['mean']) for g in groups]
        present = [(i, value) for i, value in enumerate(values) if value is not None]
        ax.bar([i for i, _ in present], [v for _, v in present], color='#377ba8')
        ax.set_xticks(range(len(groups)), labels, rotation=70, ha='right', fontsize=6)
        ax.set_title(title)
        ax.grid(axis='y', alpha=.2)
    fig.suptitle(report['evidence_domain']+' — pre-request whole-host metrics; separate cache/load cells')
    fig.tight_layout()
    fig.savefig(destination/f'resources{suffix}.svg')
    fig.savefig(destination/f'resources{suffix}.png', dpi=150)
    plt.close(fig)
