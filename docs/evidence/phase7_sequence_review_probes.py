"""Read-only tamper probes against the completed sequence journal, not DB trials."""
import copy
import json
from pathlib import Path
import sys

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2]/'scripts'))
import phase7_sequence_e2e as study

JOURNAL = Path('data/phase7_sequence_fresh_20261103/journal.jsonl')
COMPACT_JOURNAL = Path('data/phase7_sequence_compact_20261103/journal.jsonl')


@pytest.fixture(scope='module', params=[JOURNAL, COMPACT_JOURNAL])
def evidence(request):
    path = request.param
    events = [json.loads(line) for line in path.open()]
    protocol = events[2]['body']
    source = study.source_models(Path(protocol['training_path']))
    return events, source, path


def test_complete_real_sequence_replays(evidence, monkeypatch):
    _, source, path = evidence
    monkeypatch.setattr(study, 'source_models', lambda path: source)
    report = study.analyze(path)
    assert report['requests'] == 990 and report['sequences'] == 270
    assert report['sequence_accounting_complete']
    assert not report['stage_accepted'] and not report['online_ready']


@pytest.mark.parametrize('mutation', ['missing_first', 'timer', 'cache_origin', 'after_terminal',
    'ttl', 'model', 'omit_sequence', 'admission', 'query_line'])
def test_tampering_is_rejected(evidence, monkeypatch, tmp_path, mutation):
    events, source, _ = evidence
    events = copy.deepcopy(events)
    monkeypatch.setattr(study, 'source_models', lambda path: source)
    requests = [e for e in events if e['kind'] == 'request']
    if mutation == 'missing_first': events.remove(requests[0])
    elif mutation == 'timer': requests[0]['body']['elapsed_ns'] = 1
    elif mutation == 'cache_origin':
        hit = next(e for e in requests if e['body']['workflow']['run']['execution']['attempts'][-1]['execution']['served_from_cache'])
        hit['body']['workflow']['run']['execution']['attempts'][-1]['execution']['cache_source_execution_id'] = 'FOREIGN_SEQUENCE'
    elif mutation == 'after_terminal': events.append(events[-1])
    elif mutation == 'ttl': events[2]['body']['snapshot_ttl_ms'] = 5000
    elif mutation == 'model': events[2]['body']['model_fingerprint'] = 'wrong'
    elif mutation == 'admission': requests[0]['body']['admission_sample'] = None
    elif mutation == 'query_line': events[2]['body']['query_line'] = 7
    elif mutation == 'omit_sequence':
        begin = next(i for i,e in enumerate(events[3:], 3) if e['kind'] == 'round_start')
        end = next(i for i,e in enumerate(events[begin:], begin) if e['kind'] == 'round_complete')
        del events[begin:end+1]
    path = tmp_path/'tampered.jsonl'
    path.write_text(''.join(json.dumps(e)+'\n' for e in events))
    with pytest.raises(ValueError): study.analyze(path)
