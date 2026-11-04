"""Read-only decomposition of existing phase8 records. No DB connection."""
import csv,json,hashlib,statistics
from pathlib import Path
from collections import defaultdict
from tsdb_optimizer.benchmark.journal import read_events

root=Path('benchmarks/results/phase8_small_count_20261104_101201_154809532')
out=Path(__file__).parent
journal=root/'journal.jsonl'
before=hashlib.sha256(journal.read_bytes()).hexdigest()
rows=[]
for event in read_events(journal):
 if event['kind']!='request':continue
 r=event['body'];a=r['allocation']
 if r['role']!='MEASURED' or a['phase']!='HOLDOUT':continue
 ex=r['run']['execution'];d=r['run']['decision']
 row={'cache':a['cache_condition'],'group':a['group'],'block':a['block'],'index':r['index'],
  'allocation_id':a['allocation_id'],'query_id':d['query_id'],'e2e_ms':r['elapsed_ns']/1e6,
  'decision_ms':d['decision_overhead_ms'],'executor_ms':ex['total_wall_ms'],
  'adapter_ms':sum(x['execution']['metrics']['client_wall_ms'] for x in ex['attempts'])}
 row['executor_other_ms']=row['executor_ms']-row['adapter_ms']
 row['remaining_ms']=row['e2e_ms']-row['decision_ms']-row['executor_ms']
 row['report_cutoff_ms']=ex['end_to_end_ms']
 # Cutoff differs across paths; do not treat as a comparable component.
 wf=r['workflow']
 if wf:
  t=wf['request_timing']
  row.update({'store_open_ms':t['store_open_ms'],'store_close_ms':t['store_close_ms'],
   'workflow_body_ms':t['body_ms'],'outer_ms':row['e2e_ms']-t['total_ms']})
  last=0
  for ev in wf['trace']['events']:
   row['stage_'+ev['state']+'_ms']=ev['elapsed_ms']-last;last=ev['elapsed_ms']
   for k in ('parsing_ms','query_persistence_ms'):
    if k in ev['details']:row[k]=ev['details'][k]
  row['post_trace_body_ms']=t['body_ms']-last
  assert abs(t['total_ms']-t['body_ms']-t['store_open_ms']-t['store_close_ms'])<1e-6
 assert abs(row['e2e_ms']-sum(row[k] for k in ('decision_ms','adapter_ms','executor_other_ms','remaining_ms')))<1e-6
 rows.append(row)
assert len(rows)==3360
fields=sorted(set().union(*(r.keys() for r in rows)))
with (out/'requests.csv').open('w') as f:
 w=csv.DictWriter(f,fieldnames=fields);w.writeheader();w.writerows(rows)
cells=defaultdict(list)
for r in rows:cells[(r['cache'],r['group'])].append(r)
summary=[]
for (cache,group),rs in sorted(cells.items()):
 s={'cache':cache,'group':group,'n':len(rs),'blocks':len(set(r['block'] for r in rs))}
 for k in fields:
  if k.endswith('_ms'):
   values=[r[k] for r in rs if k in r];s[k]=statistics.mean(values) if values else None
 summary.append(s)
assert hashlib.sha256(journal.read_bytes()).hexdigest()==before
original=json.loads((root/'analysis/report.json').read_text())
for s in summary:
 g=next(g for g in original['groups'] if g['cache_condition']==s['cache'] and g['group']==s['group'])
 assert s['n']==g['requests']==240 and s['blocks']==30
 for key,metric in [('e2e_ms','end_to_end_ms'),('decision_ms','decision_overhead_ms'),('adapter_ms','client_wall_ms')]:
  assert abs(s[key]-g['metrics'][metric]['mean'])<1e-9
result={'journal_sha256':before,'scope':'REAL_IOTDB_EXISTING_HOLDOUT_ONLY','rows':len(rows),'groups':summary,
 'definitions':{'remaining_ms':'E2E minus decision minus executor; mixed uninstrumented work, NOT pure persistence',
 'stage_fields':'differences of cumulative trace timestamps; include event overhead, not pure component timers',
 'report_cutoff_ms':'path-dependent cutoff; not comparable between service and orchestrator',
 'server_execution_ms':'UNKNOWN'}}
(out/'summary.json').write_text(json.dumps(result,indent=2)+'\n')
for s in summary:print(s['cache'],s['group'],{k:round(s[k],3) for k in ('e2e_ms','decision_ms','adapter_ms','executor_other_ms','remaining_ms')})
