"""Read-only wait for an operator-requested exclusive window; never sends SQL."""
import argparse
import json
from pathlib import Path
import time
from tsdb_optimizer.experiments.exclusive_instance import inspect_clients


def main():
    p=argparse.ArgumentParser();p.add_argument('--pid',type=int,required=True)
    p.add_argument('--port',type=int,default=6667);p.add_argument('--output',type=Path,required=True)
    p.add_argument('--quiet-seconds',type=float,default=60)
    args=p.parse_args()
    if not 60<=args.quiet_seconds<=600:raise ValueError('quiet window must be 60–600 seconds')
    deadline=time.monotonic()+7200;clear_since=None;last=None
    with args.output.open('x') as stream:
        while time.monotonic()<deadline:
            observation=inspect_clients(args.pid,args.port)
            now=time.monotonic();body=observation.model_dump(mode='json')
            stream.write(json.dumps(body)+'\n');stream.flush()
            state=(tuple(observation.foreign_pids),observation.unknown_connections,observation.passed)
            if state!=last:print(json.dumps(body),flush=True);last=state
            if observation.passed:
                if clear_since is None:clear_since=now
                if now-clear_since>=args.quiet_seconds:
                    print(json.dumps({'ready':True,'quiet_seconds':now-clear_since}),flush=True);return
            else:clear_since=None
            time.sleep(15)
    raise TimeoutError('exclusive instance window unavailable within two hours; no query/load dispatched')


if __name__=='__main__':main()
