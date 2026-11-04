"""Summarize screening, including failures; never certify learning readiness."""
import argparse
from collections import Counter
from datetime import datetime
import hashlib
import json
from pathlib import Path
import runpy


from tsdb_optimizer.experiments.profile_audit import summarize


if __name__=='__main__':
    p=argparse.ArgumentParser();p.add_argument('journal',type=Path);p.add_argument('--output',type=Path,required=True)
    args=p.parse_args(); report=summarize(args.journal)
    args.output.write_text(json.dumps(report,indent=2,allow_nan=False)+'\n')
    print(json.dumps({'complete':report['schedule_complete'],'rounds':len(report['rounds'])}))
