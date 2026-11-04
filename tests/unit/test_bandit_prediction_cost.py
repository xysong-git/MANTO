from tsdb_optimizer.bandit import model as module
from tsdb_optimizer.bandit.context import DIMENSIONS
from tsdb_optimizer.schemas.common import utc_now


def test_greedy_prediction_does_not_compute_unused_uncertainty(monkeypatch):
    model=module.OfflineLinUCB(['a','b'],baseline_arm='a',partition='test',training_until=utc_now())
    x=[1.]+[0.]*(len(DIMENSIONS)-1)
    solve=module.solve;calls=[]
    def counted(a,b):calls.append(1);return solve(a,b)
    monkeypatch.setattr(module,'solve',counted)
    assert model.predict('a',x,ucb=False)==0
    assert len(calls)==1
    assert model.predict('a',x,ucb=True)==.1
    assert len(calls)==3


def test_canonical_fingerprint_matches_snapshot_without_allocating_snapshot(monkeypatch):
    from tsdb_optimizer.experiments.controlled_policy import fingerprint
    from tsdb_optimizer.schemas.learning import digest
    model=module.OfflineLinUCB(['a','b'],baseline_arm='a',partition='test',training_until=utc_now())
    expected=digest({'test':model.snapshot().model_dump(mode='json',exclude={'model_version','created_at'})})
    def forbidden():raise AssertionError('fingerprinting must not allocate a snapshot')
    monkeypatch.setattr(model,'snapshot',forbidden)
    assert fingerprint({'test':model})==expected
    detached=model.fingerprint_state()
    detached['arms']['a']['b'][0]=999
    assert fingerprint({'test':model})==expected
