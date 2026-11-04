"""Post-fix rejection regressions for the four original review counterexamples.
No live DB calls; copied journals and explicit synthetic scheduler only.
"""
import pytest
import json
from pathlib import Path
import runpy
from types import SimpleNamespace as N

ROOT=Path(__file__).resolve().parents[2]
PROFILE=runpy.run_path(str(ROOT/'scripts/phase7_profile_load.py'))
SUMMARY=runpy.run_path(str(ROOT/'scripts/phase7_summarize_load_profile.py'))['summarize']
RAW=ROOT/'data/phase7_cpu_profile_counts_20261102/journal.jsonl'


def events(): return [json.loads(l) for l in RAW.read_text().splitlines()]
def write(tmp_path, data):
    p=tmp_path/'copy.jsonl';p.write_text('\n'.join(json.dumps(e) for e in data));return p


def test_reproduces_schedule_mismatch_accepted(tmp_path):
    rows=events()
    rows[0]['body']['schedule'][0]['workers']=99
    with pytest.raises(ValueError):SUMMARY(write(tmp_path,rows))


def test_reproduces_aborted_then_complete_accepted(tmp_path):
    rows=events();rows.insert(-1,{'kind':'aborted','body':{'error_type':'TEST_ONLY'}})
    with pytest.raises(ValueError,match='terminal'):SUMMARY(write(tmp_path,rows))


def test_reproduces_client_source_bad_bucket_and_naive_time_accepted():
    body=next(e['body'] for e in events() if e['kind']=='sample').copy()
    body.pop('index');body['db_host']=dict(body['db_host']);body['db_host'].pop('valid_until',None)
    body['db_host']['source_type']='client_host'
    body['db_host']['cpu_percent']=1
    body['cpu_bucket']='HIGH'
    body['sampled_at']='2000-01-01T00:00:00'
    with pytest.raises(ValueError):PROFILE['ActivityObservation'].model_validate(body)


def test_reproduces_sampling_after_round_deadline(tmp_path, monkeypatch):
    # Same failure class, now checked against the real process boundary rather
    # than permitting a fake Future to overrun the deadline.
    from test_experiment_supervisor import test_blocking_worker_is_killed_without_waiting_for_thread_shutdown
    test_blocking_worker_is_killed_without_waiting_for_thread_shutdown(tmp_path,'fetch')
