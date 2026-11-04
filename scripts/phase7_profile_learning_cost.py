"""Read-only replay of persisted experience; timings are not query benefits."""
import argparse
import json
import statistics
import time
from pathlib import Path
from tsdb_optimizer.learning import repository as module
from tsdb_optimizer.storage import SQLiteStore


def run(path,output):
    store=SQLiteStore(path);repo=module.ExperienceRepository(store)
    with store._connect() as c:
        groups=[r[0] for r in c.execute('SELECT group_key FROM experiences GROUP BY group_key ORDER BY count(*) DESC LIMIT 4')]
    if len(groups)!=4:raise ValueError('four real groups required')
    result={'source':str(path),'scope':'read-only L1 build_model replay, not query or policy performance','groups':[]}
    for group in groups:
        module._cached_fact.cache_clear()
        saved=module._statistics_fact;measurements={'full_validation':[],'content_cache':[]};reference=None
        try:
            for i in range(12):
                for mode in (('full_validation','content_cache') if i%2==0 else ('content_cache','full_validation')):
                    module._statistics_fact=module._read_fact if mode=='full_validation' else saved
                    start=time.perf_counter();model=repo.build_model(group);elapsed=(time.perf_counter()-start)*1000
                    comparable=model.model_dump(exclude={'model_version','created_at'})
                    if reference is None:reference=comparable
                    if reference!=comparable:raise ValueError('statistics changed between implementations')
                    measurements[mode].append(elapsed)
        finally:module._statistics_fact=saved
        result['groups'].append({'group':group,'samples':len(model.sample_execution_ids),'identical_statistics':True,
            'raw_ms':measurements,'median_ms':{k:statistics.median(v) for k,v in measurements.items()}})
    output.write_text(json.dumps(result,indent=2)+'\n');print(json.dumps([g['median_ms'] for g in result['groups']]))

if __name__=='__main__':
    p=argparse.ArgumentParser();p.add_argument('database',type=Path);p.add_argument('--output',type=Path,required=True)
    a=p.parse_args();run(a.database,a.output)
