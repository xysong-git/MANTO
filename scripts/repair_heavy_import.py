"""Explicit maintenance import, separate from read-only optimizer execution.

Work around iot-benchmark's one-file-per-device map by staging one batch file
per device in each pass. Never delete data or enable a production model.
"""
import argparse
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
from typing import Literal

from pydantic import BaseModel, ConfigDict
from tsdb_optimizer.adapters.iotdb.tree_adapter import IoTDBTreeAdapter
from tsdb_optimizer.config import load_settings
from tsdb_optimizer.schemas.common import QueryRequest, utc_now

ROOT=Path(__file__).resolve().parents[1]
BENCH=Path('/opt/program-code/iot-benchmark/iotdb-2.0/target/iot-benchmark-iotdb-2.0/iot-benchmark-iotdb-2.0')
DEFAULT=ROOT/'data/heavy_import_repair_20261102'


class ScalePlan(BaseModel):
    model_config=ConfigDict(extra='forbid',frozen=True)
    scale: str
    devices: list[str]
    prefix: str
    source: str


class RepairPlan(BaseModel):
    model_config=ConfigDict(extra='forbid',frozen=True)
    created_at: str
    endpoint: Literal['127.0.0.1:6667']
    server_version: Literal['2.0.6'] = '2.0.6'
    scales: list[ScalePlan]
    missing_batches: tuple[Literal[0],Literal[1]] = (0,1)
    first_ms: Literal[1640966405000] = 1640966405000
    missing_upper_ms: Literal[1640976405000] = 1640976405000
    complete_upper_ms: Literal[1640981405000] = 1640981405000
    sensor_count: Literal[2000] = 2000
    points_per_series: Literal[3000] = 3000
    data_epoch: str = 'heavy-complete-20261102-v2'


def scan_source(path, batch):
    """Read all timestamp rows; repeated headers must delimit exactly 100 rows."""
    digest=hashlib.sha256(); rows=headers=0
    start=1640966405000+batch*5000000
    first_header=None
    with path.open('rb') as f:
        for line in f:
            digest.update(line)
            token=line.split(b',',1)[0]
            if token==b'Sensor':
                if rows != headers*100: raise ValueError(f'header boundary mismatch: {path}')
                header=line.rstrip(b'\r\n')
                if first_header is None:
                    first_header=header
                    expected=b'Sensor,'+b','.join(f's_{i}'.encode() for i in range(2000))
                    if header != expected: raise ValueError(f'header/schema mismatch: {path}')
                elif header != first_header: raise ValueError(f'changed header: {path}')
                headers+=1
            else:
                if not headers or int(token)!=start+rows*5000:
                    raise ValueError(f'noncontiguous source timestamp: {path}')
                rows+=1
    if (rows,headers)!=(1000,10): raise ValueError(f'incomplete source file: {path}')
    return {'path':str(path),'sha256':digest.hexdigest(),'rows':rows,'headers':headers}


def replace_properties(original, updates):
    lines=[]; seen=set()
    for line in original.splitlines():
        if line.strip() and not line.lstrip().startswith('#') and '=' in line:
            key=line.split('=',1)[0].strip()
            if key in updates:
                if key not in seen: lines.append(f'{key}={updates[key]}')
                seen.add(key);continue
        lines.append(line)
    lines.extend(f'{k}={v}' for k,v in updates.items() if k not in seen)
    return '\n'.join(lines)+'\n'


def prepare(output):
    settings=load_settings()
    if settings.iotdb_host!='127.0.0.1' or settings.iotdb_port!=6667:
        raise ValueError('this audited repair targets only 127.0.0.1:6667')
    output.mkdir(parents=True,exist_ok=False)
    os.chmod(output,0o700)
    manifest=json.loads((ROOT/'query/heavy_scale/manifest.json').read_text())
    scales=[ScalePlan(scale=k,devices=v['devices'],prefix=v['database_prefix'],source=v['source_directory'])
            for k,v in manifest['datasets'].items()]
    plan=RepairPlan(created_at=utc_now().isoformat(),endpoint='127.0.0.1:6667',scales=scales)
    original=(BENCH/'conf/config.properties').read_text()
    with (output/'source_inventory.jsonl').open('x') as inventory:
        for scale in scales:
            for device in scale.devices:
                for batch in range(3):
                    record=scan_source(Path(scale.source)/device/f'BigBatch_{batch}.csv',batch)
                    inventory.write(json.dumps(record)+'\n')
            print(json.dumps({'source_validated':scale.scale,'devices':len(scale.devices)}),flush=True)
            for batch in plan.missing_batches:
                base=output/scale.scale/f'batch_{batch}'; stage=base/'source';conf=base/'conf'
                conf.mkdir(parents=True);stage.mkdir()
                for name in ('schema.txt','info.txt'):
                    (stage/name).symlink_to(Path(scale.source)/name)
                for device in scale.devices:
                    folder=stage/device;folder.mkdir()
                    (folder/f'BigBatch_{batch}.csv').symlink_to(Path(scale.source)/device/f'BigBatch_{batch}.csv')
                properties=replace_properties(original,{
                    'HOST':settings.iotdb_host,'PORT':str(settings.iotdb_port),
                    'USERNAME':settings.iotdb_user,'PASSWORD':settings.iotdb_password.get_secret_value(),
                    'DB_SWITCH':'IoTDB-200-SESSION_BY_TABLET','IoTDB_DIALECT_MODE':'tree',
                    'DB_NAME':scale.scale,'FILE_PATH':str(stage),
                    'DEVICE_NUMBER':str(len(scale.devices)),'SENSOR_NUMBER':'2000',
                    'BENCHMARK_WORK_MODE':'verificationWriteMode','IS_DELETE_DATA':'false',
                    'CREATE_SCHEMA':'false','IS_DOUBLE_WRITE':'false','IS_COPY_MODE':'false',
                    'SCHEMA_CLIENT_NUMBER':'1','DATA_CLIENT_NUMBER':'2',
                    'BATCH_SIZE_PER_WRITE':'100','BIG_BATCH_SIZE':'10','LOOP':'30',
                    'TEST_MAX_TIME':'0','WRITE_OPERATION_TIMEOUT_MS':'30000',
                    'READ_OPERATION_TIMEOUT_MS':'30000','TEST_DATA_PERSISTENCE':'None'})
                cfg=conf/'config.properties';cfg.write_text(properties);os.chmod(cfg,0o600)
                (conf/'function.xml').symlink_to(BENCH/'conf/function.xml')
                # Keep logs in the repair workspace, with no credential-bearing config dump.
                (conf/'logback.xml').write_text('''<configuration><appender name="FILE" class="ch.qos.logback.core.FileAppender"><file>'''+str(base/'benchmark.log')+'''</file><encoder><pattern>%date %-5level %logger - %msg%n</pattern></encoder></appender><root level="INFO"><appender-ref ref="FILE"/></root></configuration>''')
    (output/'plan.json').write_text(plan.model_dump_json(indent=2)+'\n')
    print('prepared; no database writes',flush=True)


def verify(output, mode):
    plan=RepairPlan.model_validate_json((output/'plan.json').read_text())
    settings=load_settings()
    if f'{settings.iotdb_host}:{settings.iotdb_port}'!=plan.endpoint: raise ValueError('endpoint changed')
    report=output/f'verify_{mode}.jsonl'
    if report.exists() or any((output/f'{scale.scale}_{mode}.jsonl').exists() for scale in plan.scales):
        raise FileExistsError('audit outputs exist; preserve prior evidence')
    def worker(scale):
        adapter=IoTDBTreeAdapter(settings); results=[]
        try:
            doctor=adapter.doctor()
            if not doctor.ok or doctor.server.version!=plan.server_version or doctor.server.model!='tree':
                raise ValueError('server identity mismatch')
            for device in scale.devices:
                upper=plan.missing_upper_ms if mode=='missing' else plan.complete_upper_ms
                # Verified on this 2.0.6 tree instance: COUNT(*) returns every expected measurement.
                sql='SELECT count(*)'
                sql+=f' FROM {scale.prefix}.{device} WHERE time >= {plan.first_ms} AND time < {upper}'
                r=adapter.execute_readonly(QueryRequest(sql=sql,timeout_ms=30000,source='heavy-import-audit'),preview_rows=1)
                expected=0 if mode=='missing' else plan.points_per_series
                columns={f'count({scale.prefix}.{device}.s_{i})' for i in range(plan.sensor_count)}
                good=(r.status.value=='SUCCESS' and not r.preview_truncated and r.row_count==1
                      and len(r.columns)==plan.sensor_count and {c.name for c in r.columns}==columns
                      and len(r.rows_preview[0])==plan.sensor_count and all(v==expected for v in r.rows_preview[0]))
                row={'scale':scale.scale,'device':device,'execution_id':r.execution_id,'sql_sha256':r.sql_sha256,
                     'started_at':r.started_at.isoformat(),'finished_at':r.finished_at.isoformat(),
                     'expected_per_series':expected,'series_checked':len(r.columns),
                     'counts_histogram':dict(Counter(r.rows_preview[0])) if r.rows_preview else {},
                     'status':'PASS' if good else 'FAIL','error_type':r.error_type,'error_message':r.error_message}
                results.append(row)
                if not good: raise ValueError(json.dumps(row))
                if len(results)%25==0: print(json.dumps({'mode':mode,'scale':scale.scale,'devices_checked':len(results)}),flush=True)
        finally:
            adapter.close()
            # One file per worker, then merge only after all workers terminate.
            (output/f'{scale.scale}_{mode}.jsonl').write_text(''.join(json.dumps(r)+'\n' for r in results))
        return results
    all_rows=[]
    with ThreadPoolExecutor(max_workers=3) as pool:
        for rows in pool.map(worker,plan.scales): all_rows.extend(rows)
    report.write_text(''.join(json.dumps(r)+'\n' for r in all_rows))
    print(json.dumps({'mode':mode,'devices':len(all_rows),'series_checked':sum(r['series_checked'] for r in all_rows),'status':'PASS'}))


def validate_staging(plan, output):
    for scale in plan.scales:
        for batch in plan.missing_batches:
            base=output/scale.scale/f'batch_{batch}'
            if not (base/'conf/function.xml').is_file():
                raise ValueError('missing benchmark function.xml startup resource')
            properties={line.split('=',1)[0].strip():line.split('=',1)[1].strip()
                for line in (base/'conf/config.properties').read_text().splitlines()
                if line.strip() and not line.lstrip().startswith('#') and '=' in line}
            required={'IS_DELETE_DATA':'false','CREATE_SCHEMA':'false','IS_DOUBLE_WRITE':'false',
                'DB_NAME':scale.scale,'HOST':'127.0.0.1','PORT':'6667',
                'FILE_PATH':str(base/'source'),'DEVICE_NUMBER':str(len(scale.devices)),
                'SENSOR_NUMBER':'2000','DATA_CLIENT_NUMBER':'2','BATCH_SIZE_PER_WRITE':'100',
                'BIG_BATCH_SIZE':'10','BENCHMARK_WORK_MODE':'verificationWriteMode'}
            if any(properties.get(k)!=v for k,v in required.items()):
                raise ValueError('staged maintenance configuration differs from audited plan')
            expected={base/'source'/d/f'BigBatch_{batch}.csv' for d in scale.devices}
            if set((base/'source').rglob('*.csv'))!=expected:
                raise ValueError('stage must contain exactly one file per device')
            for f in expected:
                if f.resolve()!=(Path(scale.source)/f.parent.name/f.name).resolve():
                    raise ValueError('staged source mapping changed')


def check_import_result(text, returncode, batches, points):
    matches=re.findall(r'^INGESTION\s+(\d+)\s+(\d+)\s+(\d+)\s+(\d+)\s+',text,re.M)
    warnings=[line for line in text.splitlines() if re.search(r'\bWARN\b',line)]
    unexpected=[line for line in warnings if not ('TSFileDescriptor' in line and
        line.endswith('not found iotdb-system.properties, use the default configs.'))]
    valid=(returncode==0 and len(matches)==1 and tuple(map(int,matches[0]))==(batches,points,0,0)
        and not re.search(r'\bERROR\b',text) and not unexpected)
    return valid,matches,warnings


def import_missing(output):
    plan=RepairPlan.model_validate_json((output/'plan.json').read_text())
    rows=[json.loads(s) for s in (output/'verify_missing.jsonl').read_text().splitlines()]
    expected={(s.scale,d) for s in plan.scales for d in s.devices}
    if len(rows)!=len(expected) or {(r['scale'],r['device']) for r in rows}!=expected or any(
            r['status']!='PASS' or r['expected_per_series']!=0 or r['series_checked']!=2000 for r in rows):
        raise ValueError('exhaustive missing-range preflight required')
    validate_staging(plan,output)
    journal=output/'import.jsonl'
    if journal.exists(): raise FileExistsError('never blindly replay a partial import; re-audit first')
    with journal.open('x') as log:
        for scale in plan.scales:
            for batch in plan.missing_batches:
                base=output/scale.scale/f'batch_{batch}'
                command=[str(BENCH/'benchmark.sh'),'-cf',str(base/'conf'),'-heapsize','512M','-maxheapsize','2G']
                event={'scale':scale.scale,'batch':batch,'started_at':utc_now().isoformat(),
                       'expected_batches':len(scale.devices)*10,'expected_points':len(scale.devices)*2000*1000}
                log.write(json.dumps({'kind':'start',**event})+'\n');log.flush()
                with (base/'console.log').open('x') as console:
                    completed=subprocess.run(command,cwd=output,stdout=console,stderr=subprocess.STDOUT,check=False,timeout=900)
                text=(base/'benchmark.log').read_text()
                valid,matches,warnings=check_import_result(text,completed.returncode,event['expected_batches'],event['expected_points'])
                result={'kind':'complete',**event,'finished_at':utc_now().isoformat(),
                        'returncode':completed.returncode,'startup_warnings':warnings,'result_matrix':matches,'status':'PASS' if valid else 'FAIL'}
                log.write(json.dumps(result)+'\n');log.flush();print(json.dumps(result),flush=True)
                if not valid: raise RuntimeError('import failed or warned; preserve logs and stop without automatic retry')


if __name__=='__main__':
    p=argparse.ArgumentParser();p.add_argument('action',choices=('prepare','verify-missing','import','verify-complete'))
    p.add_argument('--output',type=Path,default=DEFAULT);args=p.parse_args();out=args.output.resolve()
    if args.action=='prepare':prepare(out)
    elif args.action=='import':import_missing(out)
    else:verify(out,args.action.split('-',1)[1])
