from pathlib import Path
import pytest
from tsdb_optimizer.bandit.workload_audit import audit_source_reference


def setup_source(tmp_path):
    p=tmp_path/'d_1';p.mkdir()
    f=p/'BigBatch_0.csv'
    f.write_text('Sensor,s_355\n5000,1\nSensor,s_355\n10000,2\n15000,null\n')
    sql='SELECT count(s_355) FROM root.small_scale.g_0.d_1 WHERE time >= 5000 AND time < 20000;'
    ref={'status':'SUCCESS','preview_truncated':False,'row_count':1,
         'execution_id':'real-or-test-labelled-by-caller',
         'columns':[{'name':'count(root.small_scale.g_0.d_1.s_355)'}],'rows_preview':[[2]]}
    from tsdb_optimizer.sql_guard import sql_sha256, validate_readonly_sql
    ref.update(sql_sha256=sql_sha256(validate_readonly_sql(sql)),strategy_id='S0_NATIVE',served_from_cache=False)
    return f,sql,ref


def test_full_csv_headers_and_nonnull_counts_detect_import_mismatch(tmp_path):
    f,sql,ref=setup_source(tmp_path)
    r=audit_source_reference('small_scale',tmp_path,sql,ref)
    assert r['status']=='PASS' and r['source_nonnull_count']==2
    assert r['source_files'][0]['header_rows']==2
    ref['rows_preview']=[[1]]
    assert audit_source_reference('small_scale',tmp_path,sql,ref)['status']=='MISMATCH'


def test_duplicate_timestamp_and_incomplete_reference_fail_closed(tmp_path):
    f,sql,ref=setup_source(tmp_path)
    ref['preview_truncated']=True
    with pytest.raises(ValueError,match='complete successful'):
        audit_source_reference('small_scale',tmp_path,sql,ref)
    ref['preview_truncated']=False
    f.write_text(f.read_text()+'5000,3\n')
    with pytest.raises(ValueError,match='duplicate source timestamp'):
        audit_source_reference('small_scale',tmp_path,sql,ref)


def test_unmapped_reference_rejected(tmp_path):
    _,sql,ref=setup_source(tmp_path)
    ref['columns'][0]['name']='count(root.other.d_1.s_355)'
    with pytest.raises(ValueError,match='unsupported reference shape'):
        audit_source_reference('small_scale',tmp_path,sql,ref)


def test_wrong_sql_reference_cannot_certify_source(tmp_path):
    _,sql,ref=setup_source(tmp_path)
    ref['sql_sha256']='0'*64
    with pytest.raises(ValueError,match='exact Native SQL'):
        audit_source_reference('small_scale',tmp_path,sql,ref)


def test_import_certificate_requires_hash_endpoint_epoch_and_full_counts(tmp_path):
    import hashlib,json
    from tsdb_optimizer.bandit.workload_audit import verify_import_certificate
    path=tmp_path/'docs/evidence/import.json';path.parent.mkdir(parents=True)
    report={'status':'PASS','endpoint':'127.0.0.1:6667','data_epoch':'new-epoch','value_check_status':'PASS',
        'per_scale':{s:{'devices':n,'series_checked':n*2000,'points_per_series':3000,'failed_devices':0}
            for s,n in [('small_scale',100),('medium_scale',500),('large_scale',1000)]}}
    path.write_text(json.dumps(report))
    entry={'report':'docs/evidence/import.json','sha256':hashlib.sha256(path.read_bytes()).hexdigest()}
    assert verify_import_certificate(tmp_path,entry,'127.0.0.1:6667','new-epoch')['status']=='PASS'
    with pytest.raises(ValueError,match='epoch mismatch'):
        verify_import_certificate(tmp_path,entry,'127.0.0.1:6667','old-epoch')
    with pytest.raises(ValueError,match='endpoint'):
        verify_import_certificate(tmp_path,entry,'other:6667','new-epoch')
    report['per_scale']['large_scale']['series_checked']=1;path.write_text(json.dumps(report))
    with pytest.raises(ValueError,match='digest'):
        verify_import_certificate(tmp_path,entry,'127.0.0.1:6667','new-epoch')
    entry['sha256']=hashlib.sha256(path.read_bytes()).hexdigest()
    with pytest.raises(ValueError,match='coverage'):
        verify_import_certificate(tmp_path,entry,'127.0.0.1:6667','new-epoch')
