import json
import pytest
from tsdb_optimizer.learning.repository import _statistics_fact, _cached_fact
from test_exploration import trial


def test_statistics_cache_validates_full_body_and_invalidates_by_content():
    e=trial().experience;body=e.model_dump_json(round_trip=True)
    _cached_fact.cache_clear()
    a=_statistics_fact(body);assert a==_statistics_fact(body)
    assert _cached_fact.cache_info().hits==1
    changed=json.loads(body);changed['sample_status']='INVALID';changed['reward']=None
    b=_statistics_fact(json.dumps(changed))
    assert b.sample_status.value=='INVALID' and b.reward is None
    changed['record']['execution_id']='forged'
    with pytest.raises(ValueError):_statistics_fact(json.dumps(changed))
    with pytest.raises(AttributeError):a.reward=100


def test_sequential_scan_above_old_capacity_reuses_validated_compact_facts(monkeypatch):
    from tsdb_optimizer.learning import repository as module
    cache = module._FactCache()
    body = trial().experience.model_dump_json(round_trip=True)
    # Whitespace gives distinct content keys without inventing execution evidence.
    bodies = [body+' '*i for i in range(160)]
    original = module._read_fact
    calls = []
    def read(value):
        calls.append(1)
        return original(value)
    monkeypatch.setattr(module, '_read_fact', read)
    first = [cache(value) for value in bodies]
    assert [cache(value) for value in bodies] == first
    assert len(calls) == 160 and cache.cache_info().hits == 160
    assert all(isinstance(key, bytes) and len(key) == 32 for key in cache._entries)
    assert 0 < cache.retained_bytes <= cache.max_bytes


def test_fact_cache_has_count_and_byte_bounds_and_does_not_cache_errors():
    from tsdb_optimizer.learning.repository import _FactCache
    body = trial().experience.model_dump_json(round_trip=True)
    cache = _FactCache(maxsize=2)
    for i in range(3): cache(body+' '*i)
    assert cache.cache_info().currsize == 2
    tiny = _FactCache(max_bytes=1)
    assert tiny(body) == cache(body)
    assert tiny.cache_info().currsize == tiny.retained_bytes == 0
    for _ in range(2):
        with pytest.raises(ValueError): cache('{invalid')
    assert cache.cache_info().currsize == 2
    cache.cache_clear()
    assert cache.cache_info().currsize == cache.retained_bytes == 0
