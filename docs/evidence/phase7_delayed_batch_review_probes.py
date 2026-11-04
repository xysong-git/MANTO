"""Read-only adversarial replay; modifies streamed copies, never original evidence."""
import importlib.util
import json
from pathlib import Path

import pytest

ROOT=Path(__file__).resolve().parents[2]
spec=importlib.util.spec_from_file_location('delayed_batch_audit',ROOT/'scripts/phase7_analyze_delayed_batch.py')
audit=importlib.util.module_from_spec(spec);spec.loader.exec_module(audit)
JOURNAL=ROOT/'data/phase7_delayed_batch_v2_20261103/journal.jsonl'


class MutatedJournal:
    def __init__(self, mutation):self.mutation=mutation;self.changed=False
    def open(self):
        def stream():
            with JOURNAL.open() as source:
                for line in source:
                    e=json.loads(line)
                    if not self.changed:
                        k,b=e['kind'],e['body'];m=self.mutation
                        if m=='seed' and k=='protocol':b['test_seed']+=1;self.changed=True
                        elif m=='future_context' and k=='batch_selected':
                            b['contract']['declared_at']='2099-01-01T00:00:00Z';self.changed=True
                        elif m=='foreign_hit' and k=='request':
                            execution=b['workflow']['run']['execution']['attempts'][-1]['execution']
                            if execution['served_from_cache']:
                                execution['cache_source_execution_id']='FOREIGN';self.changed=True
                        elif m=='reward' and k=='batch_closed':b['reward']+=1;self.changed=True
                        elif m=='update' and k=='feedback_applied':b['revision']+=1;self.changed=True
                        elif m=='freeze' and k=='feedback_enqueued':
                            e={'kind':'frozen_model','body':{'revision':0}};self.changed=True
                        elif m=='holdout_training' and k=='batch_assignment':
                            b['phase']='HOLDOUT';self.changed=True
                        elif m=='completion' and k=='batch_closed':
                            e={'kind':'complete','body':{'training_batches':240,'heldout_batches':360,
                                'online_ready':False,'stage_accepted':False}};self.changed=True
                    yield json.dumps(e)+'\n'
        return stream()
    def read_bytes(self):return JOURNAL.read_bytes()


@pytest.mark.parametrize('mutation',['seed','future_context','foreign_hit','reward','update','freeze','holdout_training','completion'])
def test_tampered_evidence_rejected(mutation):
    journal=MutatedJournal(mutation)
    with pytest.raises(ValueError):audit.analyze(journal)
    assert journal.changed, 'must reach intended mutation, not an unrelated earlier failure'


def test_real_aborted_study_cannot_produce_benefit_report():
    with pytest.raises(ValueError,match='aborted or unknown journal event'):
        audit.analyze(JOURNAL)
