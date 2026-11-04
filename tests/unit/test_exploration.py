"""Evidence protocol tests; historical fixtures are not new live measurements."""
import json
from datetime import timedelta
from pathlib import Path
import random

import pytest
from pydantic import ValidationError

from tsdb_optimizer.bandit.exploration import Assignment, Trial, draw, summarize
from tsdb_optimizer.schemas.learning import Experience


def trial():
    raw=json.loads(Path('docs/evidence/phase5_learning_fixes_2026-11-02.json').read_text())
    e=next(Experience.model_validate(e) for e in raw['experiences']
           if e['sample_status']=='VALID' and e['strategy_id']=='S0_NATIVE')
    a=Assignment(phase='train',seed=1,index=0,workload_sha256=e.query_hash,
        cache_state_before='EMPTY',background_workers=0,strategy_id=e.strategy_id,
        assigned_at=e.record.started_at-timedelta(milliseconds=1))
    return Trial(assignment=a,experience=e,service_wall_ms=10,
                 background_started=0,background_completed=0,background_inflight_before=0)


def test_seeded_assignment_has_real_probability_and_both_arms():
    a=trial().assignment
    context=a.model_dump(exclude={'assignment_id','strategy_id','propensity'})
    def sequence():
        rng=random.Random(20261102)
        return [draw(rng,**context) for _ in range(128)]
    first,second=sequence(),sequence()
    assert [a.strategy_id for a in first]==[a.strategy_id for a in second]
    assert {a.strategy_id for a in first}=={'S0_NATIVE','S1_REPEAT_CACHE'}
    assert all(a.propensity==.5 for a in first)
    assert len({a.assignment_id for a in first})==128


@pytest.mark.parametrize('changes', [
    {'propensity':1.0}, {'background_workers':4}, {'strategy_id':'UNKNOWN'},
])
def test_assignment_rejects_invalid_control(changes):
    with pytest.raises(ValidationError):
        Assignment.model_validate({**trial().assignment.model_dump(),**changes})


def test_trial_requires_preexecution_assignment_and_result_source():
    t=trial()
    for a in [t.assignment.model_copy(update={'assigned_at':t.experience.record.finished_at}),
              t.assignment.model_copy(update={'strategy_id':'S1_REPEAT_CACHE'})]:
        with pytest.raises(ValueError):
            Trial.model_validate({**t.model_dump(round_trip=True),'assignment':a})


def test_duplicate_and_overlap_refused_not_double_counted():
    t=trial()
    with pytest.raises(ValueError,match='duplicate'):
        summarize([t,t])
    from tsdb_optimizer.schemas.common import new_id
    body=t.model_dump(round_trip=True)
    body['assignment'].update(assignment_id=new_id(),phase='holdout')
    eid=new_id()
    body['experience']['execution_id']=eid
    body['experience']['record']['execution_id']=eid
    with pytest.raises(ValueError,match='overlap'):
        summarize([t,Trial.model_validate(body)])


def test_reporting_does_not_enable_deployment_or_invent_db_load():
    r=summarize([trial()])
    assert r['online_ready'] is False
    cell=next(iter(r['cells'].values()))
    assert cell['per_arm_sample_gate'] is False
    assert cell['s1_minus_s0_mean_ms'] is None
    assert cell['confidence_interval'] is None
    assert cell['result_sources']=={'SERVER':1}


def test_journal_audit_requires_complete_unmodified_record(tmp_path):
    import runpy
    analyze=runpy.run_path('scripts/phase7_analyze_exploration.py')['analyze']
    t=trial()
    # Seed must reproduce this actual assigned action.
    seed=next(s for s in range(100) if random.Random(s).choice(('S0_NATIVE','S1_REPEAT_CACHE'))==t.assignment.strategy_id)
    t=t.model_copy(update={'assignment':t.assignment.model_copy(update={'seed':seed})})
    summary=summarize([t])
    events=[{'kind':'protocol','body':{'operator_immutable_guarantee':True,'epoch':t.experience.context.dataset_epoch}},
            {'kind':'assignment','body':t.assignment.model_dump(mode='json')},
            {'kind':'trial','body':t.model_dump(mode='json',round_trip=True)},
            {'kind':'round_complete','body':{'elapsed_s':1}},
            {'kind':'summary','body':summary}]
    path=tmp_path/'journal.jsonl'
    def write(rows):
        path.write_text('\n'.join(json.dumps(row) for row in rows))
    write(events)
    assert analyze(path)['randomization_audit']['verified']
    write(events[:-1])
    with pytest.raises(ValueError,match='incomplete'):
        analyze(path)
    changed=json.loads(json.dumps(events))
    changed[1]['body']['strategy_id']='S1_REPEAT_CACHE'
    write(changed)
    with pytest.raises(ValueError,match='random assignment'):
        analyze(path)
    write([*events[:-1],{'kind':'aborted','body':{}}])
    with pytest.raises(ValueError,match='aborted'):
        analyze(path)


def test_background_counters_cannot_claim_more_connections_than_started():
    t=trial()
    with pytest.raises(ValueError,match='counters'):
        Trial.model_validate({**t.model_dump(round_trip=True),'background_inflight_before':1})


def test_isolated_endpoint_mismatch_stops_before_database_or_output(tmp_path, monkeypatch):
    import runpy
    from types import SimpleNamespace
    run=runpy.run_path('scripts/phase7_exploration.py')['run']
    called=[]
    def settings(**kwargs):
        called.append(kwargs)
        return SimpleNamespace(iotdb_host='isolated-host',iotdb_port=6667)
    def forbidden(*args, **kwargs):
        raise AssertionError('must not connect')
    monkeypatch.setitem(run.__globals__,'load_settings',settings)
    monkeypatch.setitem(run.__globals__,'IoTDBTreeAdapter',forbidden)
    args=SimpleNamespace(immutable_epoch_confirmed=True,trials=2,env_file=tmp_path/'isolated.env',
        expect_endpoint='original-host:6667',output=tmp_path/'out')
    with pytest.raises(ValueError,match='endpoint'):
        run(args)
    assert called==[{'env_file':args.env_file,'environ':{}}]
    assert not args.output.exists()


def test_workload_selection_keeps_exact_line_and_hash(tmp_path):
    import runpy, hashlib
    select = runpy.run_path('scripts/phase7_exploration.py')['select_workload']
    path = tmp_path/'queries.sql'
    path.write_text('SELECT s1 FROM root.test.d1;\nSELECT count(s1) FROM root.test.d1;\n')
    sql, evidence = select(path, 2)
    assert sql == 'SELECT count(s1) FROM root.test.d1'
    assert evidence['line'] == 2
    assert evidence['file_sha256'] == hashlib.sha256(path.read_bytes()).hexdigest()
    from tsdb_optimizer.sql_guard import sql_sha256
    assert evidence['sql_sha256'] == sql_sha256(sql)
    for line in (0, -1, 3):
        with pytest.raises(ValueError, match='line number'):
            select(path, line)


def test_workload_selection_rejects_write_and_multiple_statements(tmp_path):
    import runpy
    select = runpy.run_path('scripts/phase7_exploration.py')['select_workload']
    path = tmp_path/'queries.sql'
    for sql in ('DELETE FROM root.test.d1 WHERE time > 0;',
                'SELECT s1 FROM root.test.d1; SELECT s1 FROM root.test.d1;', ''):
        path.write_text(sql+'\n')
        from tsdb_optimizer.errors import UnsafeSQLError
        with pytest.raises(UnsafeSQLError):
            select(path, 1)


def test_repeated_round_audit_checks_workload_and_completion(tmp_path):
    import runpy
    analyze=runpy.run_path('scripts/phase7_analyze_exploration.py')['analyze']
    t=trial()
    seed=next(s for s in range(100) if random.Random(s).choice(('S0_NATIVE','S1_REPEAT_CACHE'))==t.assignment.strategy_id)
    t=t.model_copy(update={'assignment':t.assignment.model_copy(update={'seed':seed})})
    events=[{'kind':'protocol','body':{'operator_immutable_guarantee':True,
        'epoch':t.experience.context.dataset_epoch,'rounds_per_cell':2,
        'sql_sha256':t.experience.query_hash,'workload':{'sql_sha256':t.experience.query_hash}}},
        {'kind':'assignment','body':t.assignment.model_dump(mode='json')},
        {'kind':'trial','body':t.model_dump(mode='json',round_trip=True)},
        {'kind':'round_complete','body':{'elapsed_s':1,'phase':'train','cache':'EMPTY','workers':0,'round_index':0}},
        {'kind':'round_complete','body':{'elapsed_s':1,'phase':'train','cache':'EMPTY','workers':0,'round_index':1}},
        {'kind':'summary','body':summarize([t])}]
    path=tmp_path/'journal.jsonl'
    def write(): path.write_text('\n'.join(json.dumps(e) for e in events))
    write()
    assert analyze(path)['randomization_audit']['verified']
    events[0]['body']['workload']['sql_sha256']='0'*64
    write()
    with pytest.raises(ValueError,match='workload identity'): analyze(path)
    events[0]['body']['workload']['sql_sha256']=t.experience.query_hash
    events.pop(-2)
    write()
    with pytest.raises(ValueError,match='completion'): analyze(path)


def test_import_certificate_cannot_be_added_after_assignment(tmp_path):
    import runpy
    analyze=runpy.run_path('scripts/phase7_analyze_exploration.py')['analyze']
    t=trial()
    seed=next(s for s in range(100) if random.Random(s).choice(('S0_NATIVE','S1_REPEAT_CACHE'))==t.assignment.strategy_id)
    a=t.assignment.model_copy(update={'seed':seed})
    events=[{'kind':'protocol','body':{'operator_immutable_guarantee':True}},
        {'kind':'assignment','body':a.model_dump(mode='json')},
        {'kind':'dataset_import_certification','body':{}}]
    path=tmp_path/'journal.jsonl';path.write_text('\n'.join(json.dumps(e) for e in events))
    with pytest.raises(ValueError,match='precede assignments'):analyze(path)


def test_production_protocol_rejects_missing_whole_cells_and_seed_drift(tmp_path):
    import runpy
    analyze = runpy.run_path('scripts/phase7_analyze_exploration.py')['analyze']
    t = trial()
    seed = next(s for s in range(100) if random.Random(s).choice(
        ('S0_NATIVE', 'S1_REPEAT_CACHE')) == t.assignment.strategy_id)
    t = t.model_copy(update={'assignment': t.assignment.model_copy(update={'seed': seed})})
    events = [
        {'kind': 'protocol', 'body': {'protocol': 'randomized-cache-state-v1',
            'operator_immutable_guarantee': True, 'epoch': t.experience.context.dataset_epoch,
            'seed': seed, 'background_workers_planned': [0]}},
        {'kind': 'assignment', 'body': t.assignment.model_dump(mode='json')},
        {'kind': 'trial', 'body': t.model_dump(mode='json', round_trip=True)},
        {'kind': 'round_complete', 'body': {'elapsed_s': 1}},
        {'kind': 'summary', 'body': summarize([t])},
    ]
    path = tmp_path/'journal.jsonl'
    def write():
        path.write_text('\n'.join(json.dumps(e) for e in events))
    write()
    with pytest.raises(ValueError, match='planned experiment cells'):
        analyze(path)
    events[0]['body']['seed'] += 1
    write()
    with pytest.raises(ValueError, match='seed differs from protocol'):
        analyze(path)
    events.pop(0)
    write()
    with pytest.raises(ValueError, match='precedes protocol'):
        analyze(path)


def test_legacy_journal_reports_unattested_plan(tmp_path):
    # Reuse a historical single-cell fixture only to exercise the legacy reader.
    import runpy
    analyze = runpy.run_path('scripts/phase7_analyze_exploration.py')['analyze']
    t = trial()
    seed = next(s for s in range(100) if random.Random(s).choice(
        ('S0_NATIVE', 'S1_REPEAT_CACHE')) == t.assignment.strategy_id)
    t = t.model_copy(update={'assignment': t.assignment.model_copy(update={'seed': seed})})
    events = [
        {'kind': 'protocol', 'body': {'operator_immutable_guarantee': True,
            'epoch': t.experience.context.dataset_epoch}},
        {'kind': 'assignment', 'body': t.assignment.model_dump(mode='json')},
        {'kind': 'trial', 'body': t.model_dump(mode='json', round_trip=True)},
        {'kind': 'round_complete', 'body': {'elapsed_s': 1}},
        {'kind': 'summary', 'body': summarize([t])},
    ]
    path = tmp_path/'journal.jsonl'
    path.write_text('\n'.join(json.dumps(e) for e in events))
    report = analyze(path)
    assert report['planned_cell_coverage_verified'] is False
    assert 'PLANNED_EXPERIMENT_COVERAGE_NOT_ATTESTED' in report['remaining_blockers']


def test_load_coverage_requires_valid_samples_in_both_arms_and_phases():
    import runpy
    from types import SimpleNamespace as N
    coverage = runpy.run_path('scripts/phase7_analyze_exploration.py')['qualified_load_coverage']
    def row(bucket, phase, arm, status='VALID'):
        return N(experience=N(sample_status=N(value=status), context=N(load_bucket=bucket)),
                 assignment=N(phase=phase, strategy_id=arm))
    medium = [row('MEDIUM', phase, arm) for phase in ('train', 'holdout')
              for arm in ('S0_NATIVE', 'S1_REPEAT_CACHE') for _ in range(30)]
    bad = [row('HIGH', phase, arm, 'LOW_CONFIDENCE') for phase in ('train', 'holdout')
           for arm in ('S0_NATIVE', 'S1_REPEAT_CACHE') for _ in range(30)]
    report = coverage(medium + bad)
    assert report['qualified_buckets'] == ['MEDIUM']
    assert 'HIGH' not in report['valid_counts']
    sparse = [row('LOW', phase, arm) for phase in ('train', 'holdout')
              for arm in ('S0_NATIVE', 'S1_REPEAT_CACHE') for _ in range(29)]
    assert coverage(medium + sparse)['qualified_buckets'] == ['MEDIUM']
    sparse += [row('LOW', phase, arm) for phase in ('train', 'holdout')
               for arm in ('S0_NATIVE', 'S1_REPEAT_CACHE')]
    assert coverage(medium + sparse)['qualified_buckets'] == ['LOW', 'MEDIUM']
