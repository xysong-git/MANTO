"""Synthetic rejection tests for F2/F3. No DB access."""
import copy
import json
from datetime import timedelta
import pytest
from tsdb_optimizer.experiments.profile_audit import summarize
from tsdb_optimizer.monitoring.activity import ActivityObservation, observation
from test_load_protocol import sample
from profile_test_support import journal_events


def fixture_events():
    s,_=sample()
    return journal_events(observation(s.db_host,1))


def save(tmp_path,rows):
    p=tmp_path/'journal.jsonl';p.write_text('\n'.join(json.dumps(e) for e in rows));return p


def test_valid_journal_is_complete_and_successful(tmp_path):
    report=summarize(save(tmp_path,fixture_events()))
    assert report['schedule_complete'] and report['all_executions_successful']


@pytest.mark.parametrize('mutation',[
    'plan_workers','start_workers','sql_hash','empty_results','duplicate_result','aborted_complete',
    'after_complete','missing_terminal','unknown_event','sample_after_round','missing_round',
    'bad_index','bad_repeat','failed_complete','wrong_result_sql','bad_elapsed',
])
def test_corrupted_transcripts_rejected(tmp_path,mutation):
    rows=fixture_events()
    if mutation=='plan_workers': rows[0]['body']['schedule'][0]['workers']=99
    if mutation=='start_workers': rows[1]['body']['workers']=2
    if mutation=='sql_hash': rows[1]['body']['case']['sql_sha256']='f'*64
    if mutation=='empty_results': rows[3]['body']['results']=[]
    if mutation=='duplicate_result': rows[3]['body']['results']*=2
    if mutation=='aborted_complete': rows.insert(-1,{'kind':'aborted','body':{}})
    if mutation=='after_complete': rows.append({'kind':'sample','body':rows[2]['body']})
    if mutation=='missing_terminal': rows.pop()
    if mutation=='unknown_event': rows.insert(2,{'kind':'ignored','body':{}})
    if mutation=='sample_after_round': rows[2],rows[3]=rows[3],rows[2]
    if mutation=='missing_round': rows.pop(3)
    if mutation=='bad_index': rows[1]['body']['index']=2
    if mutation=='bad_repeat': rows[1]['body']['repeat']=3
    if mutation=='failed_complete': rows[3]['body']['results'][0].update(status='ERROR',error_type='TEST_FAILURE')
    if mutation=='wrong_result_sql': rows[3]['body']['results'][0]['sql_sha256']='f'*64
    if mutation=='bad_elapsed': rows[3]['body']['elapsed_s']=-1
    with pytest.raises(ValueError): summarize(save(tmp_path,rows))


def test_genuine_abort_retained_not_promoted(tmp_path):
    rows=fixture_events(); rows[-1]={'kind':'aborted','body':{'error_type':'TEST_FAILURE'}}
    rows[3]['body']['results'][0].update(status='ERROR',error_type='TEST_FAILURE')
    r=summarize(save(tmp_path,rows))
    assert not r['schedule_complete'] and not r['all_executions_successful']
    assert r['rounds'][0]['results'][0]['status']=='ERROR'


@pytest.mark.parametrize('mutation',['client','wrong_bucket','naive','future','stale'])
def test_activity_rejects_inconsistent_evidence(mutation):
    s,_=sample()
    obs=observation(s.db_host,1);body=obs.model_dump(mode='json',round_trip=True)
    if mutation=='client': body['db_host']['source_type']='client_host'
    if mutation=='wrong_bucket':body['cpu_bucket']='HIGH'
    if mutation=='naive':body['sampled_at']='2000-01-01T00:00:00'
    if mutation=='future':body['sampled_at']=(s.db_host.captured_at-timedelta(seconds=1)).isoformat()
    if mutation=='stale':body['sampled_at']=(s.db_host.valid_until+timedelta(seconds=1)).isoformat()
    with pytest.raises(ValueError):ActivityObservation.model_validate(body)
    if mutation in ('future','stale'):
        body['cpu_bucket']='UNKNOWN'
        assert ActivityObservation.model_validate(body).cpu_bucket=='UNKNOWN'


def test_new_transcript_cannot_drop_executed_sql_hash(tmp_path):
    rows=fixture_events();rows[0]['body']['result_identity_version']=2
    rows[3]['body']['results'][0].pop('sql_sha256')
    with pytest.raises(ValueError,match='SQL identity'):summarize(save(tmp_path,rows))


def test_duplicate_execution_ids_rejected_at_correct_cardinality(tmp_path):
    rows=fixture_events()
    rows[0]['body']['schedule'][0]['workers']=2;rows[1]['body']['workers']=2
    rows[3]['body']['results']*=2
    with pytest.raises(ValueError,match='duplicate execution'):summarize(save(tmp_path,rows))


def test_execution_cannot_precede_snapshot(tmp_path):
    rows=fixture_events()
    rows[3]['body']['results'][0]['started_at']='2000-01-01T00:00:00+00:00'
    with pytest.raises(ValueError,match='precedes'):summarize(save(tmp_path,rows))
