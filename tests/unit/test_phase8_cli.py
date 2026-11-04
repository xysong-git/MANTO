"""CLI output ownership regression tests; no IoTDB access."""
import importlib.util
from pathlib import Path
import sys
from unittest.mock import Mock

import pytest


@pytest.fixture
def cli(monkeypatch, tmp_path):
    spec = importlib.util.spec_from_file_location('phase8_cli_test', Path('benchmarks/run_benchmark.py'))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    monkeypatch.setattr(sys, 'argv', ['run_benchmark.py', '--config',
        'benchmarks/workloads/small_count.json', '--output', str(tmp_path / 'run')])
    return module, tmp_path / 'run'


def test_existing_output_never_exports_or_starts(cli, monkeypatch, capsys):
    module, output = cli
    output.mkdir()
    (output / 'config.json').write_text('{}')
    (output / 'report.json').write_text('original report')
    before = {p.name: p.read_bytes() for p in output.iterdir()}
    export = Mock()
    monkeypatch.setattr(module, 'export', export)
    # Keep the real supervisor so the pre-fix path reproduces its rejection.
    with pytest.raises(SystemExit) as caught:
        module.main()
    assert caught.value.code == 2
    error = capsys.readouterr().err
    assert str(output) in error and '--output' in error
    export.assert_not_called()
    assert {p.name: p.read_bytes() for p in output.iterdir()} == before


def test_supervisor_output_collision_never_exports(cli, monkeypatch):
    module, output = cli
    def collision(*args, **kwargs):
        output.mkdir()
        (output / 'config.json').write_text('{}')
        raise FileExistsError('output must be new')
    monkeypatch.setattr(module, 'supervise', collision)
    export = Mock()
    monkeypatch.setattr(module, 'export', export)
    with pytest.raises(FileExistsError):
        module.main()
    export.assert_not_called()


def test_current_run_failure_still_exports_and_reraises(cli, monkeypatch):
    module, output = cli
    failure = RuntimeError('synthetic child failed')
    def fails(*args, **kwargs):
        output.mkdir()
        (output / 'config.json').write_text('{}')
        raise failure
    monkeypatch.setattr(module, 'supervise', fails)
    export = Mock()
    monkeypatch.setattr(module, 'export', export)
    with pytest.raises(RuntimeError) as caught:
        module.main()
    assert caught.value is failure
    export.assert_called_once_with(output)


def test_interrupt_exits_130_without_export(cli, monkeypatch, capsys):
    module, output = cli
    def interrupted(*args, **kwargs):
        output.mkdir()
        (output/'config.json').write_text('{}')
        (output/'journal.jsonl').write_text('{"kind":"aborted"}\n')
        raise KeyboardInterrupt
    monkeypatch.setattr(module, 'supervise', interrupted)
    export = Mock()
    monkeypatch.setattr(module, 'export', export)
    with pytest.raises(SystemExit) as caught:
        module.main()
    assert caught.value.code == 130
    export.assert_not_called()
    assert 'interrupted' in capsys.readouterr().err
    assert (output/'journal.jsonl').read_text() == '{"kind":"aborted"}\n'


def test_live_progress_preserves_supervisor_signals(cli, monkeypatch, capsys):
    from types import SimpleNamespace
    module, output = cli
    args = SimpleNamespace(config=Path('benchmarks/workloads/small_count.json'), output=output,
        immutable_epoch_confirmed=True, exclusive_window_confirmed=True,
        local_db_pid=123, env_file=None, cpu_cotenant_authorized=False)
    monkeypatch.setattr(module, 'LiveEnvironment', Mock())
    def execute(_config, _output, _environment, *, signal):
        for _ in range(10):
            signal('round_start')
            signal('round_complete')
    monkeypatch.setattr(module, 'execute', execute)
    signals = []
    module.run_live(args, _signal=signals.append)
    assert signals == ['round_start', 'round_complete']*10
    output = capsys.readouterr().out
    assert '1 rounds completed' in output and '10 rounds completed' in output
