"""Independent Phase-7 review. Recorded facts are replayed; no new DB execution."""
import json
from pathlib import Path

import pytest

from tsdb_optimizer.bandit import OfflineLinUCB
from tsdb_optimizer.bandit.context import partition_key
from tsdb_optimizer.bandit.evaluation import audit, frozen_evaluate
from tsdb_optimizer.schemas.learning import Experience, SampleStatus
from tsdb_optimizer.strategies import build_default_registry

ROOT = Path(__file__).resolve().parents[2]


def records():
    train = [Experience.model_validate(e) for e in json.loads((ROOT / 'docs/evidence/phase5_learning_fixes_2026-11-02.json').read_text())['experiences']]
    report = json.loads((ROOT / 'docs/evidence/phase7_offline_2026-11-02.json').read_text())
    heldout = [Experience.model_validate(e) for e in report['independent_benchmark']['raw']['experiences']]
    return train, heldout


def trained():
    train, heldout = records()
    registry = build_default_registry()
    example = next(e for e in train if e.sample_status.value == 'VALID' and e.context.result_source == 'SERVER')
    model = OfflineLinUCB(['S0_NATIVE@0.1.0', 'S1_REPEAT_CACHE@1.0.0'],
        baseline_arm='S0_NATIVE@0.1.0', partition=partition_key(example),
        training_until=max(e.record.finished_at for e in train))
    for e in train:
        try:
            model.update_experience(e, registry)
        except ValueError:
            continue
    valid = [e for e in heldout if e.sample_status.value == 'VALID' and partition_key(e) == model.snapshot().partition]
    return model, registry, valid


def test_frozen_evaluation_rejects_duplicate_execution_ids():
    model, registry, heldout = trained()
    with pytest.raises(ValueError, match='duplicate'):
        frozen_evaluate(model, [heldout[0], heldout[0]], registry)


def test_audit_reports_quarantined_records_without_aborting():
    train, _ = records()
    registry = build_default_registry()
    registry.quarantine_version('S1_REPEAT_CACHE', '1.0.0')
    report = audit(train, registry)
    assert report['online_ready'] is False
    assert all('S1_REPEAT_CACHE@1.0.0' not in p['valid_counts'] for p in report['partitions'])
    assert sum(p['rejected_records'] for p in report['partitions']) >= 12


def test_frozen_evaluation_excludes_quarantined_observed_arm():
    model, registry, heldout = trained()
    registry.quarantine_version('S1_REPEAT_CACHE', '1.0.0')
    alt = [e for e in heldout if e.strategy_id == 'S1_REPEAT_CACHE']
    report = frozen_evaluate(model, alt, registry)
    assert report['test_rows'] == []
    assert report['factual_reward_mse'] is None
    assert report['frozen_model_unchanged'] is True


def test_conflicting_heldout_id_is_rejected_without_mutating_model():
    model, registry, heldout = trained()
    first = heldout[0]
    changed = first.model_copy(update={'quality_reasons': ['TEST ONLY conflicting evidence']})
    before = model._state.model_dump()
    with pytest.raises(ValueError, match='duplicate'):
        frozen_evaluate(model, [first, changed], registry)
    assert model._state.model_dump() == before


def test_audit_exact_duplicates_excluded_and_generator_supported():
    train, _ = records()
    registry = build_default_registry()
    expected = audit(train, registry)
    actual = audit(iter([*train, train[0]]), registry)
    assert actual['unique_execution_count'] == len(train)
    assert actual['duplicates_excluded'] == 1
    assert actual['partitions'] == expected['partitions']
    assert sum(actual['sample_status_counts'].values()) == len(train) + 1


def test_conflicting_audit_identity_is_rejected():
    train, _ = records()
    changed = train[0].model_copy(update={'quality_reasons': ['TEST ONLY conflicting evidence']})
    with pytest.raises(ValueError, match='conflicting duplicate'):
        audit([train[0], changed], build_default_registry())


def test_rejection_reasons_are_visible_and_remaining_facts_evaluated():
    model, registry, heldout = trained()
    before = model._state.model_dump()
    registry.quarantine_version('S1_REPEAT_CACHE', '1.0.0')
    result = frozen_evaluate(model, iter(heldout), registry)
    assert len(result['test_rows']) == 4
    assert result['rejected_records'] == len(result['rejections']) == 4
    assert all(r['reason'] == 'StrategyRegistryError' and 'quarantined' in r['detail']
               for r in result['rejections'])
    assert all(r['observed_arm'] == r['chosen_offline_arm'] == 'S0_NATIVE@0.1.0'
               for r in result['test_rows'])
    assert model._state.model_dump() == before
    train, _ = records()
    report = audit(train, registry)
    assert sum(len(p['rejections']) for p in report['partitions']) == sum(
        p['rejected_records'] for p in report['partitions'])


def test_invalid_partition_and_quality_rejections_are_distinct():
    train, heldout = records()
    model, registry, valid = trained()
    bad = valid[0].model_copy(update={'sample_status': SampleStatus.LOW_CONFIDENCE, 'reward': None})
    # Revalidate the contract: fault injection must not depend on an invalid object.
    bad = Experience.model_validate(bad.model_dump(round_trip=True))
    cache = next(e for e in heldout if e.context.result_source == 'CACHE_HIT')
    result = frozen_evaluate(model, [bad, cache], registry)
    assert result['test_rows'] == []
    assert {r['reason'] for r in result['rejections']} == {'ValueError', 'PARTITION_MISMATCH'}
    assert result['policy_value'] is None and result['frozen_model_unchanged']


def gaussian_solve(a, b):
    """Independent Gaussian elimination, not the production Cholesky solver."""
    rows = [list(row) + [rhs] for row, rhs in zip(a, b)]
    n = len(b)
    for i in range(n):
        pivot = max(range(i, n), key=lambda j: abs(rows[j][i]))
        rows[i], rows[pivot] = rows[pivot], rows[i]
        scale = rows[i][i]
        assert abs(scale) > 1e-15
        rows[i] = [v/scale for v in rows[i]]
        for j in range(n):
            if j != i:
                factor = rows[j][i]
                rows[j] = [v-factor*w for v,w in zip(rows[j], rows[i])]
    return [row[-1] for row in rows]


def test_real_evidence_parameters_and_mse_recomputed_independently():
    import math
    from tsdb_optimizer.bandit.context import DIMENSIONS, encode
    train, _ = records()
    model, registry, heldout = trained()
    state = model.snapshot()
    d = len(DIMENSIONS)
    errors = []
    for arm, actual in state.arms.items():
        a = [[state.config.regularization if i == j else 0. for j in range(d)] for i in range(d)]
        b = [0.]*d
        rows = [e for e in train if e.sample_status.value == 'VALID'
                and partition_key(e) == state.partition and f'{e.strategy_id}@{e.strategy_version}' == arm]
        assert len(rows) == actual.n == 4
        for e in rows:
            x = encode(e.features, e.before, at=e.record.started_at)
            reward = -math.log1p(e.record.client_wall_ms/1000.)
            for i in range(d):
                b[i] += reward*x[i]
                for j in range(d):
                    a[i][j] += x[i]*x[j]
        for row, expected in zip(actual.a, a):
            assert row == pytest.approx(expected)
        assert actual.b == pytest.approx(b)
        theta = gaussian_solve(a, b)
        for e in heldout:
            if f'{e.strategy_id}@{e.strategy_version}' == arm:
                x = encode(e.features, e.before, at=e.record.started_at)
                prediction = sum(v*t for v,t in zip(x, theta))
                assert model.predict(arm, x, ucb=False) == pytest.approx(prediction, abs=1e-14)
                errors.append((prediction-e.reward)**2)
    result = frozen_evaluate(model, heldout, registry)
    recorded = json.loads((ROOT/'docs/evidence/phase7_offline_2026-11-02.json').read_text())
    original = recorded['independent_benchmark']['evaluation'][state.partition]
    assert len(errors) == len(result['test_rows']) == 8
    assert result['factual_reward_mse'] == pytest.approx(sum(errors)/len(errors), rel=1e-10)
    assert result['factual_reward_mse'] == pytest.approx(original['factual_reward_mse'], rel=1e-10)
    assert result['policy_value'] is None
