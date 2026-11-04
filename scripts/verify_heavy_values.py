"""Read-only, six-type value comparison after the count-complete repair."""
import csv
import hashlib
import json
import math
from pathlib import Path

from tsdb_optimizer.adapters.iotdb.tree_adapter import IoTDBTreeAdapter
from tsdb_optimizer.config import load_settings
from tsdb_optimizer.schemas.common import QueryRequest

SENSORS=['s_0','s_333','s_666','s_1000','s_1333','s_1666']


def source_rows(path):
    rows=[]; indices=None
    with path.open(newline='') as f:
        for row in csv.reader(f):
            if row[0]=='Sensor': indices=[row.index(s) for s in SENSORS];continue
            if indices is None: raise ValueError('missing header')
            values=[row[i] for i in indices]
            if values[0] not in ('true','false'): raise ValueError('unsupported boolean source')
            rows.append([int(row[0]),values[0]=='true',int(values[1]),int(values[2]),
                         float(values[3]),float(values[4]),values[5]])
    return rows


def equal_rows(actual,expected):
    if len(actual)!=len(expected):return False
    for a,b in zip(actual,expected):
        if len(a)!=7 or a[:4]!=b[:4] or a[6]!=b[6]:return False
        if any(type(a[i]) is not type(b[i]) for i in (0,1,2,3,6)):return False
        if a[4] is None or not math.isclose(a[4],b[4],rel_tol=1e-6,abs_tol=1e-6):return False
        if a[5] is None or not math.isclose(a[5],b[5],rel_tol=1e-12,abs_tol=1e-12):return False
    return True


if __name__=='__main__':
    manifest=json.loads(Path('query/heavy_scale/manifest.json').read_text())
    results=[];adapter=IoTDBTreeAdapter(load_settings())
    try:
        for scale,device in [('small_scale','d_1'),('medium_scale','d_6'),('large_scale','d_5')]:
            prefix=manifest['datasets'][scale]['database_prefix']
            for batch in range(3):
                path=Path(manifest['datasets'][scale]['source_directory'])/device/f'BigBatch_{batch}.csv'
                expected=source_rows(path)
                lower=1640966405000+batch*5000000;upper=lower+5000000
                sql=f'SELECT {",".join(SENSORS)} FROM {prefix}.{device} WHERE time >= {lower} AND time < {upper}'
                r=adapter.execute_readonly(QueryRequest(sql=sql,timeout_ms=30000,source='heavy-import-value-audit'),preview_rows=1000)
                good=(r.status.value=='SUCCESS' and not r.preview_truncated and r.row_count==1000
                      and [c.name for c in r.columns]==['Time']+[f'{prefix}.{device}.{s}' for s in SENSORS]
                      and equal_rows(r.rows_preview,expected))
                results.append({'scale':scale,'device':device,'batch':batch,'rows':r.row_count,
                    'sensors':SENSORS,'execution_id':r.execution_id,'sql_sha256':r.sql_sha256,
                    'started_at':r.started_at.isoformat(),'finished_at':r.finished_at.isoformat(),
                    'source_sha256':hashlib.sha256(path.read_bytes()).hexdigest(),
                    'actual_rows_sha256':hashlib.sha256(json.dumps(r.rows_preview).encode()).hexdigest(),
                    'status':'PASS' if good else 'FAIL','error_type':r.error_type,'error_message':r.error_message})
                print(json.dumps(results[-1]),flush=True)
                if not good: raise RuntimeError('source/database value mismatch')
    finally:
        adapter.close()
        Path('docs/evidence/heavy_import_values_20261102.json').write_text(json.dumps(results,indent=2)+'\n')
