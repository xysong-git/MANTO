"""Protocol tests use synthetic snapshots, never claim measured load."""
from datetime import timedelta
import runpy
import pytest
from pydantic import ValidationError
from tsdb_optimizer.schemas.common import utc_now
from tsdb_optimizer.schemas.learning import LearningContext, digest, load_identity
from tsdb_optimizer.schemas.monitoring import SystemSample, SystemSnapshot
from test_exploration import trial


def sample(cpu=10, memory=64, disk=21):
    t=utc_now()
    snapshot=SystemSnapshot(source_host='test-db',source_type='db_host',captured_at=t,
        cpu_percent=cpu,memory_percent=memory,disk_usage_percent=disk,valid=True,
        disk_read_bytes_per_sec=0,disk_write_bytes_per_sec=0,active_queries=0)
    return trial().experience.before.model_copy(update={'db_host':snapshot}), t


def test_cpu_protocol_separates_activity_from_capacity_without_changing_legacy():
    s,t=sample()
    assert load_identity(s,'db_host',t)==('test-db','MEDIUM')
    assert load_identity(s,'db_host',t,protocol='cpu-activity-v2')==('test-db','LOW')
    for cpu,bucket in [(49.9,'LOW'),(50,'MEDIUM'),(90,'MEDIUM'),(90.1,'HIGH')]:
        s,t=sample(cpu)
        assert load_identity(s,'db_host',t,protocol='cpu-activity-v2')[1]==bucket
    assert load_identity(s,'db_host',t+timedelta(seconds=60),protocol='cpu-activity-v2')[1]=='UNKNOWN'
    assert load_identity(s,'client_host',t,protocol='cpu-activity-v2')[1]=='UNKNOWN'
    with pytest.raises(ValueError,match='protocol'):
        load_identity(s,'db_host',t,protocol='bogus')


def test_legacy_context_json_and_key_are_preserved_v2_isolated():
    old=trial().experience.context
    raw=old.model_dump(mode='json')
    assert 'load_protocol' not in raw
    assert old.key==digest(raw)
    new=old.model_copy(update={'load_protocol':'cpu-activity-v2'})
    assert new.key!=old.key
    assert new.model_dump()['load_protocol']=='cpu-activity-v2'
    assert LearningContext.model_validate(raw).load_protocol=='resource-max-v1'
    with pytest.raises(ValidationError):
        LearningContext.model_validate({**raw,'load_protocol':'bogus'})


def test_profile_observation_does_not_invent_memory_pressure_or_io_saturation():
    observe=runpy.run_path('scripts/phase7_profile_load.py')['observation']
    s,_=sample()
    result=observe(s.db_host,1.2)
    assert result.cpu_bucket=='LOW'
    assert result.memory_pressure==result.disk_saturation=='UNKNOWN'
    assert result.db_host.memory_percent==64
    assert result.db_host.disk_usage_percent==21


def test_v2_does_not_disable_resource_safety():
    from tsdb_optimizer.decision.resource_policy import assess_resource_pressure, load_default_resource_policy
    s,t=sample(memory=95)
    assert load_identity(s,'db_host',t,protocol='cpu-activity-v2')[1]=='LOW'
    assert assess_resource_pressure(s,load_default_resource_policy(),now=t).breached


def test_v2_partition_and_admission_do_not_mix_legacy_models():
    from tsdb_optimizer.bandit.context import partition_key
    from tsdb_optimizer.bandit.model import OfflineLinUCB
    from tsdb_optimizer.strategies import build_default_registry
    old=trial().experience
    new=old.model_copy(update={'context':old.context.model_copy(update={'load_protocol':'cpu-activity-v2'})})
    assert partition_key(old)!=partition_key(new)
    registry=build_default_registry()
    arms=['S0_NATIVE@0.1.0','S1_REPEAT_CACHE@1.0.0']
    model=OfflineLinUCB(arms,baseline_arm=arms[0],partition=partition_key(new),
                       training_until=new.record.finished_at)
    assert model.update_experience(new,registry)
    with pytest.raises(ValueError): model.update_experience(old,registry)
    assert sum(a.n for a in model.snapshot().arms.values())==1


def test_profile_json_roundtrip_and_tampered_expiry_rejected(tmp_path):
    import json
    observe=runpy.run_path('scripts/phase7_profile_load.py')['observation']
    summarize=runpy.run_path('scripts/phase7_summarize_load_profile.py')['summarize']
    s,_=sample()
    obs=observe(s.db_host,1)
    from profile_test_support import journal_events
    events=journal_events(obs)
    path=tmp_path/'journal.jsonl'
    def write():path.write_text('\n'.join(json.dumps(e) for e in events))
    write();assert summarize(path)['rounds'][0]['cpu_max_percent']==10
    events[2]['body']['db_host']['valid_until']=s.db_host.valid_until.isoformat()
    write();assert summarize(path)['schedule_complete']
    events[2]['body']['db_host']['valid_until']=utc_now().isoformat()
    write()
    with pytest.raises(ValueError,match='expiry mismatch'): summarize(path)


def test_exploration_refuses_protocol_mixed_with_legacy_trials(tmp_path):
    import json,random
    from tsdb_optimizer.bandit.exploration import summarize
    analyze=runpy.run_path('scripts/phase7_analyze_exploration.py')['analyze']
    t=trial()
    seed=next(s for s in range(100) if random.Random(s).choice(('S0_NATIVE','S1_REPEAT_CACHE'))==t.assignment.strategy_id)
    t=t.model_copy(update={'assignment':t.assignment.model_copy(update={'seed':seed})})
    events=[{'kind':'protocol','body':{'operator_immutable_guarantee':True,
             'epoch':t.experience.context.dataset_epoch,'load_protocol':'cpu-activity-v2'}},
            {'kind':'assignment','body':t.assignment.model_dump(mode='json')},
            {'kind':'trial','body':t.model_dump(mode='json',round_trip=True)},
            {'kind':'summary','body':summarize([t])}]
    path=tmp_path/'journal.jsonl';path.write_text('\n'.join(json.dumps(e) for e in events))
    with pytest.raises(ValueError,match='load protocol mismatch'): analyze(path)


def test_v3_requires_instance_counters_and_isolates_context():
    s,t=sample(cpu=60)
    assert load_identity(s,'db_host',t,protocol='cpu-activity-v3')[1]=='UNKNOWN'
    s=s.model_copy(update={'db_host':s.db_host.model_copy(update={'cpu_measurement':'cpu-times-instance-v1'})})
    assert load_identity(s,'db_host',t,protocol='cpu-activity-v3')[1]=='MEDIUM'
    old=trial().experience.context.model_copy(update={'load_protocol':'cpu-activity-v2'})
    assert old.key!=old.model_copy(update={'load_protocol':'cpu-activity-v3'}).key
