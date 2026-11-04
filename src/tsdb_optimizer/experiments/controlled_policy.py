"""Explicit experiment-only frozen policy. Never publishes production advice."""
from ..bandit.context import encode, partition_key
from ..bandit.model import OfflineLinUCB, Config
from ..bandit.completed_request import E2E_REWARD_VERSION
from ..schemas.learning import digest

ARMS=['S0_NATIVE@0.1.0','S1_REPEAT_CACHE@1.0.0']


def train(rows, registry, *, completed=None):
    if not rows:raise ValueError('empty training set')
    identities=set();ids=set()
    for cache,e in rows:
        if cache not in ('EMPTY','READY') or e.context.cache_condition!='controlled-'+cache.lower():
            raise ValueError('cache condition mismatch')
        if e.context.load_protocol!='cpu-activity-v3':raise ValueError('CPU v3 protocol required')
        identity=e.context.model_dump(mode='json')
        for key in ('load_bucket','result_rows','result_source','cache_condition'):identity.pop(key)
        identities.add(digest([identity,e.query_hash,e.resource_policy_hash]))
        if e.execution_id in ids:raise ValueError('duplicate training execution')
        ids.add(e.execution_id)
    if len(identities)!=1:raise ValueError('mixed training environments or queries')
    if completed is not None and set(completed) != ids:
        raise ValueError('completed receipt IDs differ from training')
    cutoff=max(completed[e.execution_id][0].finished_at if completed is not None else e.record.finished_at for cache,e in rows)
    models={}
    for cache,e in rows:
        # Full admission before numerical update: no errors, mismatches, missing
        # identities, resource violations or uncertified strategies can train.
        check=OfflineLinUCB(ARMS,baseline_arm=ARMS[0],partition=partition_key(e),training_until=cutoff)
        check.update_experience(e,registry)
        receipt = None
        if completed is not None:
            receipt, workflow = completed[e.execution_id]
            check = OfflineLinUCB(ARMS, baseline_arm=ARMS[0],
                partition='completed-request-v1:'+partition_key(e), training_until=cutoff,
                config=Config(reward_version=E2E_REWARD_VERSION))
            check.update_completed_request(receipt,e,workflow,registry)
        model=models.setdefault(cache,OfflineLinUCB(ARMS,baseline_arm=ARMS[0],
            partition=('controlled-e2e-v6:' if completed is not None else 'controlled-e2e-v2:')+cache,
            config=Config(reward_version=E2E_REWARD_VERSION) if completed is not None else None,
            training_until=cutoff))
        model._update(execution_id=e.execution_id,arm=e.strategy_id+'@'+e.strategy_version,
            x=encode(e.features,e.before,at=e.record.started_at),latency_ms=receipt.end_to_end_ms if receipt is not None else e.record.client_wall_ms,
            finished_at=receipt.finished_at if receipt is not None else e.record.finished_at)
    return models


def fingerprint(models):
    return digest({k:v.fingerprint_state() for k,v in models.items()})
