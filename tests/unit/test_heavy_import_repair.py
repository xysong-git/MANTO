"""Maintenance preflight tests; no database writes are performed here."""
from pathlib import Path
import runpy
import pytest

helpers=runpy.run_path('scripts/repair_heavy_import.py')


def source_file(path, batch=0, bad_time=False, missing_header=False):
    header='Sensor,'+','.join(f's_{i}' for i in range(2000))+'\n'
    with path.open('w') as f:
        for n in range(1000):
            if n%100==0 and not (missing_header and n==100):f.write(header)
            t=1640966405000+batch*5000000+n*5000
            if bad_time and n==10:t+=1
            f.write(str(t)+',1\n')


def test_scan_repeated_headers_and_exact_batch_timestamps(tmp_path):
    path=tmp_path/'batch.csv';source_file(path,batch=1)
    r=helpers['scan_source'](path,1)
    assert r['rows']==1000 and r['headers']==10 and len(r['sha256'])==64


@pytest.mark.parametrize('kwargs',[{'bad_time':True},{'missing_header':True}])
def test_source_timestamp_or_header_corruption_refused(tmp_path,kwargs):
    path=tmp_path/'batch.csv';source_file(path,**kwargs)
    with pytest.raises(ValueError):helpers['scan_source'](path,0)


def test_maintenance_properties_replace_duplicate_destructive_defaults():
    result=helpers['replace_properties']('IS_DELETE_DATA=true\nIS_DELETE_DATA=true\nCREATE_SCHEMA=true\nDB_NAME=old\n',
        {'IS_DELETE_DATA':'false','CREATE_SCHEMA':'false','DB_NAME':'small_scale','IS_DOUBLE_WRITE':'false'})
    assert result.count('IS_DELETE_DATA=')==1
    assert 'IS_DELETE_DATA=false' in result and 'CREATE_SCHEMA=false' in result
    assert 'DB_NAME=small_scale' in result and 'IS_DOUBLE_WRITE=false' in result


def test_import_requires_exhaustive_preflight_before_starting_process(tmp_path):
    plan=helpers['RepairPlan'](created_at='2026-11-02T00:00:00+00:00',endpoint='127.0.0.1:6667',
        scales=[helpers['ScalePlan'](scale='small_scale',devices=['d_0'],prefix='root.small_scale.g_0',source=str(tmp_path))])
    (tmp_path/'plan.json').write_text(plan.model_dump_json())
    (tmp_path/'verify_missing.jsonl').write_text('')
    with pytest.raises(ValueError,match='exhaustive'):helpers['import_missing'](tmp_path)
    assert not (tmp_path/'import.jsonl').exists()


def test_value_comparison_checks_types_timestamps_null_and_float_tolerances():
    eq=runpy.run_path('scripts/verify_heavy_values.py')['equal_rows']
    expected=[[100,True,2,3,0.1,0.2,'text']]
    assert eq([[100,True,2,3,0.10000000149,0.2,'text']],expected)
    for actual in ([[101,True,2,3,0.1,0.2,'text']],[[100,True,2,3,None,0.2,'text']],
                   [[100,True,2,3,0.1,0.2001,'text']],[[100,True,2,3,0.1,0.2,'wrong']]):
        assert not eq(actual,expected)


def test_zero_exit_without_exact_success_matrix_is_failure():
    check=helpers['check_import_result']
    assert not check('startup failed',0,10,2000000)[0]
    assert not check('INGESTION 10 2000000 1 200000 1.0\n',0,10,2000000)[0]
    assert not check('INGESTION 10 2000000 0 0 1.0\nERROR failed\n',0,10,2000000)[0]
    assert check('INGESTION 10 2000000 0 0 1.0\n',0,10,2000000)[0]


def test_plan_cannot_expand_repair_to_existing_third_batch():
    with pytest.raises(ValueError):
        helpers['RepairPlan'](created_at='now',endpoint='127.0.0.1:6667',scales=[],missing_batches=[0,1,2])
