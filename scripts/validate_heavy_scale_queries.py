#!/usr/bin/env python
"""Independent workload inventory checks; optional non-executing server EXPLAIN probes."""
import argparse
import hashlib
import json
from pathlib import Path
import re


def validate(root):
    manifest=json.loads((root/'manifest.json').read_text())
    rows=[]
    all_sql=set()
    for entry in manifest['files']:
        path=root/entry['path']
        content=path.read_bytes()
        if hashlib.sha256(content).hexdigest()!=entry['sha256']:
            raise ValueError('file digest changed')
        statements=content.decode().splitlines()
        canonical={' '.join(s.lower().split()) for s in statements}
        if len(statements)!=1000 or len(canonical)!=1000:
            raise ValueError('requires 1000 unique statements per file')
        meta=manifest['datasets'][path.parent.name]
        prefix=meta['database_prefix']
        expected_devices=max(2,(meta['device_count']+9)//10)
        grouped='group_by_time' in path.stem
        for sql in statements:
            if not sql.startswith('SELECT ') or not sql.endswith(';') or sql.count(';')!=1:
                raise ValueError('not a single-line SELECT')
            if sql in all_sql: raise ValueError('duplicate across files')
            all_sql.add(sql)
            from_clause=sql.split(' FROM ',1)[1].split(' WHERE ',1)[0]
            paths=from_clause.split(', ')
            if len(paths)!=expected_devices or len(set(paths))!=len(paths):
                raise ValueError('device fanout mismatch')
            for item in paths:
                if not item.startswith(prefix+'.') or item[len(prefix)+1:] not in meta['devices']:
                    raise ValueError('unknown device')
            # These patterns inspect generated artifact fields, not the SQL security boundary.
            sensors=re.findall(r'\bs_\d+\b',sql)
            if any(meta['sensor_type_codes'].get(s) not in ('1','2','3','4') for s in sensors):
                raise ValueError('non-numeric or missing sensor')
            selected=sql.split(' FROM ',1)[0]
            if len(re.findall(r'\bs_\d+\b',selected))!=(8 if grouped else 16):
                raise ValueError('sensor fanout mismatch')
            match=re.search(r'WHERE time >= (\d+) AND time < (\d+)',sql)
            if not match: raise ValueError('missing bounded range')
            lo,hi=map(int,match.groups())
            if not (meta['min_time']<=lo<hi<=meta['max_time']+meta['step_ms']):
                raise ValueError('time range outside actual CSV bounds')
            if (hi-lo)//meta['step_ms']<1800:
                raise ValueError('query does not cover the intended heavy time window')
            if grouped:
                if not re.search(rf'GROUP BY \(\[{lo}, {hi}\), (20000|30000|60000)ms\)',sql):
                    raise ValueError('group window differs from WHERE range')
        rows.append({'path':entry['path'],'unique_statements':len(canonical),'integrity':'PASS'})
    if len(rows)!=24 or len(all_sql)!=24000:
        raise ValueError('expected 24 files / 24000 unique queries')
    return {'files':rows,'total_unique_queries':len(all_sql),'offline_integrity':'PASS'}


def probe(root):
    from tsdb_optimizer.config import load_settings
    from tsdb_optimizer.adapters.iotdb import IoTDBTreeAdapter
    adapter=IoTDBTreeAdapter(load_settings())
    evidence=[]
    try:
        doctor=adapter.doctor()
        if not doctor.ok or doctor.server.version!='2.0.6' or doctor.server.model!='tree':
            raise RuntimeError('verified 2.0.6 tree server required')
        for path in sorted(root.glob('*/*.sql')):
            sql=path.read_text().splitlines()[0]
            result=adapter.execute_explain(sql,preview_rows=5)
            evidence.append({'file':str(path.relative_to(root)),'line':1,
                'sql_sha256':hashlib.sha256(sql.encode()).hexdigest(),
                'status':result.status.value,'rows_returned':result.row_count,
                'client_wall_ms':result.metrics.client_wall_ms,
                'error_type':result.error_type,'error_message':result.error_message,
                'operation':'EXPLAIN_ONLY','target_query_executed':False})
            print(json.dumps(evidence[-1],ensure_ascii=False),flush=True)
    finally:
        adapter.close()
    return {'server_version':doctor.server.version,'client_version':doctor.server.client_version,
        'model':doctor.server.model,'queries':evidence,
        'all_passed':all(e['status']=='SUCCESS' for e in evidence),
        'scope':'first statement per file only; no EXPLAIN ANALYZE or full heavy-query execution'}


if __name__=='__main__':
    p=argparse.ArgumentParser()
    p.add_argument('--root',type=Path,default=Path('query/heavy_scale'))
    p.add_argument('--probe-explain',action='store_true')
    args=p.parse_args()
    result=validate(args.root)
    if args.probe_explain: result['server_explain']=probe(args.root)
    (args.root/'validation.json').write_text(json.dumps(result,ensure_ascii=False,indent=2)+'\n')
    print(json.dumps({'offline_integrity':'PASS','total_queries':result['total_unique_queries'],
        'explain_passed':result.get('server_explain',{}).get('all_passed')},ensure_ascii=False))
    if args.probe_explain and not result['server_explain']['all_passed']:
        raise SystemExit(1)
