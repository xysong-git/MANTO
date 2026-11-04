"""Audit randomized journal; train/holdout gate and fixed-action contrasts only."""
import argparse
import hashlib
from collections import Counter, defaultdict
import json
from pathlib import Path
import random

from tsdb_optimizer.bandit.workload_audit import verify_import_certificate
from tsdb_optimizer.bandit.exploration import Assignment, Trial, STRATEGIES, summarize
from tsdb_optimizer.bandit.randomized_model import evaluate_randomized
from tsdb_optimizer.bandit.evaluation import audit
from tsdb_optimizer.schemas.learning import digest
from tsdb_optimizer.strategies import build_default_registry


def qualified_load_coverage(trials, min_samples=30):
    counts = defaultdict(Counter)
    for t in trials:
        if t.experience.sample_status.value == 'VALID' and t.experience.context.load_bucket != 'UNKNOWN':
            counts[t.experience.context.load_bucket][f'{t.assignment.phase}/{t.assignment.strategy_id}'] += 1
    required = [f'{phase}/{arm}' for phase in ('train', 'holdout') for arm in STRATEGIES]
    qualified = sorted(bucket for bucket, cell in counts.items()
                       if all(cell[key] >= min_samples for key in required))
    return {'valid_counts': {bucket: dict(cell) for bucket, cell in counts.items()},
            'qualified_buckets': qualified, 'min_samples_per_phase_arm': min_samples}


def analyze(path):
    assignments, trials, rounds = {}, [], []
    streams = {}
    protocol = None
    certificate = None
    final_summary = None
    last_kind = None
    for line in path.read_text().splitlines():
        item=json.loads(line)
        kind,body=item['kind'],item['body']
        if final_summary is not None: raise ValueError('records after final summary')
        last_kind = kind
        if kind=='protocol':
            if protocol is not None: raise ValueError('duplicate protocol')
            protocol=body
        elif kind=='dataset_import_certification':
            if certificate is not None or assignments or protocol is None:
                raise ValueError('import certification must precede assignments')
            certificate=verify_import_certificate(Path(__file__).resolve().parents[1],body,
                protocol['server_endpoint'],protocol['epoch'])
        elif kind=='aborted': raise ValueError('aborted experiment cannot certify readiness')
        elif kind=='assignment':
            if protocol is None: raise ValueError('assignment precedes protocol')
            a=Assignment.model_validate(body)
            if protocol.get('protocol') == 'randomized-cache-state-v1':
                expected_seed = protocol['seed'] + (100 if a.phase == 'holdout' else 0) + a.background_workers*10 + (a.cache_state_before == 'READY')
                if a.seed != expected_seed:
                    raise ValueError('assignment seed differs from protocol')
            if a.assignment_id in assignments: raise ValueError('duplicate assignment')
            key=(a.phase,a.cache_state_before,a.background_workers)
            stream=streams.setdefault(key,{'rng':random.Random(a.seed),'next':0,'seed':a.seed})
            if a.seed!=stream['seed'] or a.index!=stream['next']:
                raise ValueError('seed/index mismatch')
            if stream['rng'].choice(STRATEGIES)!=a.strategy_id:
                raise ValueError('recorded action differs from seeded random assignment')
            stream['next']+=1
            assignments[a.assignment_id]=a
        elif kind=='trial':
            t=Trial.model_validate(body)
            if assignments.get(t.assignment.assignment_id)!=t.assignment:
                raise ValueError('trial lacks prior identical assignment')
            trials.append(t)
        elif kind=='round_complete': rounds.append(body)
        elif kind=='summary': final_summary=body
    if protocol is None or not protocol['operator_immutable_guarantee']:
        raise ValueError('missing immutable dataset declaration')
    if len(assignments)!=len(trials): raise ValueError('unresolved assignments')
    if any(t.experience.context.dataset_epoch!=protocol['epoch'] for t in trials):
        raise ValueError('epoch mismatch')
    if last_kind!='summary' or final_summary is None:
        raise ValueError('incomplete experiment journal')
    if any(t.experience.context.load_protocol != protocol.get('load_protocol', 'resource-max-v1') for t in trials):
        raise ValueError('load protocol mismatch')
    report=summarize(trials)
    report_load_protocol = protocol.get('load_protocol', 'resource-max-v1')
    planned_workers = protocol.get('background_workers_planned')
    if planned_workers is not None and planned_workers not in ([0], [0,3]):
        raise ValueError('invalid planned background workers')
    if protocol.get('protocol') == 'randomized-cache-state-v1':
        # Legacy v1 did not persist background intent. Still require all four
        # foreground cells; never infer full planned coverage from observed cells.
        workers = planned_workers if planned_workers is not None else [0]
        required_cells = {f'{phase}/{cache}/background={w}'
            for phase in ('train','holdout') for cache in ('EMPTY','READY') for w in workers}
        if not required_cells.issubset(report['cells']) or (planned_workers is not None
                and set(report['cells']) != required_cells):
            raise ValueError('missing or unexpected planned experiment cells')
    if any(final_summary.get(key)!=value for key,value in report.items()):
        raise ValueError('final summary differs from raw trials')
    if any(r['elapsed_s']>60 for r in rounds):
        raise ValueError('round exceeded allowed duration')
    if len(rounds)!=len(report['cells'])*protocol.get('rounds_per_cell',1):
        raise ValueError('missing round completion records')
    if 'rounds_per_cell' in protocol:
        expected_rounds = {(key, i) for key in report['cells'] for i in range(protocol['rounds_per_cell'])}
        actual_rounds = [(f"{r['phase']}/{r['cache']}/background={r['workers']}", r['round_index']) for r in rounds]
        if len(set(actual_rounds)) != len(actual_rounds) or set(actual_rounds) != expected_rounds:
            raise ValueError('duplicate or missing round completion identity')
    if protocol.get('workload'):
        expected = protocol['workload']['sql_sha256']
        if expected != protocol['sql_sha256'] or any(t.experience.query_hash != expected for t in trials):
            raise ValueError('protocol workload identity mismatch')
    train=[t.experience for t in trials if t.assignment.phase=='train']
    report['legacy_model_readiness']=audit(train,build_default_registry())
    report['randomization_audit']={'verified':True,'unique_assignments':len(assignments),
        'conditional_propensity':.5,'assignment_precedes_execution':True,
        'journal_sha256':hashlib.sha256(path.read_bytes()).hexdigest()}
    report['load_protocol']=report_load_protocol
    report['rounds']=rounds
    report['measured_load_buckets']=dict(Counter(t.experience.context.load_bucket for t in trials))
    report['qualified_load_coverage']=qualified_load_coverage(trials)
    report['observed_background_inflight']=dict(Counter(t.background_inflight_before for t in trials))
    report['all_samples_valid']=all(t.experience.sample_status.value=='VALID' for t in trials)
    # Bootstrap differences only within pre-action randomized cells. No policy training claim.
    for i,(key,cell) in enumerate(sorted(report['cells'].items())):
        data=cell['latencies_ms']
        if not cell['per_arm_sample_gate'] or len(data)!=2: continue
        rng=random.Random(20261102+i)
        left,right=(data[s] for s in STRATEGIES)
        diffs=[]
        for _ in range(2000):
            a=sum(rng.choices(left,k=len(left)))/len(left)
            b=sum(rng.choices(right,k=len(right)))/len(right)
            diffs.append(b-a)
        diffs.sort()
        cell['confidence_interval']={'method':'independent-arm percentile bootstrap; IID assumption',
            'replicates':2000,'level':.95,'s1_minus_s0_mean_ms':[diffs[49],diffs[1949]],
            'limitations':'serial autocorrelation and single-session drift may invalidate IID assumption'}
    if report['all_samples_valid'] and {t.assignment.phase for t in trials} == {'train','holdout'}:
        report['randomized_model_evaluation']=evaluate_randomized(trials,build_default_registry())
    else:
        report['randomized_model_evaluation']=None
    report['remaining_blockers']=[
        'CONTROLLED_PREACTION_MODEL_NOT_DEPLOYMENT_CERTIFIED',
        'ADAPTIVE_POLICY_BENEFIT_NOT_ESTABLISHED',
    ]
    report['planned_cell_coverage_verified'] = (
        protocol.get('protocol') == 'randomized-cache-state-v1' and planned_workers is not None)
    if not report['planned_cell_coverage_verified']:
        report['remaining_blockers'].append('PLANNED_EXPERIMENT_COVERAGE_NOT_ATTESTED')
    report['db_host_metrics_verified'] = bool(trials) and bool(protocol.get('local_db_identity')) and all(
        t.experience.before is not None and t.experience.before.db_host.source_type.value == 'db_host'
        and t.experience.before.db_host.is_usable(now=t.experience.record.started_at)
        and t.experience.context.load_source == 'db_host' for t in trials)
    report['dataset_import_certification']=certificate
    if '/heavy_scale/' in protocol.get('sql_file','') and certificate is None:
        report['remaining_blockers'].append('HEAVY_SOURCE_IMPORT_NOT_FULLY_VERIFIED')
    if not report['db_host_metrics_verified']:
        report['remaining_blockers'].append('DB_HOST_METRICS_UNAVAILABLE')
    if not report['all_samples_valid']:
        report['remaining_blockers'].append('INVALID_RANDOMIZED_TRIALS_PREVENT_POLICY_EVALUATION')
    if len(report['qualified_load_coverage']['qualified_buckets'])<2:
        report['remaining_blockers'].append('INSUFFICIENT_MEASURED_LOAD_BUCKET_COVERAGE')
    if not all(c['per_arm_sample_gate'] for c in report['cells'].values()):
        report['remaining_blockers'].append('INSUFFICIENT_SAMPLES_IN_SOME_CELLS')
    return report


if __name__=='__main__':
    p=argparse.ArgumentParser()
    p.add_argument('journal',type=Path)
    p.add_argument('--output',type=Path,required=True)
    args=p.parse_args()
    report=analyze(args.journal)
    args.output.write_text(json.dumps(report,ensure_ascii=False,indent=2,allow_nan=False)+'\n')
    print(json.dumps({'trials':report['unique_executions'],'randomization_verified':True,
        'load_buckets':report['measured_load_buckets'],'remaining_blockers':report['remaining_blockers']}))
