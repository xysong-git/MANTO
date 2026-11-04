"""Publish a repair certificate only after independent post-write verification."""
from datetime import datetime
import hashlib
import importlib.metadata
import json
from pathlib import Path

ROOT=Path(__file__).resolve().parents[1]
WORK=ROOT/'data/heavy_import_repair_20261102'


def lines(path): return [json.loads(line) for line in path.read_text().splitlines()]


def summarize():
    plan=json.loads((WORK/'plan.json').read_text())
    writes=[r for r in lines(WORK/'import.jsonl') if r['kind']=='complete']
    wanted={(s['scale'],b) for s in plan['scales'] for b in (0,1)}
    if len(writes)!=6 or {(r['scale'],r['batch']) for r in writes}!=wanted or any(r['status']!='PASS' for r in writes):
        raise ValueError('incomplete or failed import journal')
    if sum(int(r['result_matrix'][0][1]) for r in writes)!=6400000000:
        raise ValueError('unexpected acknowledged write count')
    finished=max(datetime.fromisoformat(r['finished_at']) for r in writes)
    before=lines(WORK/'verify_missing.jsonl');after=lines(WORK/'verify_complete.jsonl')
    devices={(s['scale'],d) for s in plan['scales'] for d in s['devices']}
    for rows,expected in [(before,0),(after,3000)]:
        if len(rows)!=1600 or {(r['scale'],r['device']) for r in rows}!=devices:
            raise ValueError('incomplete device audit')
        if any(r['status']!='PASS' or r['series_checked']!=2000 or r['counts_histogram']!={str(expected):2000} for r in rows):
            raise ValueError('series counts differ from source')
    if any(datetime.fromisoformat(r['started_at'])<finished for r in after):
        raise ValueError('completion audit predates last write')
    value_path=ROOT/'docs/evidence/heavy_import_values_20261102.json'
    values=json.loads(value_path.read_text())
    if len(values)!=9 or any(r['status']!='PASS' or r['rows']!=1000 or datetime.fromisoformat(r['started_at'])<finished for r in values):
        raise ValueError('missing independent six-type value checks')
    live_path=ROOT/'docs/evidence/heavy_import_live_after_20261102.json'
    live=json.loads(live_path.read_text())
    if len(live)!=3 or any(r['status']!='PASS' or r['source_nonnull_count']!=1931 or r['database_count']!=1931
        or datetime.fromisoformat(r['reference']['started_at'])<finished for r in live):
        raise ValueError('fresh original-workload source check required')
    per_scale={}
    for scale in plan['scales']:
        rows=[r for r in after if r['scale']==scale['scale']]
        per_scale[scale['scale']]={'devices':len(rows),'series_checked':sum(r['series_checked'] for r in rows),
            'points_per_series':3000,'failed_devices':0,'verified_points':len(rows)*2000*3000}
    files=[WORK/'plan.json',WORK/'source_inventory.jsonl',WORK/'verify_missing.jsonl',WORK/'verify_complete.jsonl',
           WORK/'import.jsonl',value_path,live_path]
    return {'status':'PASS','endpoint':plan['endpoint'],'data_epoch':plan['data_epoch'],
        'server_version':plan['server_version'],'python_read_client_version':importlib.metadata.version('apache-iotdb'),
        'java_import_client_version':'2.0.5-250801-20250801.020248-1 (existing bundled importer)',
        'per_scale':per_scale,'inserted_points':6400000000,'total_verified_points':9600000000,
        'value_check_status':'PASS','sampled_value_rows':9000,'sampled_sensor_values':54000,
        'value_check_scope':'six sensor types on one device per scale across all three batches; not all values',
        'finished_at':max(r['finished_at'] for r in after),
        'evidence_sha256':{str(p.relative_to(ROOT)):hashlib.sha256(p.read_bytes()).hexdigest() for p in files},
        'phase7_online_ready':False,'limitations':['maintenance certification is not adaptive performance evidence',
            'old dataset epochs and their cached results/models cannot be reused with the repaired data']}


if __name__=='__main__':
    report=summarize()
    path=ROOT/'docs/evidence/heavy_import_repair_20261102.json'
    with path.open('x') as f:json.dump(report,f,ensure_ascii=False,indent=2);f.write('\n')
    print(json.dumps({'status':report['status'],'inserted_points':report['inserted_points'],
                      'per_scale':report['per_scale']},ensure_ascii=False))
