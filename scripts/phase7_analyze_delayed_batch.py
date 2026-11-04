"""Streaming independent replay of closed batches, delayed updates and holdout."""
import argparse
from collections import Counter
from datetime import datetime
import hashlib
import json
from pathlib import Path
import statistics

from tsdb_optimizer.agents import QueryAnalysisAgent
from tsdb_optimizer.bandit.episodes import BatchFeedback, BatchPolicy, batch_family
from tsdb_optimizer.bandit.model import AdmissionError
from tsdb_optimizer.bandit.workload_audit import audit_source_reference, verify_import_certificate
from tsdb_optimizer.experiments.batches import plan, summarize, BatchSummary, LOADS, SIZES, POLICIES, SelectionTiming
from tsdb_optimizer.experiments.batch_statistics import BatchStatistics
from tsdb_optimizer.experiments.sequences import SequenceRequest
from tsdb_optimizer.experiments.exclusive_instance import ClientOccupancy
from tsdb_optimizer.schemas.execution import ExecutionResult
from tsdb_optimizer.schemas.learning import digest
from tsdb_optimizer.schemas.monitoring import SystemSnapshot
from tsdb_optimizer.strategies import build_default_registry
from tsdb_optimizer.sql_guard import validate_readonly_sql

ROOT=Path(__file__).resolve().parents[1]


def validate_protocol(p):
    if p.get('protocol') not in ('delayed-batch-v2','delayed-batch-v3','delayed-batch-v4','delayed-batch-v5','delayed-batch-v6','delayed-batch-v7'):
        raise ValueError('unknown delayed batch protocol')
    required={'rounds':45 if p['protocol'] in ('delayed-batch-v4','delayed-batch-v5','delayed-batch-v6','delayed-batch-v7') else 30,'sizes':[2,8],'loads':['LOW','MEDIUM'],
        'query_lines':{'TRAIN':10,'HOLDOUT':11},'train_seed':20261107,'test_seed':20261108,
        'analysis_seed':20261109,'minimum_per_arm':30,'bootstrap_replicates':4000,'confidence':.99,
        'cpu_target':55,'cpu_stop':80,'memory_stop':90,'disk_stop':90,'cpu_duration_s':30,
        'query_timeout_ms':30000,'round_limit_s':60,'max_query_connections':1,'snapshot_ttl_ms':500,
        'snapshot_max_age_ms':250,'snapshot_wait_s':.5,'statistics_mode':'record_only',
        'snapshot_return_max_age_ms':200,'monitor_interval_s':.1,
        'reward_version':'negative-batch-service-seconds-v1','load_protocol':'cpu-activity-v3',
        'process_supervisor':True,'production_online_enabled':False}
    if p['protocol'] in ('delayed-batch-v3','delayed-batch-v4','delayed-batch-v5','delayed-batch-v6','delayed-batch-v7'):
        required.update(cpu_profile='staggered-v1',cpu_worker_fraction_cap=.60,cpu_pulse_period_s=.02,
                        foreign_client_guard='linux-tcp-boundary-v1')
    if p['protocol'] in ('delayed-batch-v5','delayed-batch-v6','delayed-batch-v7'):
        required.update(cpu_profile='staggered-spawn-v2',cpu_start_method='spawn')
    if p['protocol'] in ('delayed-batch-v6','delayed-batch-v7'):
        required.update(query_lines={'TRAIN':12,'HOLDOUT':13},request_timing='store-session-v1',
                        parser_memo='request-analysis-features-v1')
    if p['protocol']=='delayed-batch-v7':
        required.update(query_lines={'TRAIN':14,'HOLDOUT':15},l1_comparator='batch-mean-v1',
                        selection_timing='selection-segments-v1')
    if any(p.get(k)!=v for k,v in required.items()):raise ValueError('changed delayed batch protocol')
    path=ROOT/'query/heavy_scale/small_scale/q03_count_aggregation.sql'
    if hashlib.sha256(path.read_bytes()).hexdigest()!=p['query_file_sha256']:raise ValueError('workload file changed')
    lines=path.read_text().splitlines()
    if p['queries']!={phase:validate_readonly_sql(lines[i-1]) for phase,i in p['query_lines'].items()}:
        raise ValueError('unbound training/test SQL')
    if p['queries']['TRAIN']==p['queries']['HOLDOUT']:raise ValueError('train/test overlap')
    manifest=json.loads((ROOT/'query/heavy_scale/manifest.json').read_text())
    if verify_import_certificate(ROOT,manifest['import_verification'],p['server_id'],p['epoch'])!=p['import_certificate']:
        raise ValueError('import certificate differs')
    return manifest


def analyze(path):
    iterator=iter(path.open())
    try:first=json.loads(next(iterator))
    except StopIteration:raise ValueError('empty journal')
    if first['kind']!='protocol':raise ValueError('missing protocol')
    p=first['body'];manifest=validate_protocol(p)
    registry=build_default_registry(cache_epoch=p['epoch'])
    model=BatchPolicy(deadline=datetime.fromisoformat(p['training_deadline']),family_key=p['family_key'],
        server_id=p['server_id'],epoch=p['epoch'])
    include_l1=p['protocol']=='delayed-batch-v7'
    l1=BatchStatistics(scope=model.scope)
    curve=[]
    analysis={phase:QueryAnalysisAgent().analyze(sql).features for phase,sql in p['queries'].items()}
    if any(batch_family(f)!=p['family_key'] for f in analysis.values()):raise ValueError('unverified query family')
    rounds=p['rounds']
    planned=list(plan('TRAIN',rounds,include_l1=include_l1))+list(plan('HOLDOUT',rounds,include_l1=include_l1));round_index=0;active=None;position=0
    pilots=[];references={};initial=False;assignment=None;metadata=None;rows=[];closed=None;pending=None
    seen=set();batch_ids=set();train_counts=Counter();outcomes=[];model_updates=0;frozen=None;gate=False;terminal=False
    previous_finish=None;closed_train=0;closed_test=0;enqueue_ns=[];update_ns=[];rejected=[];last_enqueued=None
    closed_target=None;target_samples=Counter();target_matches=Counter()
    guarded=p['protocol'] in ('delayed-batch-v3','delayed-batch-v4','delayed-batch-v5','delayed-batch-v6','delayed-batch-v7');guard_total=0;round_guards=0;observer=None;last_guard_at=None
    for line in iterator:
        event=json.loads(line);kind,b=event['kind'],event['body']
        if terminal:raise ValueError('event after terminal')
        if kind=='instance_occupancy':
            o=ClientOccupancy.model_validate(b)
            if (not guarded or not o.passed or o.foreign_pids or o.unknown_connections
                    or o.connection_count>p['max_query_connections']
                    or o.server_pid!=p['load_identity']['pid'] or o.port!=p['load_identity']['port']):
                raise ValueError('foreign/unknown client or invalid occupancy guard')
            if observer is not None and observer!=o.observer_pid:raise ValueError('occupancy observer changed')
            if ((last_guard_at is not None and o.captured_at<=last_guard_at)
                    or (previous_finish is not None and o.captured_at<previous_finish)):
                raise ValueError('stale/reordered occupancy observation')
            last_guard_at=o.captured_at
            observer=o.observer_pid
            if active is None:
                if guard_total:raise ValueError('misplaced occupancy guard')
            else:round_guards+=1
            guard_total+=1
        elif kind=='round_start':
            if active is not None or assignment is not None:raise ValueError('overlapping round')
            if guarded and not guard_total:raise ValueError('missing startup occupancy guard')
            round_guards=0
            if b['phase']=='PILOT':
                expected={'phase':'PILOT','repeat':len(pilots)//2,'load':LOADS[len(pilots)%2]} if len(pilots)<4 else None
                if b!=expected or initial:raise ValueError('invalid pilot order')
            else:
                if not initial or b!=planned[round_index]:raise ValueError('randomized round differs')
                if (b['phase']=='HOLDOUT') != (frozen is not None):raise ValueError('train/holdout freeze boundary')
                position=0
            active=b
        elif kind=='pilot':
            if active is None or active['phase']!='PILOT' or (b['repeat'],b['load'])!=(active['repeat'],active['load']):raise ValueError('pilot outside round')
            samples=[SystemSnapshot.model_validate(s) for s in b['samples']]
            if len(samples)!=20 or any(s.source_type.value!='db_host' or s.cpu_measurement!='cpu-times-instance-v1'
                    or not s.valid or s.cpu_percent>80 or s.memory_percent>90 or s.disk_usage_percent>90 for s in samples):raise ValueError('unsafe pilot samples')
            if any(a.captured_at>=z.captured_at for a,z in zip(samples,samples[1:])):raise ValueError('pilot sample order')
            passed=sum(s.cpu_percent<50 if b['load']=='LOW' else 50<=s.cpu_percent<=70 for s in samples)>=16
            if not passed or b['passed']!=passed:raise ValueError('unstable pilot')
            pilots.append(b)
        elif kind=='round_complete':
            if active is None:raise ValueError('completion outside round')
            expected_guards=1 if active['phase']=='PILOT' else 1+2*len(active['batches'])
            if guarded and round_guards!=expected_guards:raise ValueError('missing round/batch occupancy guards')
            if active['phase']=='PILOT':
                if b!=active or len(pilots)!=(active['repeat']*2+LOADS.index(active['load'])+1):raise ValueError('incomplete pilot')
            else:
                if (any(b.get(k)!=v for k,v in active.items()) or assignment is not None
                        or position!=len(active['batches']) or not 0<b['elapsed_s']<60):raise ValueError('incomplete round')
                control=b['cpu_control']
                if (active['load']=='MEDIUM') != bool(control):raise ValueError('CPU load proof missing/unexpected')
                if any(not 0<=s['cpu_percent']<=80 or not 0<=s['memory_percent']<=90 or not 0<s['elapsed_s']<30
                    or not 0<=s['worker_duty']<=1 for s in control):raise ValueError('unsafe CPU controller log')
                round_index+=1
            active=None
        elif kind=='reference':
            phase=b['phase']
            if active or initial or len(pilots)!=4 or phase in references or phase not in analysis:raise ValueError('misplaced reference')
            ref=ExecutionResult.model_validate(b['result'])
            if ref.sql_sha256!=analysis[phase].query_hash or ref.status.value!='SUCCESS' or ref.preview_truncated:raise ValueError('invalid reference')
            audit=audit_source_reference('small_scale',Path(manifest['datasets']['small_scale']['source_directory']),p['queries'][phase],ref.model_dump(mode='json'))
            if audit['status']!='PASS' or b['audit']!=audit:raise ValueError('reference source audit mismatch')
            references[phase]=ref
        elif kind=='initial_model':
            if initial or len(references)!=2 or active:raise ValueError('misplaced initialization')
            if BatchPolicy.restore(b).fingerprint!=model.fingerprint:raise ValueError('model not initialized from scratch')
            initial=True
        elif kind=='feedback_applied':
            if pending is None or assignment or frozen:raise ValueError('update without delayed feedback')
            f,at,target=pending
            if b['batch_id']!=f.contract.batch_id or datetime.fromisoformat(b['at'])<at or b['elapsed_ns']<=0:raise ValueError('invalid delayed update receipt')
            prior_prediction=None
            try:
                if include_l1:
                    f.admit_learning(registry)
                    prior_prediction=model.models[str(f.contract.size)].predict(
                        f.allocated_strategy+'@'+registry.assert_selectable(f.allocated_strategy).spec.version,
                        f.vector,ucb=False)
                changed=model.update(f,registry)
                if not changed:raise ValueError('duplicate update')
            except AdmissionError as exc:
                if b['status']!='REJECTED' or b.get('reason')!=str(exc) or b['revision']!=model_updates:raise ValueError('rejection differs')
                rejected.append({'batch_id':f.contract.batch_id,'reason':str(exc)})
            else:
                model_updates+=1
                if include_l1:
                    if not l1.observe(f,registry):raise ValueError('duplicate L1 replay update')
                    curve.append({'revision':model_updates,'batch_id':f.contract.batch_id,
                        'load':f.load_bucket,'size':f.contract.size,'strategy':f.allocated_strategy,
                        'observed_reward':f.reward,'service_ms':f.service_ms,
                        'pre_update_prediction_reward':prior_prediction})
                if f.load_bucket==target:
                    train_counts[(f.load_bucket,f.contract.size,f.allocated_strategy)]+=1
                if (b['status']!='APPLIED' or b['revision']!=model_updates or b['fingerprint']!=model.fingerprint
                        or b['reward']!=f.reward or b['service_ms']!=f.service_ms):raise ValueError('model/reward not reproducible')
            update_ns.append(b['elapsed_ns']);pending=None
        elif kind=='batch_assignment':
            if active is None or active['phase']=='PILOT' or assignment or pending or closed:raise ValueError('invalid batch assignment lifecycle')
            if guarded and round_guards!=2+2*position:raise ValueError('batch not preceded by occupancy guard')
            item=active['batches'][position]
            expected={k:v for k,v in active.items() if k!='batches'}|item|{'position':position,'batch_id':b['batch_id']}
            if b!=expected or b['batch_id'] in batch_ids:raise ValueError('batch allocation/schedule differs')
            batch_ids.add(b['batch_id']);assignment=b;metadata=None;rows=[]
        elif kind=='batch_selected':
            if assignment is None or metadata:raise ValueError('selection outside batch')
            metadata=dict(b);recorded=metadata.pop('model_fingerprint')
            if include_l1:
                if metadata.pop('l1_fingerprint')!=l1.fingerprint:raise ValueError('L1 selection state differs')
                timing=SelectionTiming.model_validate(metadata.pop('selection_timing'))
                if timing.total_ns!=metadata['selection_ns']:raise ValueError('selection timing total differs')
            if guarded and datetime.fromisoformat(metadata['contract']['declared_at'])<last_guard_at:
                raise ValueError('batch preceded occupancy observation')
            if recorded!=model.fingerprint:raise ValueError('selection used different model')
            if metadata['contract']['batch_id']!=assignment['batch_id']:raise ValueError('selection identity differs')
        elif kind=='request':
            if metadata is None:raise ValueError('request before selection')
            r=SequenceRequest.model_validate(b)
            if p['protocol'] in ('delayed-batch-v6','delayed-batch-v7'):
                if r.workflow.request_timing is None:
                    raise ValueError('v6 request session timing missing')
                parsed=next(e for e in r.workflow.trace.events if e.state.value=='PARSED')
                costs=[parsed.details.get(k) for k in ('parsing_ms','query_persistence_ms')]
                if any(type(v) not in (int,float) or not 0<=v<=parsed.elapsed_ms for v in costs):
                    raise ValueError('v6 parsing/persistence timing invalid')
                if sum(costs)>parsed.elapsed_ms+.001:
                    raise ValueError('v6 parsing intervals exceed trace')
            if (r.block,r.length,r.policy,r.index)!=(assignment['block'],assignment['size'],assignment['policy'],len(rows)):raise ValueError('request order/assignment differs')
            if previous_finish and r.started_at<=previous_finish:raise ValueError('overlapping requests')
            previous_finish=r.finished_at
            ref=references[assignment['phase']]
            actual=r.workflow.run.execution.attempts[-1].execution
            if (actual.status.value!='SUCCESS' or actual.columns!=ref.columns or actual.rows_preview!=ref.rows_preview
                    or actual.row_count!=ref.row_count or actual.preview_truncated):raise ValueError('failed/mismatched result')
            rows.append(r)
        elif kind=='batch_closed':
            if metadata is None or assignment is None:raise ValueError('close without open batch')
            f=BatchFeedback(**metadata,closed_at=b['closed_at'],requests=rows)
            if (f.contract.phase!=assignment['phase'] or f.policy!=assignment['policy']
                    or f.contract.size!=assignment['size'] or f.contract.batch_id!=b['batch_id']
                    or b['service_ms']!=f.service_ms or b['reward']!=f.reward
                    or b['feedback_sha256']!=digest(f.model_dump(mode='json',round_trip=True))):raise ValueError('batch close/credit mismatch')
            expected_features=analysis[f.contract.phase].model_dump(exclude={'query_id'})
            if f.features.model_dump(exclude={'query_id'})!=expected_features:raise ValueError('selection features differ from SQL')
            model._scope(f.contract)
            target=assignment['load']
            for s in [f.selection_sample, *(r.admission_sample for r in f.requests)]:
                key=f.contract.phase+'/'+target
                target_samples[key]+=1
                cpu=s.db_host.cpu_percent
                target_matches[key]+=int(cpu<50 if target=='LOW' else 50<=cpu<=70)
            if set(f.identities)&seen:raise ValueError('reused execution across batches')
            seen.update(f.identities)
            if f.policy=='ADAPTIVE':
                expected=model.choose(f.contract,f.features,f.selection_sample,f.context_at,registry)
                if f.allocated_strategy!=expected:raise ValueError('adaptive action not from frozen model')
            if f.policy=='L1_BATCH':
                if not include_l1:raise ValueError('L1 not declared in protocol')
                expected=l1.choose(f.contract,f.features,f.selection_sample,f.context_at,registry)
                if f.allocated_strategy!=expected:raise ValueError('L1 action not from frozen training statistics')
            if f.contract.phase=='TRAIN':
                closed_train+=1;closed=f;closed_target=target
            else:
                if not frozen or f.contract.declared_at<=datetime.fromisoformat(frozen['frozen_at']):raise ValueError('holdout before freeze')
                closed_test+=1
                outcomes.append((assignment['load'],assignment['block'],BatchSummary.from_feedback(f)))
            position+=1;assignment=None;metadata=None;rows=[]
        elif kind=='feedback_enqueued':
            if closed is None or pending is not None or frozen or b['batch_id']!=closed.contract.batch_id:
                raise ValueError('feedback submitted before close or during holdout')
            at=datetime.fromisoformat(b['at'])
            if at<closed.closed_at or b['elapsed_ns']<=0:raise ValueError('invalid enqueue timing')
            enqueue_ns.append(b['elapsed_ns']);pending=(closed,at,closed_target);closed=None
        elif kind=='training_gate':
            expected=all(train_counts[(l,n,s)]>=30 for l in LOADS for n in SIZES for s in POLICIES[:2])
            if active or pending or closed or closed_train!=8*rounds or round_index!=2*rounds or gate:
                raise ValueError('premature training gate')
            if not expected or b!={'passed':expected,'counts':{'/'.join(map(str,k)):v for k,v in train_counts.items()}}:raise ValueError('training sample gate mismatch')
            gate=True
        elif kind=='frozen_model':
            if not gate or frozen or pending or active or b['revision']!=model_updates:
                raise ValueError('invalid freeze')
            if b['fingerprint']!=model.fingerprint or BatchPolicy.restore(b['snapshot']).fingerprint!=model.fingerprint:
                raise ValueError('frozen parameters differ from delayed replay')
            if datetime.fromisoformat(b['frozen_at'])<previous_finish:raise ValueError('freeze precedes training completion')
            if include_l1:
                snapshot=l1.freeze().model_dump(mode='json')
                if b['l1_snapshot']!=snapshot or b['l1_fingerprint']!=l1.fingerprint:
                    raise ValueError('L1 frozen statistics differ from replay')
                if snapshot['evidence']!=model.evidence:
                    raise ValueError('L1 and LinUCB training evidence differs')
            frozen=b
        elif kind=='complete':
            holdout_per_block=16 if include_l1 else 12
            if guarded and guard_total!=5+(52 if include_l1 else 44)*rounds:raise ValueError('incomplete occupancy coverage')
            if (active or assignment or pending or closed or not frozen or round_index!=4*rounds
                    or closed_train!=8*rounds or closed_test!=holdout_per_block*rounds
                    or b!={'training_batches':8*rounds,'heldout_batches':holdout_per_block*rounds,'online_ready':False,'stage_accepted':False}):
                raise ValueError('incomplete study')
            terminal=True
        else:raise ValueError('aborted or unknown journal event; no filtering')
    iterator.close()
    if not terminal:raise ValueError('missing complete terminal')
    result=summarize(outcomes,train_counts,rounds,include_l1=include_l1)
    stability={k:target_matches[k]/v for k,v in target_samples.items()}
    stable=all(v>=.8 for v in stability.values()) and len(stability)==4
    result['target_stability']=stability
    result['target_stability_gate']=stable
    if not stable:
        result['contrasts']={}
        result['within_session_benefit_gate']=False
        if include_l1:result['l1_contrast']=None
    if include_l1:
        result['l1_fingerprint']=l1.fingerprint
        result['l1_reproduced']=True
        result['l1_training_statistics']=l1.snapshot().model_dump(mode='json')['statistics']
        result['training_observations']=curve
        # Actual training rewards only: never invent an unexecuted arm reward.
        result['training_cumulative_reward']=sum(row['observed_reward'] for row in curve)
        result['counterfactual_regret']='UNKNOWN: unexecuted option rewards are not observed'
    result.update(protocol=p['protocol'],journal_sha256=hashlib.sha256(path.read_bytes()).hexdigest(),
        training_batches=closed_train,heldout_batches=closed_test,delayed_model_updates=model_updates,
        training_counts={'/'.join(map(str,k)):v for k,v in train_counts.items()},rejected_training=rejected,
        frozen_fingerprint=model.fingerprint,model_reproduced=True,pilot_passed=True,
        feedback_cost={'enqueue_total_ms':sum(enqueue_ns)/1e6,'update_total_ms':sum(update_ns)/1e6,
            'enqueue_mean_ms':statistics.mean(enqueue_ns)/1e6,'update_mean_ms':statistics.mean(update_ns)/1e6},
        limitations=['Declared finite same-SQL batches only; no unknown-future request forecast.',
            'Independent SQL/time holdout in one study, not independent-session replication.',
            'Co-tenant whole-host CPU is not IoTDB query/process CPU.',
            'Delayed queue/update costs reported separately; frozen holdout is inference-only.',
            'Pointwise 99% exploratory intervals; serial dependence and multiple comparisons remain.'])
    return result


if __name__=='__main__':
    parser=argparse.ArgumentParser();parser.add_argument('journal',type=Path);parser.add_argument('--output',type=Path,required=True)
    args=parser.parse_args();report=analyze(args.journal)
    with args.output.open('x') as stream:stream.write(json.dumps(report,indent=2)+'\n')
    print(json.dumps(report,indent=2))
