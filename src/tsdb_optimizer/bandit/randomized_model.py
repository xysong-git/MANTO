"""Offline randomized-trial LinUCB, partitioned by pre-action cache state.

Not a replacement for legacy Experience partitions or production AdaptiveAdvice.
Only genuine observed rewards update arms. Cross-result-source comparisons are
limited to the explicitly randomized, same pre-action cache-state experiment.
"""
import math
import random
from collections import defaultdict

from .context import encode, partition_key
from .exploration import STRATEGIES, Trial, summarize
from .model import OfflineLinUCB
from ..schemas.learning import digest


def trial_partition(t: Trial):
    context=t.experience.context.model_dump(mode='json')
    for key in ('result_source','result_rows','load_bucket'):
        context.pop(key)
    return digest({'protocol':'randomized-cache-state-v1','context':context,
        'cache_state_before':t.assignment.cache_state_before,
        'background_workers':t.assignment.background_workers,
        'resource_policy':t.experience.resource_policy_hash})


def evaluate_randomized(trials, registry):
    """Frozen off-policy evaluation; never submits queries or publishes a policy."""
    summarize(trials)  # uniqueness and train/holdout chronological separation
    streams={}
    for t in trials:
        a=t.assignment
        key=(a.phase,a.cache_state_before,a.background_workers)
        stream=streams.setdefault(key, [random.Random(a.seed),0,a.seed])
        if a.seed!=stream[2] or a.index!=stream[1] or stream[0].choice(STRATEGIES)!=a.strategy_id:
            raise ValueError('randomization transcript mismatch')
        stream[1]+=1
    training=[t for t in trials if t.assignment.phase=='train']
    heldout=[t for t in trials if t.assignment.phase=='holdout']
    if not training or not heldout:
        raise ValueError('both training and independent holdout required')
    # Selection after excluding outcomes biases IPS. Fail rather than silently filter.
    if any(t.experience.sample_status.value!='VALID' for t in trials):
        raise ValueError('IPS evaluation requires all assigned outcomes qualified')
    cutoff=max(t.experience.record.finished_at for t in training)
    arms=[f'{s}@{registry.assert_selectable(s).spec.version}' for s in STRATEGIES]
    models={}
    for t in trials:
        e=t.experience
        # Same full admission check as legacy importer, but do not combine its partitions.
        validator=OfflineLinUCB(arms,baseline_arm=arms[0],partition=partition_key(e),
                               training_until=e.record.finished_at)
        validator.update_experience(e,registry)
    for t in training:
        e=t.experience
        key=trial_partition(t)
        m=models.setdefault(key,OfflineLinUCB(arms,baseline_arm=arms[0],
                             partition=key,training_until=cutoff))
        # Private numerical update only after Trial and real Experience admission above.
        m._update(execution_id=e.execution_id,arm=f'{e.strategy_id}@{e.strategy_version}',
            x=encode(e.features,e.before,at=e.record.started_at),
            latency_ms=e.record.client_wall_ms,finished_at=e.record.finished_at)
    states={k:m.snapshot().model_dump(mode='json',exclude={'model_version','created_at'}) for k,m in models.items()}
    rows=[]
    for t in heldout:
        e=t.experience
        key=trial_partition(t)
        if key not in models: raise ValueError('unseen holdout context')
        m=models[key]
        selected=m.choose_registered(encode(e.features,e.before,at=e.record.started_at),registry)
        observed=f'{e.strategy_id}@{e.strategy_version}'
        reward=-math.log1p(e.record.client_wall_ms/1000.)
        row={'execution_id':e.execution_id,'partition':key,'chosen_arm':selected,
            'observed_arm':observed,'observed_reward':reward,'propensity':t.assignment.propensity,
            'chosen_reward':reward if selected==observed else None,
            'ips_adaptive_contribution':reward/t.assignment.propensity if selected==observed else 0.,
            'ips_native_contribution':reward/t.assignment.propensity if observed==arms[0] else 0.,
            'ips_fixed_s1_contribution':reward/t.assignment.propensity if observed==arms[1] else 0.}
        rows.append(row)
    for key,m in models.items():
        if m.snapshot().model_dump(mode='json',exclude={'model_version','created_at'})!=states[key]:
            raise RuntimeError('holdout modified training model')
    means={name:sum(r[name] for r in rows)/len(rows) for name in
           ('ips_adaptive_contribution','ips_native_contribution','ips_fixed_s1_contribution')}
    return {'protocol':'randomized-cache-state-v1','models':states,'holdout':rows,
        'ips_mean_rewards':means,'higher_reward_is_better':True,
        'model_frozen':True,'online_ready':False,
        'limitations':['offline IPS estimate with known randomized assignment, not executed adaptive policy',
            'one experimental session does not establish reproducible production benefit',
            'partition uses controlled pre-action cache state and requested background workers',
            'background workers are not total DB concurrency; DB host availability must be checked in original source-tagged snapshots']}
