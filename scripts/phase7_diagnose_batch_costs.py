"""Read-only diagnosis of completed evidence; no SQL, training or acceptance changes.

Stage differences are trace intervals, not causal CPU/profiling measurements.
Run after the independent acceptance analyzer; this is a separate diagnostic.
"""
import argparse
from collections import defaultdict
from datetime import datetime
import hashlib
import json
from pathlib import Path
import statistics

from tsdb_optimizer.bandit.context import encode
from tsdb_optimizer.bandit.episodes import BatchPolicy
from tsdb_optimizer.schemas.monitoring import SystemSample
from tsdb_optimizer.schemas.query_features import QueryFeatures


def describe(values):
    values = sorted(values)
    return {'count': len(values), 'mean': statistics.mean(values),
            'median': statistics.median(values),
            'p95_nearest_lower': values[int(.95 * (len(values)-1))], 'max': values[-1]}


def diagnose(path):
    groups = defaultdict(list)
    scores = defaultdict(list)
    tails = defaultdict(list)
    active = selected = frozen = None
    terminal = False
    sha = hashlib.sha256()
    requests = batches = 0
    with path.open('rb') as source:
        for line in source:
            sha.update(line)
            event = json.loads(line)
            kind, body = event['kind'], event['body']
            if terminal:
                raise ValueError('events after complete')
            if kind == 'batch_assignment':
                if active is not None:
                    raise ValueError('unclosed batch')
                active = body
                totals = defaultdict(float)
                count = 0
            elif kind == 'frozen_model':
                frozen = BatchPolicy.restore(body['snapshot'])
                if frozen.fingerprint != body['fingerprint']:
                    raise ValueError('frozen fingerprint mismatch')
            elif kind == 'batch_selected':
                if active is None:
                    raise ValueError('selection without assignment')
                selected = body
                totals['selection_ms'] = body['selection_ns']/1e6
                for name, value in body.get('selection_timing', {}).items():
                    totals['detail_selection_'+name.removesuffix('_ns')+'_ms'] = value/1e6
                if active['phase'] == 'HOLDOUT' and active['policy'] == 'ADAPTIVE':
                    if frozen is None:
                        raise ValueError('holdout before freeze')
                    vector = encode(QueryFeatures.model_validate(body['features']),
                        SystemSample.model_validate(body['selection_sample']),
                        at=datetime.fromisoformat(body['context_at']))
                    model = frozen.models[str(active['size'])]
                    scores[f"{active['load']}/{active['size']}"].append({
                        arm: model.predict(arm, vector, ucb=False)
                        for arm in model.snapshot().arms})
            elif kind == 'request':
                if active is None or selected is None or body['index'] != count:
                    raise ValueError('request outside ordered batch')
                count += 1
                requests += 1
                previous = 0.
                components = {}
                for entry in body['workflow']['trace']['events']:
                    elapsed = entry['elapsed_ms']
                    if elapsed < previous:
                        raise ValueError('trace clock reversed')
                    components[entry['state']+'_ms'] = elapsed-previous
                    previous = elapsed
                elapsed = body['elapsed_ns']/1e6
                components['outside_trace_ms'] = elapsed-previous
                if components['outside_trace_ms'] < 0:
                    raise ValueError('trace exceeds request')
                for name, value in components.items():
                    totals[name] += value
                timing = body['workflow'].get('request_timing')
                if timing is not None:
                    for name, value in timing.items():
                        totals['detail_'+name] += value
                    totals['detail_outer_wrapper_ms'] += elapsed-timing['total_ms']
                    totals['detail_body_outside_trace_ms'] += timing['body_ms']-previous
                    parsed = next(e for e in body['workflow']['trace']['events'] if e['state']=='PARSED')
                    for name in ('parsing_ms', 'query_persistence_ms'):
                        totals['detail_'+name] += parsed['details'][name]
                totals['adapter_ms'] += sum(a['execution']['metrics']['client_wall_ms']
                    for a in body['workflow']['run']['execution']['attempts'])
                key = f"{active['phase']}/{active['load']}/{active['size']}/{active['policy']}"
                tails[key].append({'block': active['block'], 'index': body['index'],
                    'elapsed_ms': elapsed, 'components': components})
            elif kind == 'batch_closed':
                if active is None or selected is None or count != active['size']:
                    raise ValueError('incomplete batch')
                if body['batch_id'] != active['batch_id']:
                    raise ValueError('batch identity mismatch')
                accounted = sum(v for k, v in totals.items()
                                if k != 'adapter_ms' and not k.startswith('detail_'))
                if abs(accounted-body['service_ms']) > 1e-6:
                    raise ValueError('stage accounting does not reconcile')
                totals['service_ms'] = body['service_ms']
                key = f"{active['phase']}/{active['load']}/{active['size']}/{active['policy']}"
                groups[key].append({'block': active['block'], **totals})
                batches += 1
                active = selected = None
            elif kind == 'complete':
                if active is not None:
                    raise ValueError('complete with open batch')
                terminal = True
    if not terminal or frozen is None:
        raise ValueError('completed frozen study required')
    cells = {key: {name: describe([row[name] for row in rows]) for name in rows[0]
                   if name != 'block'} for key, rows in groups.items()}
    differences = {}
    for load in ('LOW', 'MEDIUM'):
        for size in (2, 8):
            adaptive = cells[f'HOLDOUT/{load}/{size}/ADAPTIVE']
            fixed = cells[f'HOLDOUT/{load}/{size}/S1_REPEAT_CACHE']
            differences[f'{load}/{size}'] = {name: adaptive[name]['mean']-fixed[name]['mean']
                                             for name in adaptive}
    return {'journal_sha256': sha.hexdigest(), 'batches': batches, 'requests': requests,
        'frozen_fingerprint': frozen.fingerprint, 'cells': cells,
        'adaptive_minus_fixed_s1_mean_ms': differences,
        'frozen_mean_reward_predictions': {key: {arm: statistics.mean(r[arm] for r in rows)
            for arm in rows[0]} for key, rows in scores.items()},
        'largest_requests': {key: sorted(rows, key=lambda r: r['elapsed_ms'], reverse=True)[:5]
                             for key, rows in tails.items()},
        'limitations': ['Diagnostic only; does not replace independent acceptance replay.',
            'Adapter time overlaps EXECUTED; do not add it twice.',
            'detail_* fields overlap trace/outer intervals; not additional service cost.',
            'Old outside_trace includes admission/open/trace persistence/close; causes UNKNOWN.',
            'No samples removed, no model updates, no post-hoc acceptance threshold changes.']}


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('journal', type=Path)
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    result = diagnose(args.journal)
    with args.output.open('x') as target:
        json.dump(result, target, indent=2, allow_nan=False)
        target.write('\n')
    print(json.dumps({k: result[k] for k in ('journal_sha256', 'batches', 'requests')}))
