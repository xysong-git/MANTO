"""Compare saved real COUNT references with CSV values; never writes to IoTDB."""
import argparse
import hashlib
import json
from pathlib import Path

from tsdb_optimizer.bandit.workload_audit import audit_source_reference
from tsdb_optimizer.sql_guard import sql_sha256, validate_readonly_sql


def audit_reference(scale, source, sql_file, journal):
    sql = validate_readonly_sql(sql_file.read_text().splitlines()[0])
    events = [json.loads(line) for line in journal.read_text().splitlines()]
    protocol = next(e['body'] for e in events if e['kind']=='protocol')
    reference = next(e['body'] for e in events if e['kind']=='reference')
    if protocol['sql_sha256'] != sql_sha256(sql):
        raise ValueError('saved reference workload differs from selected SQL')
    result = audit_source_reference(scale,source,sql,reference)
    result['journal_sha256'] = hashlib.sha256(journal.read_bytes()).hexdigest()
    return result


if __name__=='__main__':
    p=argparse.ArgumentParser()
    p.add_argument('--live',action='store_true',help='Read fresh Native references instead of historical pilot logs')
    p.add_argument('--output',type=Path,required=True)
    args=p.parse_args()
    manifest=json.loads(Path('query/heavy_scale/manifest.json').read_text())
    results=[]
    adapter=None
    if args.live:
        from tsdb_optimizer.config import load_settings
        from tsdb_optimizer.adapters.iotdb.tree_adapter import IoTDBTreeAdapter
        from tsdb_optimizer.schemas.common import QueryRequest
        adapter=IoTDBTreeAdapter(load_settings())
    try:
        for scale in ('small_scale','medium_scale','large_scale'):
            source=Path(manifest['datasets'][scale]['source_directory'])
            sql_file=Path('query/heavy_scale')/scale/'q03_count_aggregation.sql'
            if adapter:
                sql=validate_readonly_sql(sql_file.read_text().splitlines()[0])
                reference=adapter.execute_readonly(QueryRequest(sql=sql,timeout_ms=30000,
                    source='heavy-import-live-audit'),preview_rows=1)
                result=audit_source_reference(scale,source,sql,reference.model_dump(mode='json'))
                result['reference']=reference.model_dump(mode='json')
                results.append(result)
            else:
                results.append(audit_reference(scale,source,sql_file,
                    Path('data')/f'phase7_heavy_{scale.split("_")[0]}_pilot_20261102'/'journal.jsonl'))
    finally:
        if adapter: adapter.close()
    args.output.write_text(json.dumps(results,ensure_ascii=False,indent=2)+'\n')
    print(json.dumps([{k:r[k] for k in ('scale','source_nonnull_count','database_count','status')} for r in results]))
    if any(r['status']!='PASS' for r in results): raise SystemExit(1)
