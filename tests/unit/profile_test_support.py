"""Synthetic valid screening journal; never a database measurement."""
from tsdb_optimizer.sql_guard import sql_sha256


def journal_events(obs):
    sql='SELECT s1 FROM root.test.d1'
    case={'scale':'small_scale','type':'q03_count_aggregation','path':'TEST_ONLY.sql','line':1,
          'sql':sql,'sql_sha256':sql_sha256(sql),'file_sha256':'0'*64}
    plan={'repeat':0,'workers':1,'case':case}
    before=obs.model_copy(update={'elapsed_s':0}).model_dump(mode='json',round_trip=True)
    result={'execution_id':'TEST_ONLY_EXECUTION','status':'SUCCESS','strategy_id':'S0_NATIVE',
            'row_count':1,'metrics':{'client_wall_ms':1},'fetch_mode':'FULL',
            'started_at':obs.sampled_at.isoformat(),'finished_at':obs.sampled_at.isoformat(),
            'error_type':None,'warnings':[],'fallback':False,'sql_sha256':sql_sha256(sql)}
    return [{'kind':'protocol','body':{'protocol':'cpu-activity-v2','purpose':'NATIVE_SCREENING_NOT_LEARNING',
        'epoch':'TEST_ONLY','schedule':[plan],'repeats':1,'limitations':[],
        'max_connections':4,'round_limit_s':60,'query_timeout_ms':30000,'cpu_boundaries_percent':[50,90]}},
        {'kind':'round_start','body':{'index':0,**plan,'before':before}},
        {'kind':'sample','body':{'index':0,**obs.model_dump(mode='json',round_trip=True)}},
        {'kind':'round_complete','body':{'index':0,'elapsed_s':1,'results':[result]}},
        {'kind':'complete','body':{'rounds':1}}]
