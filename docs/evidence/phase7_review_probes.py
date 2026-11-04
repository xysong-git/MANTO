"""Independent Phase-7 review. Recorded facts are replayed; no new DB execution."""
import json
from pathlib import Path

import pytest

from tsdb_optimizer.bandit import OfflineLinUCB
from tsdb_optimizer.bandit.context import partition_key
from tsdb_optimizer.bandit.evaluation import audit, frozen_evaluate
from tsdb_optimizer.schemas.learning import Experience
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


@pytest.mark.parametrize('field,value', [
    ('fetch_mode', 'LIMITED'), ('dataset_epoch', 'UNKNOWN'),
    ('server_id', 'unknown'), ('cache_condition', 'UNKNOWN'),
])
def test_import_rechecks_context_without_mutating_parameters(field, value):
    from tsdb_optimizer.schemas.learning import digest
    model, registry, heldout = trained()
    body = heldout[0].model_dump(round_trip=True)
    body['context'][field] = value
    row = Experience.model_validate(body)
    target = OfflineLinUCB(model.snapshot().arms, baseline_arm=model.snapshot().baseline_arm,
        partition=partition_key(row), training_until=row.record.finished_at)
    before = target.snapshot().model_dump(mode='json', exclude={'model_version', 'created_at'})
    with pytest.raises(ValueError):
        target.update_experience(row, registry)
    assert digest(before) == digest(target.snapshot().model_dump(mode='json', exclude={'model_version', 'created_at'}))


@pytest.mark.parametrize('field,value', [
    ('quality_reasons', ['INCOMPLETE_RESULT']), ('certification_suite', 'wrong-suite'),
    ('resource_policy_version', 'wrong-version'),
])
def test_import_rechecks_quality_proof(field, value):
    model, registry, heldout = trained()
    body = heldout[0].model_dump(round_trip=True)
    body[field] = value
    row = Experience.model_validate(body)
    target = OfflineLinUCB(model.snapshot().arms, baseline_arm=model.snapshot().baseline_arm,
        partition=partition_key(row), training_until=row.record.finished_at)
    with pytest.raises(ValueError):
        target.update_experience(row, registry)
    assert not target.snapshot().observations


def test_rejected_holdout_reports_reason_and_counts():
    model, registry, heldout = trained()
    body = heldout[0].model_dump(round_trip=True)
    body.update(sample_status='LOW_CONFIDENCE', reward=None, quality_reasons=['REVIEW_TEST'])
    result = frozen_evaluate(model, [Experience.model_validate(body)], registry)
    assert result['input_records'] == result['unique_execution_count'] == 1
    assert result['accepted_records'] == 0
    assert result['rejected_records'] == 1
    assert result['rejections'][0]['execution_id'] == body['execution_id']
    assert result['rejections'][0]['reason_code'] == 'SAMPLE_NOT_VALID'


def test_conflicting_holdout_replay_is_rejected():
    model, registry, heldout = trained()
    conflict = heldout[0].model_copy(update={'certification_suite': 'conflict'})
    with pytest.raises(ValueError, match='duplicate'):
        frozen_evaluate(model, [heldout[0], conflict], registry)


@pytest.mark.parametrize('failure', [ValueError('internal bug'), RuntimeError('internal bug')])
def test_internal_errors_propagate_from_evaluation(monkeypatch, failure):
    model, registry, heldout = trained()
    def broken(*args, **kwargs):
        raise failure
    monkeypatch.setattr(OfflineLinUCB, 'update_experience', broken)
    with pytest.raises(type(failure), match='internal bug'):
        frozen_evaluate(model, heldout, registry)
