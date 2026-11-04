"""Readiness and frozen, factual-only evaluation; never IPS without propensities."""
from collections import Counter, defaultdict
import math

from ..errors import StrategyRegistryError
from ..schemas.learning import digest
from .context import encode, partition_key
from .model import AdmissionError, Config, OfflineLinUCB


def audit(experiences, registry, *, config=None):
    config = config or Config()
    experiences = list(experiences)
    groups = defaultdict(list)
    seen = {}
    duplicates = 0
    for e in experiences:
        fingerprint = digest(e.model_dump(mode='json'))
        if e.execution_id in seen:
            if seen[e.execution_id] != fingerprint:
                raise ValueError('conflicting duplicate execution evidence in audit')
            duplicates += 1
            continue
        seen[e.execution_id] = fingerprint
        groups[partition_key(e)].append(e)
    summaries = []
    for key, rows in sorted(groups.items()):
        arms = sorted({f'{e.strategy_id}@{e.strategy_version}' for e in rows})
        baseline = 'S0_NATIVE@'+registry.get_enabled('S0_NATIVE').spec.version
        accepted, rejections = [], []
        if baseline in arms and len(arms) >= 2:
            model = OfflineLinUCB(arms, baseline_arm=baseline, partition=key,
                training_until=max(e.record.finished_at for e in rows), config=config)
            for e in rows:
                try:
                    model.update_experience(e,registry)
                    accepted.append(e)
                except (AdmissionError, StrategyRegistryError) as exc:
                    rejections.append({'execution_id': e.execution_id,
                        'reason': 'ValueError' if isinstance(exc, AdmissionError) else type(exc).__name__,
                'reason_code': exc.reason if isinstance(exc, AdmissionError) else 'STRATEGY_NOT_SELECTABLE',
                'detail': str(exc)})
        else:
            rejections = [{'execution_id': e.execution_id,
                'reason': 'NO_COMPARABLE_BASELINE_AND_ALTERNATIVE'} for e in rows]
        counts = Counter(f'{e.strategy_id}@{e.strategy_version}' for e in accepted)
        loads = sorted({e.context.load_bucket for e in accepted})
        blockers = []
        if baseline not in arms or len(arms) < 2: blockers.append('NO_COMPARABLE_BASELINE_AND_ALTERNATIVE')
        if len(counts) < 2 or any(counts[a] < config.min_samples for a in arms): blockers.append('INSUFFICIENT_PER_ARM_SAMPLES')
        if len(loads) < 2: blockers.append('INSUFFICIENT_LOAD_COVERAGE')
        # The current Experience schema does not attest assignment probabilities.
        blockers.extend(['NO_VERIFIED_PROPENSITY_LOG', 'INDEPENDENT_POLICY_BENEFIT_NOT_ESTABLISHED'])
        summaries.append({'partition': key, 'arms': arms, 'valid_counts': dict(counts),
            'rejected_records': len(rejections), 'rejections': rejections,
            'loads': loads, 'blockers': blockers})
    return {'online_ready': False, 'configured_min_samples': config.min_samples,
        'unique_execution_count': len(seen), 'duplicates_excluded': duplicates,
        'sample_status_counts': dict(Counter(e.sample_status.value for e in experiences)),
        'partitions': summaries, 'deployment': 'OFFLINE_ONLY',
        'propensities': 'UNKNOWN_NOT_PRESENT_IN_EXPERIENCE_SCHEMA'}


def frozen_evaluate(model, experiences, registry):
    experiences = list(experiences)
    ids = [e.execution_id for e in experiences]
    if len(set(ids)) != len(ids):
        raise ValueError('duplicate execution IDs in held-out evaluation')
    snapshot = model.snapshot()
    before = digest(snapshot.model_dump(mode='json',exclude={'model_version','created_at'}))
    rows, rejections = [], []
    for e in experiences:
        if e.execution_id in snapshot.observations or e.record.started_at <= snapshot.training_until:
            raise ValueError('training/test identities or time windows overlap')
        if partition_key(e) != snapshot.partition:
            rejections.append({'execution_id': e.execution_id, 'reason': 'PARTITION_MISMATCH'})
            continue
        # Reuse admission validation in a throwaway model, never update the frozen model.
        checker = OfflineLinUCB(snapshot.arms, baseline_arm=snapshot.baseline_arm,
            partition=snapshot.partition, training_until=e.record.finished_at, config=snapshot.config)
        try:
            checker.update_experience(e,registry)
        except (AdmissionError, StrategyRegistryError) as exc:
            rejections.append({'execution_id': e.execution_id,
                'reason': 'ValueError' if isinstance(exc, AdmissionError) else type(exc).__name__,
                'reason_code': exc.reason if isinstance(exc, AdmissionError) else 'STRATEGY_NOT_SELECTABLE',
                'detail': str(exc)})
            continue
        arm = f'{e.strategy_id}@{e.strategy_version}'
        x = encode(e.features,e.before,at=e.record.started_at)
        actual = -math.log1p(e.record.client_wall_ms/1000.)
        prediction = model.predict(arm,x,ucb=False)
        choice = model.choose_registered(x,registry)
        rows.append({'execution_id': e.execution_id, 'observed_arm': arm, 'predicted_reward': prediction,
            'observed_reward': actual, 'chosen_offline_arm': choice,
            'chosen_reward': actual if choice == arm else None})
    after = model.snapshot()
    if digest(after.model_dump(mode='json',exclude={'model_version','created_at'})) != before:
        raise RuntimeError('frozen model changed during evaluation')
    return {'test_rows': rows, 'input_records': len(experiences),
        'unique_execution_count': len(ids), 'accepted_records': len(rows),
        'rejected_records': len(rejections), 'rejections': rejections,
        'factual_reward_mse': (
        sum((r['observed_reward']-r['predicted_reward'])**2 for r in rows)/len(rows) if rows else None),
        'policy_value': None, 'policy_value_reason': 'UNKNOWN_NO_VERIFIED_PROPENSITIES; unmatched rewards remain null',
        'frozen_model_unchanged': True, 'online_ready': False}
