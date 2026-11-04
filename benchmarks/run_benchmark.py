"""Phase-8 CLI. --plan never creates a DB connection."""
import argparse
import json
import sys
from pathlib import Path

from tsdb_optimizer.benchmark.analysis import export
from tsdb_optimizer.benchmark.protocol import load_config, schedule
from tsdb_optimizer.benchmark.runner import LiveEnvironment, execute
from tsdb_optimizer.config import load_settings
from tsdb_optimizer.experiments.supervisor import supervise


def run_live(args, *, _signal=None):
    config = load_config(args.config)
    if not args.immutable_epoch_confirmed or not args.exclusive_window_confirmed:
        raise ValueError('declare the immutable epoch and exclusive experiment window before running')
    pid = args.local_db_pid
    if pid is None:
        import psutil
        settings = load_settings(env_file=args.env_file)
        owners = {c.pid for c in psutil.net_connections(kind='tcp')
                  if c.status == psutil.CONN_LISTEN and c.laddr.port == settings.iotdb_port and c.pid is not None}
        if len(owners) != 1:
            raise ValueError('cannot identify a unique local listener; pass --local-db-pid')
        pid = owners.pop()
    environment = LiveEnvironment(config, local_db_pid=pid, env_file=args.env_file,
        cpu_authorized=args.cpu_cotenant_authorized)
    completed = 0
    def progress(kind):
        nonlocal completed
        if _signal is not None:
            _signal(kind)
        if kind == 'round_complete':
            completed += 1
            if completed <= 2 or completed % 10 == 0:
                print(f'Benchmark progress: {completed} rounds completed; '
                      f'journal={args.output}/journal.jsonl', flush=True)
    execute(config, args.output, environment, signal=progress)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--config', type=Path, required=True)
    parser.add_argument('--output', type=Path)
    parser.add_argument('--plan', action='store_true', help='validate and print deterministic plan; no DB access')
    parser.add_argument('--env-file', type=Path)
    parser.add_argument('--local-db-pid', type=int)
    parser.add_argument('--immutable-epoch-confirmed', action='store_true')
    parser.add_argument('--exclusive-window-confirmed', action='store_true')
    parser.add_argument('--cpu-cotenant-authorized', action='store_true')
    args = parser.parse_args()
    config = load_config(args.config)
    if args.plan:
        plan = schedule(config)
        print(json.dumps({'config_sha256': config.checksum, 'allocations': len(plan),
            'measured_requests': sum(a.concurrency*config.requests_per_lane for a in plan),
            'warmup_and_preparation_requests': sum(a.concurrency*(config.warmup+
                (2 if a.cache_condition == 'APPLICATION_WARM' else 0)) for a in plan),
            'plan': [a.model_dump(mode='json') for a in plan]}, indent=2))
        return
    if args.output is None:
        parser.error('--output is required for live execution')
    if args.output.exists():
        parser.error(f'--output already exists: {args.output}; choose a new, nonexistent '
                     'directory for this run; preserve prior results (no append or overwrite)')
    print(f'Starting benchmark: output={args.output}; startup checks and references first. '
          'Progress follows completed rounds. Ctrl+C stops the run; partial evidence is retained.', flush=True)
    try:
        supervise(run_live, args, timeout_s=config.round_limit_s)
    except KeyboardInterrupt:
        # supervise has already stopped the child and preserved an abort record.
        # Plotting/replaying here delays cancellation and can itself be interrupted.
        print(f'Benchmark interrupted; partial evidence retained at {args.output}. '
              'Server cancellation UNKNOWN; no retry. Offline analysis can be run separately.',
              file=sys.stderr, flush=True)
        raise SystemExit(130) from None
    except FileExistsError:
        # The supervisor can also reject a collision after the CLI precheck.
        # That directory belongs to another run: never re-export its reports.
        raise
    except BaseException:
        # Preserve a partial report if the journal is structurally readable.
        # Analysis errors remain visible and never replace the original failure.
        if (args.output/'config.json').exists():
            try:
                export(args.output)
            except Exception as analysis_error:
                print(f'Partial analysis failed: {type(analysis_error).__name__}: {analysis_error}', flush=True)
        raise
    report = export(args.output)
    print(json.dumps({'output': str(args.output), 'execution_complete': report['execution_complete'],
        'analysis_qualified': report['analysis_qualified'], 'stage_accepted': False}))


if __name__ == '__main__':
    main()
