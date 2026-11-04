"""Optional provider contract tests, not evidence of real LLM quality."""
import asyncio
import json
import pytest

from tsdb_optimizer.decision.llm_advisor import SafeLLMAdvisor
from test_orchestrator import lab
from phase4_support import TEST_SQL


def decision(flow):
    return flow.engine.decide(query_id='llm-test',original_sql=TEST_SQL,mode='rule_based',
                              server_version='2.0.6',database_model='tree')


def test_off_never_calls_transport_and_on_has_no_sql(lab):
    flow,*_=lab
    calls=[]
    async def transport(payload):
        calls.append(payload)
        assert 'SELECT' not in json.dumps(payload)
        return '{"strategy_id":"S0_NATIVE"}'
    off=SafeLLMAdvisor(flow.registry,transport=transport)
    assert asyncio.run(off.recommend(decision(flow))).reason=='DISABLED'
    assert not calls
    on=SafeLLMAdvisor(flow.registry,enabled=True,transport=transport,provider_version='TEST_ONLY')
    result=asyncio.run(on.recommend(decision(flow)))
    assert result.advice.strategy_id=='S0_NATIVE' and result.advice.sample_count==0
    assert len(calls)==1


@pytest.mark.parametrize('reply', ['{"strategy_id":"EVIL"}', '{"strategy_id":"S0_NATIVE","sql":"DROP DATABASE x"}',
                                  'not json','{"strategy_id":123}', 'x'*2049])
def test_invalid_or_unknown_reply_falls_back_to_rules(lab, reply):
    flow,*_=lab
    async def transport(payload): return reply
    result=asyncio.run(SafeLLMAdvisor(flow.registry,enabled=True,transport=transport).recommend(decision(flow)))
    assert result.advice is None
    d=flow.engine.decide(query_id='fallback',original_sql=TEST_SQL,mode='adaptive',adaptive_advice=result.advice,
                         server_version='2.0.6',database_model='tree')
    assert d.selected_strategy_id in flow.registry.selectable_ids()


def test_timeout_unavailable_and_provider_exception(lab):
    flow,*_=lab
    async def slow(payload): await asyncio.sleep(.1); return '{}'
    result=asyncio.run(SafeLLMAdvisor(flow.registry,enabled=True,transport=slow,timeout_ms=1).recommend(decision(flow)))
    assert result.reason=='TIMEOUT' and result.advice is None
    assert asyncio.run(SafeLLMAdvisor(flow.registry,enabled=True).recommend(decision(flow))).reason=='UNAVAILABLE'
    async def fail(payload): raise OSError('TEST ONLY provider outage')
    assert asyncio.run(SafeLLMAdvisor(flow.registry,enabled=True,transport=fail).recommend(decision(flow))).advice is None


def test_resource_veto_and_quarantine_are_not_bypassed(lab):
    from test_learning import sample
    flow,*_=lab
    state=sample(); state.client_host.cpu_percent=99
    d=flow.engine.decide(query_id='pressure',original_sql=TEST_SQL,mode='rule_based',system_state=state,
        server_version='2.0.6',database_model='tree')
    async def suggest_vetoed(payload):
        assert 'S1_REPEAT_CACHE' not in payload['eligible_strategy_ids']
        return '{"strategy_id":"S1_REPEAT_CACHE"}'
    r=asyncio.run(SafeLLMAdvisor(flow.registry,enabled=True,transport=suggest_vetoed).recommend(d))
    assert r.advice is None
    d=decision(flow)
    async def quarantine_during_request(payload):
        flow.registry.quarantine_version('S1_REPEAT_CACHE','1.0.0')
        return '{"strategy_id":"S1_REPEAT_CACHE"}'
    r=asyncio.run(SafeLLMAdvisor(flow.registry,enabled=True,transport=quarantine_during_request).recommend(d))
    assert r.advice is None
