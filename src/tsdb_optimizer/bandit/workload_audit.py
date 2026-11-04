"""Fail-closed source COUNT audit for generated, fixed heavy workloads."""
import csv
import hashlib
from pathlib import Path
import re
from ..sql_guard import sql_sha256, validate_readonly_sql


def audit_source_reference(scale, source, sql, reference):
    sql = validate_readonly_sql(sql)
    if reference['sql_sha256'] != sql_sha256(sql) or reference['strategy_id'] != 'S0_NATIVE' or reference['served_from_cache']:
        raise ValueError('reference is not the exact Native SQL execution')
    if reference['status'] != 'SUCCESS' or reference['preview_truncated'] or reference['row_count'] != 1:
        raise ValueError('complete successful COUNT reference required')
    # Auxiliary extraction from the already-guarded, generated COUNT fixture;
    # this is neither a SQL security classifier nor an automatic rewrite.
    bound = re.search(r'WHERE time >= (\d+) AND time < (\d+)$', sql)
    column = reference['columns'][0]['name']
    match = re.fullmatch(r'count\(root\.'+re.escape(scale)+r'\.g_0\.(d_\d+)\.(s_\d+)\)',column)
    if not bound or not match:
        raise ValueError('unsupported reference shape; cannot infer source mapping')
    lower, upper = map(int,bound.groups())
    device,sensor = match.groups()
    files = sorted((source/device).glob('BigBatch_*.csv'))
    if not files: raise ValueError('source device files missing')
    values = {}; evidence = []
    for file in files:
        headers = rows = 0
        index = None
        with file.open(newline='') as handle:
            for row in csv.reader(handle):
                if row[0] == 'Sensor':
                    index = row.index(sensor); headers += 1; continue
                if index is None: raise ValueError('data preceding sensor header')
                timestamp = int(row[0]); rows += 1
                if timestamp in values:
                    raise ValueError('duplicate source timestamp; import ordering UNKNOWN')
                value = row[index].strip()
                if value.lower() == 'nan':
                    raise ValueError('NaN import semantics UNKNOWN')
                values[timestamp] = value
        evidence.append({'path':str(file),'sha256':hashlib.sha256(file.read_bytes()).hexdigest(),
                         'data_rows':rows,'header_rows':headers})
    expected = sum(lower<=t<upper and value.lower() not in ('','null') for t,value in values.items())
    actual = reference['rows_preview'][0][0]
    return {'scale':scale,'column':column,'sql_sha256':sql_sha256(sql),
        'reference_execution_id':reference['execution_id'],
        'source_files':evidence,'unique_source_timestamps':len(values),
        'lower_inclusive_ms':lower,'upper_exclusive_ms':upper,
        'source_nonnull_count':expected,'database_count':actual,
        'status':'PASS' if expected==actual else 'MISMATCH',
        'scope':'first COUNT column of first query only; other source values/import completeness UNKNOWN'}


def verify_import_certificate(root, entry, endpoint, epoch):
    """Bind a completed local maintenance audit to this endpoint and data epoch.

    This is provenance checking of trusted local evidence, not a cryptographic
    attestation from IoTDB. The caller must still guarantee dataset immutability.
    """
    import json
    if entry is None:
        return None
    root=Path(root).resolve()
    path=(root/entry['report']).resolve()
    if not path.is_relative_to(root/'docs/evidence'):
        raise ValueError('import certificate must be a repository evidence artifact')
    raw=path.read_bytes()
    if hashlib.sha256(raw).hexdigest()!=entry['sha256']:
        raise ValueError('import certificate digest mismatch')
    report=json.loads(raw)
    if report['status']!='PASS' or report['endpoint']!=endpoint or report['data_epoch']!=epoch:
        raise ValueError('import certificate status, endpoint or dataset epoch mismatch')
    for scale,devices in [('small_scale',100),('medium_scale',500),('large_scale',1000)]:
        item=report['per_scale'][scale]
        if (item['devices']!=devices or item['series_checked']!=devices*2000
                or item['points_per_series']!=3000 or item['failed_devices']!=0):
            raise ValueError('import certificate lacks complete series-count coverage')
    if report['value_check_status']!='PASS':
        raise ValueError('import certificate lacks sampled value verification')
    return {'report':str(path.relative_to(root)),'sha256':entry['sha256'],
            'endpoint':endpoint,'data_epoch':epoch,'status':'PASS',
            'scope':'all-series count coverage plus sampled six-type values; immutable epoch required'}
