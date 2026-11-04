"""Audit actual three-policy executions; no counterfactual/IPS reward estimates."""
import argparse
from collections import Counter,defaultdict
import hashlib
import json
import math
from pathlib import Path
import random
import statistics
from pydantic import BaseModel,ConfigDict,Field,model_validator
from typing import Literal
from tsdb_optimizer.bandit.context import encode, partition_key
from tsdb_optimizer.bandit.model import OfflineLinUCB
from tsdb_optimizer.bandit.completed_request import CompletedRequest, E2E_REWARD_VERSION
from tsdb_optimizer.experiments.controlled_policy import train, fingerprint
from tsdb_optimizer.orchestration.models import WorkflowResult
from tsdb_optimizer.schemas.learning import Experience
from tsdb_optimizer.sql_guard import sql_sha256
from tsdb_optimizer.strategies import build_default_registry

POLICIES=('S0_NATIVE','S1_REPEAT_CACHE','ADAPTIVE')
CELLS=[(l,c) for l in ('LOW','MEDIUM') for c in ('EMPTY','READY')]


class Trial(BaseModel):
    model_config=ConfigDict(extra='forbid',allow_inf_nan=False)
    phase: Literal['train','test']
    target_load: Literal['LOW','MEDIUM']
    cache: Literal['EMPTY','READY']
    policy: Literal['S0_NATIVE','S1_REPEAT_CACHE','ADAPTIVE']
    assignment: dict
    e2e_ms: float=Field(ge=0)
    workflow: WorkflowResult
    experience: Experience
    completed_request: CompletedRequest | None = None

    @model_validator(mode='after')
    def coherent(self):
        w,e=self.workflow,self.experience
        if self.completed_request is not None:
            self.completed_request.validate_evidence(e,w)
            if self.e2e_ms != self.completed_request.end_to_end_ms:
                raise ValueError("trial and completed request timer differ")
        if (e.sample_status.value!='VALID' or e.record.runtime_result_check!='MATCHED' or w.run is None
            or w.trace.warnings or w.run.execution.warnings or len(w.run.execution.attempts)!=1
            or w.run.execution.fallback_used or any(f.warnings for f in w.feedback)):
            raise ValueError('unqualified study trial; do not filter outcomes')
        r=w.run.execution.attempts[0].execution
        if r.execution_id!=e.execution_id or r.strategy_id!=e.strategy_id or r.query_id!=e.query_id:
            raise ValueError('workflow/experience identity mismatch')
        if (e.context.load_source!='db_host' or e.context.load_protocol!='cpu-activity-v3'
            or e.context.cache_condition!='controlled-'+self.cache.lower()):
            raise ValueError('study context mismatch')
        if self.e2e_ms+.1<max(r.metrics.client_wall_ms,w.trace.events[-1].elapsed_ms):
            raise ValueError('end-to-end timer excludes measured workflow stages')
        if self.policy!='ADAPTIVE' and r.strategy_id!=self.policy:raise ValueError('fixed arm not executed')
        if self.assignment.get('phase')!=self.phase or self.assignment.get('load')!=self.target_load or self.assignment.get('cache')!=self.cache or self.assignment.get('policy')!=self.policy:
            raise ValueError('assignment mismatch')
        return self


def contrast(blocks, comparator, *, replicates=4000, seed=20261104):
    strata=defaultdict(list)
    for (load,cache,block),rows in blocks.items():
        strata[(load,cache)].append((block,rows['ADAPTIVE'].e2e_ms-rows[comparator].e2e_ms))
    if any(len(v)<30 for v in strata.values()) or len(strata)!=4:
        return {'qualified':False,'reason':'INSUFFICIENT_PAIRED_BLOCKS'}
    values=[v for group in strata.values() for _,v in group]
    rng=random.Random(seed);bootstrap=[]
    for _ in range(replicates):
        sampled=[v for group in strata.values() for _,v in rng.choices(group,k=len(group))]
        bootstrap.append(statistics.mean(sampled))
    bootstrap.sort()
    lo=bootstrap[int(.005*replicates)];hi=bootstrap[int(.995*replicates)-1]
    batches=[statistics.mean(v for group in strata.values() for b,v in group if i*10<=b<(i+1)*10) for i in range(3)]
    return {'qualified':True,'paired_blocks':len(values),'adaptive_minus_comparator_mean_ms':statistics.mean(values),
        'ci99_ms':[lo,hi],'three_batch_mean_differences_ms':batches,
        'within_session_benefit':hi<0 and all(v<0 for v in batches),
        'method':'paired percentile bootstrap stratified by load/cache; serial dependence remains a limitation'}


def analyze(path):
    events=[json.loads(l) for l in path.read_text().splitlines()]
    if not events or events[0]['kind']!='protocol':raise ValueError('missing protocol')
    protocol=events[0]['body']
    if (protocol['protocol'] not in ('controlled-e2e-v2','controlled-e2e-v3','controlled-e2e-v4','controlled-e2e-v5','controlled-e2e-v6','controlled-e2e-v7','controlled-e2e-v8','controlled-e2e-v9') or protocol['load_protocol']!='cpu-activity-v3'
        or protocol.get('cpu_measurement')!='cpu-times-instance-v1'):raise ValueError('unknown protocol')
    if protocol['protocol'] in ('controlled-e2e-v3','controlled-e2e-v4','controlled-e2e-v5','controlled-e2e-v6','controlled-e2e-v7','controlled-e2e-v8','controlled-e2e-v9') and (protocol.get('snapshot_mode')!='latest' or protocol.get('snapshot_ttl_ms')!=500 or protocol.get('background_interval_s')!=.2):raise ValueError('changed background sampling protocol')
    if protocol['protocol'] in ('controlled-e2e-v4','controlled-e2e-v5','controlled-e2e-v6','controlled-e2e-v7','controlled-e2e-v8','controlled-e2e-v9') and protocol.get('sampling_schedule')!='fixed-rate-db-first-v1':raise ValueError('unknown sampling schedule')
    if not 50<=protocol.get('cpu_setpoint_percent',60)<=70:raise ValueError('unauthorized CPU setpoint')
    objective=protocol['protocol'] in ('controlled-e2e-v6','controlled-e2e-v7','controlled-e2e-v8','controlled-e2e-v9')
    if protocol['protocol'] in ('controlled-e2e-v7','controlled-e2e-v8','controlled-e2e-v9') and (protocol.get('request_snapshot_max_age_ms')!=250 or protocol.get('request_snapshot_wait_limit_s')!=.5):
        raise ValueError('changed bounded request snapshot wait')
    if objective and protocol.get('reward_version')!=E2E_REWARD_VERSION:
        raise ValueError('completed reward version mismatch')
    if objective and protocol.get('workload_scale') not in ('small_scale','medium_scale','large_scale'):
        raise ValueError('unknown workload scale')
    ambient=protocol['protocol'] in ('controlled-e2e-v8','controlled-e2e-v9')
    release=protocol['protocol']=='controlled-e2e-v9'
    if release and protocol.get('release_training_evidence') is not True:raise ValueError('missing release protocol')
    if ambient and (protocol.get('study_scope')!='AMBIENT_STRATEGY_APPLICABILITY_ONLY' or protocol.get('cpu_load_enabled') is not False or protocol.get('cpu_source')!='NATURAL_HOST_ACTIVITY'):
        raise ValueError('invalid ambient study scope')
    cells=[(l,c) for l in (('LOW',) if ambient else ('LOW','MEDIUM')) for c in ('EMPTY','READY')]
    loads=('LOW',) if ambient else ('LOW','MEDIUM')
    plan=protocol['benefit_analysis']
    if (plan['replicates']!=4000 or plan['confidence']!=.99 or plan['seed']!=20261104
        or plan['required_comparators']!=list(POLICIES[:2])):raise ValueError('changed benefit analysis')
    if protocol['production_online_enabled'] or protocol['max_query_connections']!=1:
        raise ValueError('unsafe protocol')
    if (protocol['minimum_per_arm_training']!=30 or protocol['minimum_test_blocks']!=30
        or protocol['pilot_repeats']!=2 or protocol['stability_min_samples']!=20
        or protocol['stability_fraction']!=.8 or protocol['cpu_target_percent']!=[50,70]
        or protocol['cpu_stop_percent']!=80 or protocol['cpu_duration_max_s']!=30 or protocol['query_timeout_ms']!=30000 or protocol['round_limit_s']!=60):
        raise ValueError('changed qualification limits')
    if sql_sha256(protocol['train_sql'])==sql_sha256(protocol['test_sql']):raise ValueError('held-out query must differ')
    registry=build_default_registry(cache_epoch=protocol['epoch'])
    streams={cell:random.Random(protocol['seed']+i) for i,cell in enumerate(cells)}
    completed_training={}
    indexes=Counter();training=[];tests=[];seen=set();pending=None;active=None;terminal=None
    memory_release_seen=False
    storage_seen=False;first_visits=set();pilot_gate=None;pilots=[];models=None;frozen=None;rounds=[];recorded_gate=None;training_gate=False
    round_rng=random.Random(protocol['seed']);planned_train=[]
    for repeat in range(protocol['training_rounds_per_cell']):
        order=cells.copy();round_rng.shuffle(order)
        planned_train.extend((repeat,*cell) for cell in order)
    test_rng=random.Random(protocol['seed']+1);planned_test=[];planned_policies={}
    for block in range(protocol['test_blocks_per_cell']):
        order=cells.copy();test_rng.shuffle(order)
        for load,cache in order:
            policies=list(POLICIES);test_rng.shuffle(policies)
            planned_test.append((block,load,cache));planned_policies[(block,load,cache)]=policies
    train_round_index=test_round_index=0
    for item in events[1:]:
        if terminal is not None:raise ValueError('records after terminal')
        kind,b=item['kind'],item['body']
        if ambient and b.get('cpu_control'):raise ValueError('ambient study contains artificial CPU load')
        if kind=='aborted':raise ValueError('aborted study cannot qualify')
        if kind=='round_start':
            if active is not None or pending is not None:raise ValueError('overlapping round')
            active=b
            if b['phase']=='train':
                if not pilot_gate or 'train' not in first_visits:raise ValueError('training before preparation')
                if tuple(b[k] for k in ('repeat','load','cache'))!=planned_train[train_round_index]:raise ValueError('training schedule differs')
                train_round_index+=1
            elif b['phase']=='test':
                if 'test' not in first_visits or models is None or tuple(b[k] for k in ('block','load','cache'))!=planned_test[test_round_index]:raise ValueError('test schedule differs')
                test_round_index+=1
            elif b['phase']!='pilot':raise ValueError('unknown round phase')
        elif kind=='round_complete':
            if active is None or pending is not None or b['phase']!=active['phase'] or not 0<=b['elapsed_s']<=60:raise ValueError('invalid round completion')
            if any(b.get(k)!=active[k] for k in ('load','cache','block') if k in b):raise ValueError('round completion identity mismatch')
            if any(c['cpu_percent']>80 or c['memory_percent']>90 or c['elapsed_s']>30.5 for c in b.get('cpu_control',[])):raise ValueError('unsafe CPU control transcript')
            rounds.append(b);active=None
        elif kind=='pilot':
            if active is None or active['phase']!='pilot' or (b['load'],b['repeat'])!=(active['load'],active['repeat']):raise ValueError('pilot outside assigned round')
            if len(b['samples'])!=20:raise ValueError('pilot sample count')
            from tsdb_optimizer.schemas.monitoring import SystemSnapshot
            samples=[SystemSnapshot.model_validate(s) for s in b['samples']]
            if any(s.source_type.value!='db_host' or not s.valid or s.cpu_measurement!='cpu-times-instance-v1'
                or s.cpu_percent>80 or s.memory_percent>90 or s.disk_usage_percent>90 for s in samples):raise ValueError('invalid pilot source')
            passed=sum(s.cpu_percent<50 if b['load']=='LOW' else 50<=s.cpu_percent<=70 for s in samples)>=16
            if passed!=b['passed']:raise ValueError('pilot gate differs from data')
            pilots.append(b)
        elif kind=='stability_gate':
            actual=len(pilots)==2*len(loads) and {(p['load'],p['repeat']) for p in pilots}=={(l,r) for l in loads for r in range(2)} and all(p['passed'] for p in pilots)
            if pilot_gate is not None or b['passed']!=actual:raise ValueError('incorrect stability gate')
            pilot_gate=actual
        elif kind=='assignment':
            if pending is not None or active is None or b['phase']!=active['phase'] or (b['load'],b['cache'])!=(active['load'],active['cache']):raise ValueError('invalid assignment lifecycle')
            if b['phase']=='train':
                key=(b['load'],b['cache'])
                if b['index']!=indexes[key] or b['propensity']!=.5 or b['policy']!=streams[key].choice(list(POLICIES[:2])):raise ValueError('random training transcript differs')
                indexes[key]+=1
            else:
                if b['block']!=active['block'] or b['policy']!=planned_policies[(b['block'],b['load'],b['cache'])][b['position']]:raise ValueError('randomized test order differs')
            pending=b
        elif kind=='trial':
            t=Trial.model_validate(b);e=t.experience
            if objective != (t.completed_request is not None):raise ValueError("missing or mixed objective receipt")
            if protocol['protocol'] in ('controlled-e2e-v3','controlled-e2e-v4','controlled-e2e-v5','controlled-e2e-v6','controlled-e2e-v7','controlled-e2e-v8','controlled-e2e-v9'):
                if any(s.stale_after_ms!=500 for s in (e.before.client_host,e.before.db_host)):
                    raise ValueError('background sample TTL differs from protocol')
                if not all(s.is_usable(now=e.record.started_at) for s in (e.before.client_host,e.before.db_host)):
                    raise ValueError('background snapshot stale at actual execution')
            if pending!=t.assignment or e.execution_id in seen:raise ValueError('missing assignment or duplicate execution')
            pending=None;seen.add(e.execution_id)
            if e.query_hash!=sql_sha256(protocol[t.phase+'_sql']) or e.context.dataset_epoch!=protocol['epoch']:raise ValueError('wrong workload or epoch')
            arms=[s+'@'+registry.assert_selectable(s).spec.version for s in POLICIES[:2]]
            check=OfflineLinUCB(arms,baseline_arm=arms[0],partition=partition_key(e),training_until=e.record.finished_at)
            check.update_experience(e,registry)
            if t.phase=='train':
                if models is not None:raise ValueError('training after freeze')
                training.append((t.cache,e))
                if objective:completed_training[e.execution_id]=(t.completed_request,t.workflow)
            else:
                if e.record.started_at<=max(x.record.finished_at for _,x in training):raise ValueError('train/test time overlap')
                if objective and t.completed_request.started_at<=max(r.finished_at for r,w in completed_training.values()):raise ValueError('completed train/test time overlap')
                if t.policy=='ADAPTIVE':
                    selected=models[t.cache].choose_registered(encode(e.features,e.before,at=e.record.started_at),registry).split('@')[0]
                    if e.strategy_id!=selected:raise ValueError('actual strategy differs from frozen policy')
                tests.append(t)
        elif kind=='training_gate':
            if active is not None or recorded_gate is not None or train_round_index!=len(planned_train):raise ValueError('premature or duplicate training gate')
            counts=Counter((e.context.load_bucket,c,e.strategy_id) for c,e in training)
            training_gate=all(counts[(l,c,s)]>=30 for l,c in cells for s in POLICIES[:2])
            if training_gate!=b['passed'] or {'/'.join(k):v for k,v in counts.items()}!=b['counts']:raise ValueError('training gate mismatch')
            recorded_gate=b
        elif kind=='frozen_models':
            if active is not None:raise ValueError('freeze during active round')
            if not training_gate or models is not None:raise ValueError('freeze without qualified training')
            models=train(training,registry,completed=completed_training if objective else None);frozen=fingerprint(models)
            if frozen!=b['fingerprint']:raise ValueError('model not reproducible from training')
            for key,m in models.items():
                expected=m.snapshot().model_dump(mode='json',exclude={'created_at','model_version'})
                actual={k:v for k,v in b['models'][key].items() if k not in ('created_at','model_version')}
                if expected!=actual:raise ValueError('stored parameters differ')
        elif kind=='training_memory_released':
            if (not release or memory_release_seen or models is None or active is not None or 'test' in first_visits
                or b['training_rows']!=len(training) or b['completed_workflows']!=len(completed_training)
                or b['model_fingerprint']!=frozen or b['gc_collected']<0 or not math.isfinite(b['elapsed_ms']) or b['elapsed_ms']<0):
                raise ValueError('invalid training memory release')
            memory_release_seen=True
        elif kind=='complete':
            if pending or active:raise ValueError('unfinished assignment or round')
            terminal=b
        elif kind=='storage_configuration':
            if storage_seen:raise ValueError('duplicate storage configuration')
            storage_seen=True
            if protocol.get('storage_mode')=='WAL_FULL_REQUEST_SESSION' and (b['journal_mode']!='wal' or b['synchronous']!=2):
                raise ValueError('WAL durability configuration mismatch')
        elif kind=='monitor_ready':
            from datetime import datetime
            from tsdb_optimizer.schemas.monitoring import SystemSample
            sample=SystemSample.model_validate(b['sample']);at=datetime.fromisoformat(b['observed_at'])
            if active is not None or pending or not 0<=b['wait_ms']<=2100:
                raise ValueError('invalid monitor preparation')
            if not all(s.is_usable(now=at) and s.is_fresh(now=at,max_age_ms=250) for s in (sample.client_host,sample.db_host)):
                raise ValueError('monitor preparation was not fresh')
        elif kind=='first_occurrence':
            if b['phase'] not in ('train','test') or b['phase'] in first_visits:raise ValueError('duplicate or unknown first occurrence')
            first_visits.add(b['phase'])
            w=WorkflowResult.model_validate(b['workflow'])
            if (active is not None or pending or w.run is None or w.trace.warnings
                or w.run.execution.attempts[-1].execution.strategy_id!='S0_NATIVE'):
                raise ValueError('invalid first occurrence preparation')
            if b['phase']=='test' and models is None:raise ValueError('test preparation before freeze')
            if b['phase']=='train' and training:raise ValueError('training preparation too late')
            if w.run.decision.query_occurrence!=1:raise ValueError('first visit is not first occurrence')
        elif kind in ('reference','cache_preparation'):
            if pending:raise ValueError('preparation after assignment')
        else:raise ValueError('unknown study event')
    if terminal is None:raise ValueError('incomplete study')
    if release and models is not None and not memory_release_seen:raise ValueError('missing training memory release')
    if terminal['qualification']=='MEASURED_NOT_YET_ACCEPTED':
        if protocol['protocol'] in ('controlled-e2e-v5','controlled-e2e-v6','controlled-e2e-v7','controlled-e2e-v8','controlled-e2e-v9') and (not storage_seen or protocol.get('storage_mode')!='WAL_FULL_REQUEST_SESSION'):
            raise ValueError('missing WAL configuration proof')
        if (train_round_index!=len(planned_train) or test_round_index!=len(planned_test)
            or len(tests)!=3*len(planned_test) or not pilot_gate or models is None):
            raise ValueError('incomplete planned study')
    elif terminal['qualification'] not in ('PILOT_ONLY','INSUFFICIENT_TRAINING'):
        raise ValueError('unknown terminal qualification')
    blocks=defaultdict(dict)
    for t in tests:
        key=(t.target_load,t.cache,t.assignment['block'])
        if t.policy in blocks[key]:raise ValueError('duplicate test policy in block')
        blocks[key][t.policy]=t
    if any(set(v)!=set(POLICIES) for v in blocks.values()):raise ValueError('incomplete paired block')
    if models is not None and fingerprint(models)!=frozen:raise ValueError('model changed during analysis')
    counts=Counter((t.experience.context.load_bucket,t.cache,t.policy) for t in tests)
    test_gate=bool(tests) and all(counts[(l,c,p)]>=30 for l,c in cells for p in POLICIES)
    stability={}
    all_trials=[x['body'] for x in events if x['kind']=='trial']
    for load in loads:
        cpu=[x['experience']['before']['db_host']['cpu_percent'] for x in all_trials if x['target_load']==load]
        fraction=sum(v<50 if load=='LOW' else 50<=v<=70 for v in cpu)/len(cpu) if cpu else None
        stability[load]={'n':len(cpu),'target_fraction':fraction,'passed':fraction is not None and fraction>=.8}
    report={'protocol':protocol,'journal_sha256':hashlib.sha256(path.read_bytes()).hexdigest(),
        'pilot_passed':len(pilots)==2*len(loads) and all(p['passed'] for p in pilots),'pilot':pilots,
        'training_samples':len(training),'training_gate':recorded_gate,'test_samples':len(tests),
        'test_sample_gate':test_gate,'test_actual_load_counts':{'/'.join(k):v for k,v in counts.items()},'execution_load_stability':stability,'model_reproduced_and_frozen':models is not None,
        'actual_policy_counts':dict(Counter(t.experience.strategy_id for t in tests if t.policy=='ADAPTIVE')),
        'adaptive_choices_by_cell':{'/'.join(k):v for k,v in Counter((t.experience.context.load_bucket,t.cache,t.experience.strategy_id) for t in tests if t.policy=='ADAPTIVE').items()},
        'storage_configuration':next((x['body'] for x in events if x['kind']=='storage_configuration'),None),
        'snapshot_age_ms_at_dispatch':{},'policy_metrics':{},'trace_interval_means_ms':{},'cell_policy_metrics':{},'contrasts':{},'online_ready':False,'stage_accepted':False,
        'limitations':[('Natural host activity only; no artificial CPU load' if ambient else 'CPU is authorized co-tenant load, not IoTDB query load'),
            'fixed cache preparation and reference executions excluded from request E2E but separately recorded',
            'EMPTY/READY describe client result cache only; IoTDB internal cache state is uncontrolled',
            'single query template, distinct train/test SQL; no generalization to all workloads',
            'single session; independent-session reproduction not established',
            ('reward is finalized whole-request latency; offline fitting and external audit excluded; L1 adapter diagnostics retained' if objective else 'learning reward is adapter client latency; outcome here is whole Orchestrator latency'),
            'assignment precedes execution in flushed journal; no separate assignment UTC field']}
    for source in ('client_host','db_host'):
        ages=sorted((e.record.started_at-getattr(e.before,source).captured_at).total_seconds()*1000 for e in [*[e for _,e in training],*[t.experience for t in tests]])
        if ages:report['snapshot_age_ms_at_dispatch'][source]={'n':len(ages),'min_ms':min(ages),'max_ms':max(ages),'p95_ms':ages[math.ceil(.95*len(ages))-1]}
    for policy in POLICIES:
        values=sorted(t.e2e_ms for t in tests if t.policy==policy)
        if values:report['policy_metrics'][policy]={'n':len(values),'mean_e2e_ms':statistics.mean(values),'median_e2e_ms':statistics.median(values),'p95_e2e_ms':values[math.ceil(.95*len(values))-1]}
    for policy in POLICIES:
        intervals=defaultdict(list)
        for t in tests:
            if t.policy!=policy:continue
            intervals['adapter_client_wall_ms'].append(t.experience.record.client_wall_ms)
            previous=0
            for event in t.workflow.trace.events:
                intervals['to_'+event.state.value].append(event.elapsed_ms-previous);previous=event.elapsed_ms
        report['trace_interval_means_ms'][policy]={k:statistics.mean(v) for k,v in intervals.items()}
    for load,cache in cells:
        for policy in POLICIES:
            values=sorted(t.e2e_ms for t in tests if (t.target_load,t.cache,t.policy)==(load,cache,policy))
            if values:report['cell_policy_metrics']['/'.join((load,cache,policy))]={'n':len(values),'mean_e2e_ms':statistics.mean(values),'median_e2e_ms':statistics.median(values),'p95_e2e_ms':values[math.ceil(.95*len(values))-1]}
    if test_gate:
        report['contrasts']={p:contrast(blocks,p) for p in POLICIES[:2]}
    report['within_session_benefit_gate']=bool(report['contrasts']) and all(c.get('within_session_benefit',False) for c in report['contrasts'].values())
    report['qualification_complete']=bool(pilot_gate) and training_gate and test_gate and all(v['passed'] for v in stability.values())
    if objective:
        from tsdb_optimizer.experiments.scenarios import scenario_report
        report['strategy_scenarios']=scenario_report(blocks,protocol['scenario_analysis'])
        report['reward_objective']={'version':E2E_REWARD_VERSION,'formula':'-end_to_end_ms/1000',
            'scope':'completed Orchestrator.run, including synchronous L1 feedback and store closure',
            'production_online_enabled':False}
    if ambient:
        report['ambient_scope_complete']=report['qualification_complete']
        report['qualification_complete']=False
        report['within_session_benefit_gate']=False
        report['limitations'].append('Ambient-only applicability study cannot satisfy multi-load qualification.')
    return report


if __name__=='__main__':
    p=argparse.ArgumentParser();p.add_argument('journal',type=Path);p.add_argument('--output',type=Path,required=True)
    a=p.parse_args();r=analyze(a.journal);a.output.write_text(json.dumps(r,indent=2,allow_nan=False)+'\n')
    print(json.dumps({k:r[k] for k in ['training_samples','test_samples','test_sample_gate','qualification_complete','within_session_benefit_gate','policy_metrics','contrasts']}))
