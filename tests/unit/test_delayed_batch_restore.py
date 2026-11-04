"""Offline recovery failure injection; no IoTDB or performance claims."""
import json
import sqlite3

import pytest

from test_delayed_batch import feedback, model, registry
from tsdb_optimizer.bandit.delayed_store import DelayedBatchStore
from tsdb_optimizer.bandit.episodes import BatchPolicy
from tsdb_optimizer.schemas.learning import digest


@pytest.fixture
def frozen(tmp_path, feedback):
    queue = DelayedBatchStore(tmp_path/'queue.sqlite', initial=model(feedback))
    initial = queue.current()[0]
    queue.enqueue(feedback)
    queue.drain_one(registry(feedback))
    queue.freeze()
    return queue, initial


def training_tables(queue):
    with queue.connection() as c:
        return {name: c.execute(f'SELECT * FROM {name} ORDER BY 1').fetchall()
                for name in ('head', 'versions', 'feedback', 'feedback_ids')}


def test_restore_predictions_survive_restart_without_rewinding_training(frozen, feedback):
    queue, initial = frozen
    before = training_tables(queue)
    current, selected, generation, training = queue.inference_current()
    assert (selected, generation, training) == (1, 0, 1)
    arm = feedback.allocated_strategy+'@'+registry(feedback).assert_selectable(feedback.allocated_strategy).spec.version
    length = str(feedback.contract.size)
    assert current.models[length].predict(arm, feedback.vector, ucb=False) < 0
    receipt = queue.restore_inference(0, expected_training_revision=1, expected_generation=0)
    assert (receipt.previous_revision, receipt.selected_revision, receipt.generation) == (1, 0, 1)
    assert receipt.fingerprint == initial.fingerprint
    restarted = DelayedBatchStore(queue.path)
    restored, selected, generation, training = restarted.inference_current()
    assert (selected, generation, training) == (0, 1, 1)
    assert restored.fingerprint == initial.fingerprint
    assert restored.models[length].predict(arm, feedback.vector, ucb=False) == 0
    assert restarted.current()[0].fingerprint == current.fingerprint
    assert restarted.freeze()['fingerprint'] == current.fingerprint
    assert restarted.drain_one(registry(feedback)) is None
    with pytest.raises(ValueError, match='frozen'):
        restarted.enqueue(feedback)
    assert training_tables(restarted) == before
    restarted.restore_inference(1, expected_training_revision=1, expected_generation=1)
    assert restarted.inference_current()[0].fingerprint == current.fingerprint
    assert training_tables(restarted) == before


@pytest.mark.parametrize('training,generation', [(0, 0), (1, 0)])
def test_stale_caller_cannot_override_new_selection(frozen, training, generation):
    queue, _ = frozen
    queue.restore_inference(0, expected_training_revision=1, expected_generation=0)
    with pytest.raises(ValueError, match='stale'):
        queue.restore_inference(1, expected_training_revision=training, expected_generation=generation)
    assert queue.inference_current()[1:3] == (0, 1)


def test_unfrozen_or_pending_store_refuses_restore(tmp_path, feedback, frozen):
    queue = DelayedBatchStore(tmp_path/'unfrozen.sqlite', initial=model(feedback))
    with pytest.raises(ValueError, match='frozen'):
        queue.restore_inference(0, expected_training_revision=0, expected_generation=0)
    queue, _ = frozen
    # Fault injection: a frozen store must not have pending training work.
    with queue.connection() as c:
        c.execute("UPDATE feedback SET status='PENDING'")
    with pytest.raises(ValueError, match='pending'):
        queue.restore_inference(0, expected_training_revision=1, expected_generation=0)


@pytest.mark.parametrize('fault', ['checksum', 'scope', 'config', 'reward', 'missing'])
def test_invalid_historical_target_is_rejected(frozen, feedback, fault):
    queue, initial = frozen
    body = initial.snapshot()
    if fault == 'scope':
        body = BatchPolicy(deadline=initial.models['2'].snapshot().training_until,
            family_key=initial.scope['family_key'], server_id='OTHER_SERVER',
            epoch=initial.scope['epoch']).snapshot()
    elif fault == 'config':
        body['models']['2']['config']['alpha'] = .2
    elif fault == 'reward':
        body['models']['2']['config']['reward_version'] = 'negative-log1p-ms-v1'
    with queue.connection() as c:
        if fault == 'missing':
            c.execute('DELETE FROM versions WHERE revision=0')
        else:
            c.execute('UPDATE versions SET body=?,checksum=? WHERE revision=0',
                      (json.dumps(body), 'CORRUPT' if fault == 'checksum' else digest(body)))
    with pytest.raises(ValueError):
        queue.restore_inference(0, expected_training_revision=1, expected_generation=0)
    assert queue.inference_current()[1:] == (1, 0, 1)
    with queue.connection() as c:
        assert c.execute('SELECT count(*) FROM inference_restores').fetchone()[0] == 0


def test_pointer_write_failure_rolls_back_audit_and_selection(frozen):
    queue, _ = frozen
    before = training_tables(queue)
    with queue.connection() as c:
        c.execute("CREATE TRIGGER fail_pointer BEFORE INSERT ON inference_head "
                  "BEGIN SELECT RAISE(ABORT, 'TEST_ONLY pointer failure'); END")
    with pytest.raises(sqlite3.IntegrityError, match='TEST_ONLY'):
        queue.restore_inference(0, expected_training_revision=1, expected_generation=0)
    assert queue.inference_current()[1:] == (1, 0, 1)
    assert training_tables(queue) == before
    with queue.connection() as c:
        assert c.execute('SELECT count(*) FROM inference_restores').fetchone()[0] == 0


def test_corrupt_restore_audit_fails_closed(frozen):
    queue, _ = frozen
    queue.restore_inference(0, expected_training_revision=1, expected_generation=0)
    with queue.connection() as c:
        c.execute("UPDATE inference_restores SET checksum='CORRUPT'")
    with pytest.raises(ValueError, match='audit checksum'):
        queue.inference_current()
    assert queue.current()[1] == 1


@pytest.mark.parametrize('revision', [-1, True, 0.0, 2])
def test_invalid_or_future_revision_cannot_be_selected(frozen, revision):
    queue, _ = frozen
    with pytest.raises(ValueError):
        queue.restore_inference(revision, expected_training_revision=1, expected_generation=0)
    assert queue.inference_current()[1:] == (1, 0, 1)
