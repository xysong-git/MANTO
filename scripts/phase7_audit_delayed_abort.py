"""Audit preserved failed studies. Never emits benefit intervals or acceptance."""
import argparse
from collections import Counter
import hashlib
import json
from pathlib import Path
import sqlite3

from tsdb_optimizer.bandit.episodes import BatchFeedback, BatchPolicy
from tsdb_optimizer.bandit.model import AdmissionError
from tsdb_optimizer.schemas.learning import digest
from tsdb_optimizer.strategies import build_default_registry


def sha(path):
    result=hashlib.sha256()
    with path.open('rb') as stream:
        for chunk in iter(lambda:stream.read(1024*1024),b''):result.update(chunk)
    return result.hexdigest()


def audit(directory):
    counts=Counter();quality=Counter();terminal=None;rejections=[];receipts={}
    enqueue_ns=[];update_ns=[]
    for line in (directory/'journal.jsonl').open():
        event=json.loads(line);k,b=event['kind'],event['body'];counts[k]+=1;terminal=k
        if k=='protocol':protocol=b
        if k=='resource_rejected':rejections.append(b)
        if k=='feedback_applied':
            receipts[b['batch_id']]=b;update_ns.append(b['elapsed_ns'])
        if k=='feedback_enqueued':enqueue_ns.append(b['elapsed_ns'])
        if k=='request':
            quality.update(e['sample_status'] for e in b['experiences'])
    if terminal!='aborted' or counts['complete'] or counts['frozen_model']:
        raise ValueError('expected aborted pre-freeze study')
    integrity={}
    for name in ('executions.sqlite','feedback.sqlite'):
        with sqlite3.connect(f'file:{directory/name}?mode=ro',uri=True) as c:
            integrity[name]=c.execute('PRAGMA integrity_check').fetchone()[0]
    if any(v!='ok' for v in integrity.values()):raise ValueError('corrupt evidence DB')
    with sqlite3.connect(f'file:{directory/"feedback.sqlite"}?mode=ro',uri=True) as c:
        revision,frozen=c.execute('SELECT revision,frozen_at FROM head').fetchone()
        checksum,body=c.execute('SELECT checksum,body FROM versions WHERE revision=0').fetchone()
        body=json.loads(body)
        if digest(body)!=checksum:raise ValueError('initial checksum differs')
        model=BatchPolicy.restore(body)
        registry=build_default_registry(cache_epoch=protocol['epoch'])
        statuses=Counter();rejection_reasons=Counter();applied=0
        for batch_id,checksum,raw,status,reason in c.execute('SELECT batch_id,checksum,body,status,reason FROM feedback ORDER BY position'):
            body=json.loads(raw)
            if digest(body)!=checksum:raise ValueError('feedback checksum differs')
            f=BatchFeedback.model_validate(body);statuses[status]+=1
            if status=='PENDING':continue
            receipt=receipts[batch_id]
            if receipt['status']!=status:raise ValueError('journal/queue status differs')
            try:changed=model.update(f,registry)
            except AdmissionError as exc:
                if status!='REJECTED' or str(exc)!=reason:raise ValueError('rejection differs')
                rejection_reasons[reason]+=1
            else:
                if not changed or status!='APPLIED':raise ValueError('update differs')
                applied+=1
                if receipt['fingerprint']!=model.fingerprint or receipt['revision']!=applied:
                    raise ValueError('journal model differs')
        checksum,body=c.execute('SELECT checksum,body FROM versions WHERE revision=?',(revision,)).fetchone()
        body=json.loads(body)
        if (digest(body)!=checksum or BatchPolicy.restore(body).fingerprint!=model.fingerprint
                or applied!=revision or frozen is not None):raise ValueError('persisted head differs')
    return {'protocol':protocol['protocol'],'journal_sha256':sha(directory/'journal.jsonl'),
        'events':dict(counts),'recorded_request_quality':dict(quality),
        'queue_status':dict(statuses),'replayed_model_updates':applied,'model_reproduced':True,
        'queue_rejection_reasons':dict(rejection_reasons),
        'feedback_cost':{'enqueue_total_ms':sum(enqueue_ns)/1e6,'update_total_ms':sum(update_ns)/1e6,
            'enqueue_mean_ms':sum(enqueue_ns)/len(enqueue_ns)/1e6,
            'update_mean_ms':sum(update_ns)/len(update_ns)/1e6},
        'fingerprint':model.fingerprint,'sqlite_integrity':integrity,'resource_rejections':rejections,
        'heldout_batches':0,'study_complete':False,'benefit_verdict':'UNKNOWN',
        'stage_accepted':False,'online_ready':False}


if __name__=='__main__':
    p=argparse.ArgumentParser();p.add_argument('directories',nargs='+',type=Path);p.add_argument('--output',required=True,type=Path)
    args=p.parse_args();result={str(d):audit(d) for d in args.directories}
    with args.output.open('x') as stream:stream.write(json.dumps(result,indent=2)+'\n')
    print(json.dumps({k:{f:v for f,v in r.items() if f not in ('resource_rejections','fingerprint')} for k,r in result.items()},indent=2))
