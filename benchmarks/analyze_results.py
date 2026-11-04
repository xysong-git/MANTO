"""Offline reanalysis: no database calls, no mutation of original evidence."""
import argparse
import json
from pathlib import Path

from tsdb_optimizer.benchmark.analysis import export


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('run_directory', type=Path)
    parser.add_argument('--output', type=Path, required=True, help='new analysis directory')
    args = parser.parse_args()
    report = export(args.run_directory, args.output)
    print(json.dumps({'execution_complete': report['execution_complete'],
        'analysis_qualified': report['analysis_qualified'], 'counts': report['counts']}))


if __name__ == '__main__':
    main()
