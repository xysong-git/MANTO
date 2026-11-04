"""Real process deadline tests with explicit synthetic workers; no IoTDB calls."""
import json
import os
from pathlib import Path
import threading
import time
from types import SimpleNamespace as N
import pytest
from tsdb_optimizer.experiments.supervisor import supervise, ExperimentDeadlineError


def blocked(args, *, _signal):
    args.output.mkdir()
    (args.output/'journal.jsonl').write_text('{"kind":"protocol","body":{"TEST_ONLY":true}}\n')
    (args.output/'pid').write_text(str(os.getpid()))
    if args.stage!='connect': _signal('round_start')
    # connect blocks before a round; fetch and close block within one.
    thread=threading.Thread(target=lambda:threading.Event().wait())
    thread.start();thread.join()
    (args.output/'must_not_dispatch').touch()


def completes(args, *, _signal):
    args.output.mkdir()
    for _ in range(3):
        _signal('round_start');time.sleep(.15);_signal('round_complete')
    (args.output/'journal.jsonl').write_text('{"kind":"complete","body":{"TEST_ONLY":true}}\n')


def raises(args, *, _signal):
    args.output.mkdir()
    (args.output/'journal.jsonl').write_text('{"kind":"aborted","body":{"error_type":"ValueError"}}\n')
    raise ValueError('TEST_ONLY_CHILD_FAILURE')


@pytest.mark.parametrize('stage',['connect','fetch','close'])
def test_blocking_worker_is_killed_without_waiting_for_thread_shutdown(tmp_path,stage):
    args=N(output=tmp_path/stage,stage=stage)
    begin=time.monotonic()
    with pytest.raises(ExperimentDeadlineError):supervise(blocked,args,timeout_s=.8,cleanup_s=.2)
    assert time.monotonic()-begin<2
    assert not (args.output/'must_not_dispatch').exists()
    pid=int((args.output/'pid').read_text())
    with pytest.raises(ProcessLookupError):os.kill(pid,0)
    end=json.loads((args.output/'journal.jsonl').read_text().splitlines()[-1])
    assert end['kind']=='aborted' and end['body']['server_cancellation']=='UNKNOWN'
    assert end['body']['retry_allowed'] is False


def test_each_round_has_own_deadline_and_success_does_not_abort(tmp_path):
    args=N(output=tmp_path/'success')
    supervise(completes,args,timeout_s=.5,cleanup_s=.2)
    assert json.loads((args.output/'journal.jsonl').read_text())['kind']=='complete'


def test_child_error_propagates_and_preserves_existing_abort(tmp_path):
    args=N(output=tmp_path/'error')
    with pytest.raises(RuntimeError,match='child failed'):supervise(raises,args,timeout_s=2,cleanup_s=.2)
    assert len((args.output/'journal.jsonl').read_text().splitlines())==1


def test_existing_journal_never_modified(tmp_path):
    args=N(output=tmp_path)
    with pytest.raises(FileExistsError):supervise(completes,args)
    assert list(tmp_path.iterdir())==[]


def blocked_before_output(args, *, _signal):
    threading.Event().wait()


def test_startup_hang_leaves_abort_even_before_child_creates_output(tmp_path):
    args=N(output=tmp_path/'startup')
    with pytest.raises(ExperimentDeadlineError):
        supervise(blocked_before_output,args,timeout_s=.8,cleanup_s=.2)
    end=json.loads((args.output/'journal.jsonl').read_text())
    assert end['kind']=='aborted'
    assert end['body']['server_cancellation']=='UNKNOWN'


def test_keyboard_interrupt_kills_child_and_records_abort(tmp_path, monkeypatch):
    import tsdb_optimizer.experiments.supervisor as module
    args = N(output=tmp_path/'interrupt')
    def interrupt(*_args):
        deadline = time.monotonic()+2
        while not (args.output/'pid').exists():
            if time.monotonic() > deadline:
                pytest.fail('synthetic child did not start')
            time.sleep(.01)
        raise KeyboardInterrupt
    args.stage = 'connect'
    monkeypatch.setattr(module.select, 'select', interrupt)
    with pytest.raises(KeyboardInterrupt):
        supervise(blocked, args, timeout_s=5, cleanup_s=.2)
    pid = int((args.output/'pid').read_text())
    with pytest.raises(ProcessLookupError):
        os.kill(pid, 0)
    end = json.loads((args.output/'journal.jsonl').read_text().splitlines()[-1])
    assert end['body']['error_type'] == 'KeyboardInterrupt'
    assert end['body']['retry_allowed'] is False
    assert not (args.output/'must_not_dispatch').exists()
