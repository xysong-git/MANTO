#!/usr/bin/env python
"""Generate fixture-shaped IoTDB tree SELECT workloads from actual CSV/schema metadata.

Never connects to or executes against a database. All output is explicit SQL.
"""
import argparse
import csv
import hashlib
import json
import math
from pathlib import Path
import random
import re
import time

from tsdb_optimizer.sql_guard import validate_readonly_sql

BASE = Path('/opt/program-code/iot-benchmark/iotdb-2.0/target/iot-benchmark-iotdb-2.0/iot-benchmark-iotdb-2.0/data')
SCALES = {'small_scale': 100, 'medium_scale': 500, 'large_scale': 1000}
KINDS = {
    'q01_range_scan': '宽时间范围、多设备、多数值测点扫描',
    'q02_value_filter_scan': '宽范围扫描加两个数值谓词',
    'q03_count_aggregation': '宽范围多测点COUNT聚合',
    'q04_sum_aggregation': '宽范围多数值测点SUM聚合',
    'q05_group_by_time': '宽范围时间窗口COUNT聚合',
    'q06_group_by_time_value_filter': '数值过滤加时间窗口COUNT聚合',
    'q07_range_descending': '宽范围原始数据按时间倒序',
    'q08_group_by_time_descending': '时间窗口COUNT聚合按时间倒序',
}
NUMERIC_CODES = {'1','2','3','4'}  # verified local SensorType enum: INT32/INT64/FLOAT/DOUBLE


def last_line(path):
    with path.open('rb') as f:
        f.seek(0, 2)
        size = f.tell()
        window = min(65536, size)
        while True:
            f.seek(size-window)
            lines = f.read().rstrip(b'\r\n').split(b'\n')
            if len(lines)>1 or window==size:
                return lines[-1].decode().strip()
            window = min(size, window*2)


def inventory(root, name, expected):
    directory = root/name
    props = {}
    for line in (directory/'info.txt').read_text().splitlines():
        key,value = line.split('=',1)
        props[key] = value.strip("'\"")
    if (props.get('TIMESTAMP_PRECISION')!='ms' or props.get('IS_SENSOR_TS_ALIGNMENT')!='true'
            or props.get('IS_OUT_OF_ORDER')!='false' or props.get('IS_REGULAR_FREQUENCY')!='true'):
        raise ValueError('this generator requires verified regular aligned millisecond datasets')
    schemas = {}
    schema_hash = hashlib.sha256()
    with (directory/'schema.txt').open('rb') as f:
        for line in f:
            schema_hash.update(line)
            device,sensor,code = line.decode().split()
            if not re.fullmatch(r'd_\d+', device) or not re.fullmatch(r's_\d+',sensor):
                raise ValueError('unverified identifier syntax')
            fields = schemas.setdefault(device,{})
            if sensor in fields: raise ValueError('duplicate schema entry')
            fields[sensor] = code
    devices = sorted(schemas,key=lambda d:int(d[2:]))
    if len(devices)!=expected or int(props['DEVICE_NUMBER'])!=expected:
        raise ValueError('device count differs from requested scale')
    schema = schemas[devices[0]]
    if len(schema)!=2000 or any(fields!=schema for fields in schemas.values()):
        raise ValueError('expected identical 2000-sensor schema on every device')
    numeric = sorted((s for s,c in schema.items() if c in NUMERIC_CODES),key=lambda s:int(s[2:]))
    step = int(props['POINT_STEP'])
    device_bounds = []
    csv_count = 0
    file_inventory = []
    for device in devices:
        files = sorted((directory/device).glob('*.csv'),key=lambda p:int(p.stem.split('_')[-1]))
        if not files: raise ValueError('device has no CSV files')
        bounds = []
        for path in files:
            with path.open() as f:
                header = next(csv.reader([f.readline()]))
                first = f.readline()
            if header[0]!='Sensor' or header[1:]!=list(schema):
                raise ValueError('CSV header differs from schema')
            lo, hi = int(first.split(',',1)[0]),int(last_line(path).split(',',1)[0])
            if hi<lo or (hi-lo)%step: raise ValueError('irregular boundary timestamp')
            if bounds and bounds[-1][1]+step!=lo: raise ValueError('batch boundary discontinuity')
            bounds.append((lo,hi))
            file_inventory.append({'file':str(path.relative_to(directory)), 'bytes':path.stat().st_size,
                'first_timestamp':lo, 'last_timestamp':hi})
            csv_count+=1
        device_bounds.append((bounds[0][0],bounds[-1][1]))
    if len(set(device_bounds))!=1: raise ValueError('device time bounds differ')
    lo, hi = device_bounds[0]
    points = (hi-lo)//step+1
    if points != int(props['LOOP'])*int(props['BATCH_SIZE_PER_WRITE']):
        raise ValueError('metadata and observed boundary time span differ')
    return {'name':name,'database_prefix':f'root.{name}.g_0','source_directory':str(directory),
        'device_count':len(devices),'sensor_count':len(schema),'series_count':len(devices)*len(schema),
        'devices':devices,'numeric_sensors':numeric,'sensor_type_codes':schema,
        'min_time':lo,'max_time':hi,'step_ms':step,'expected_points_per_series':points,
        'csv_count':csv_count,'schema_sha256':schema_hash.hexdigest(),
        'verification_scope':'all schema entries and every CSV header/first/last timestamp; interior rows not fully scanned',
        'interior_row_completeness':'UNKNOWN_NOT_FULL_SCAN','csv_files':file_inventory}


def make_query(meta, kind, index, seed):
    kind_index=list(KINDS).index(kind)
    rng=random.Random(seed+kind_index*100000+index)
    grouped=kind in ('q05_group_by_time','q06_group_by_time_value_filter','q08_group_by_time_descending')
    sensor_count=8 if grouped else 16
    sensors=sorted(rng.sample(meta['numeric_sensors'],sensor_count),key=lambda s:int(s[2:]))
    points=meta['expected_points_per_series']
    span=rng.randint(math.ceil(points*.6),points)
    offset=rng.randint(0,points-span)
    lo=meta['min_time']+offset*meta['step_ms']
    upper=lo+span*meta['step_ms']
    interval=rng.choice([20000,30000,60000])
    threshold=rng.choice([-10,-5,0,5,10])
    # Device sampling uses an independent stream so same line keeps sensor/time parameters across scales.
    drng=random.Random(seed+kind_index*100000+index+10000000)
    devices=sorted(drng.sample(meta['devices'],max(2,math.ceil(meta['device_count']*.1))),key=lambda d:int(d[2:]))
    paths=[meta['database_prefix']+'.'+d for d in devices]
    if grouped or kind=='q03_count_aggregation':
        select=', '.join(f'count({s})' for s in sensors)
    elif kind=='q04_sum_aggregation':
        select=', '.join(f'sum({s})' for s in sensors)
    else:
        select=', '.join(sensors)
    sql=f'SELECT {select} FROM '+', '.join(paths)
    sql+=f' WHERE time >= {lo} AND time < {upper}'
    if kind in ('q02_value_filter_scan','q06_group_by_time_value_filter'):
        sql+=f' AND {paths[0]}.{sensors[0]} > {threshold} AND {paths[-1]}.{sensors[-1]} > {threshold}'
    if grouped:
        sql+=f' GROUP BY ([{lo}, {upper}), {interval}ms)'
    if kind in ('q07_range_descending','q08_group_by_time_descending'):
        sql+=' ORDER BY time DESC'
    return sql+';'


def main():
    p=argparse.ArgumentParser()
    p.add_argument('--data-root',type=Path,default=BASE)
    p.add_argument('--output',type=Path,default=Path('query/heavy_scale'))
    p.add_argument('--count',type=int,default=1000)
    p.add_argument('--seed',type=int,default=20261102)
    args=p.parse_args()
    if args.count<1: raise ValueError('count must be positive')
    if args.output.exists(): raise ValueError('output already exists; use a new directory')
    manifest={'schema_version':1,'seed':args.seed,'queries_per_file':args.count,'types':KINDS,
        'total_queries':0,'datasets':{},'files':[],
        'heavy_status':'DESIGNED_HEAVY_NOT_BENCHMARKED','server_probe':'See validation.json; UNKNOWN until explicitly probed',
        'semantics':'tree SELECT only; no rewriting/Registry changes; half-open millisecond ranges',
        'comparability':'same line/type uses same sensors/time across scales; 10% device selection scales with dataset'}
    args.output.mkdir(parents=True)
    started=time.monotonic()
    for name,expected in SCALES.items():
        meta=inventory(args.data_root,name,expected)
        manifest['datasets'][name]=meta
        (args.output/name).mkdir()
        for kind in KINDS:
            queries=[make_query(meta,kind,i,args.seed) for i in range(args.count)]
            if len(set(queries))!=args.count: raise ValueError('duplicate queries')
            for sql in queries:
                validate_readonly_sql(sql)
            path=args.output/name/(kind+'.sql')
            content='\n'.join(queries)+'\n'
            path.write_text(content)
            manifest['files'].append({'path':str(path.relative_to(args.output)),
                'query_count':len(queries),'unique_query_count':len(set(queries)),
                'sha256':hashlib.sha256(content.encode()).hexdigest(),
                'readonly_guard':'PASS','database_execution':'NOT_EXECUTED'})
            manifest['total_queries']+=len(queries)
            print(json.dumps({'file':str(path),'count':len(queries),'elapsed_s':round(time.monotonic()-started,1)}),flush=True)
    (args.output/'manifest.json').write_text(json.dumps(manifest,ensure_ascii=False,indent=2)+'\n')


if __name__=='__main__': main()
