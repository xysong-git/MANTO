"""Linux process-group deadline guard for explicit experiment CLIs.

No database SDK or sampler runs in the supervisor. Killing a client does NOT
confirm server cancellation. A failed run is never retried automatically.
"""
import json
import multiprocessing
import os
from pathlib import Path
import select
import signal
import time


class ExperimentDeadlineError(TimeoutError):
    pass


def _child(target, args, read_fd, write_fd):
    os.close(read_fd)
    os.setsid()
    def send(kind):
        data=(json.dumps({'kind':kind})+'\n').encode()
        while data:
            n=os.write(write_fd,data); data=data[n:]
    try:
        target(args, _signal=send)
        send('returned')
    except BaseException:
        # Full traceback remains on stderr; parent also checks process exit.
        send('failed')
        raise
    finally:
        os.close(write_fd)


def supervise(target, args, *, timeout_s=60, cleanup_s=1):
    """Startup, each round, and shutdown have independent bounded windows.

    Total round budget includes a cleanup reserve; kill occurs before its end.
    Fork is deliberately Linux-only and starts before any parent DB connection.
    """
    if timeout_s <= cleanup_s or cleanup_s <= 0:
        raise ValueError('timeout must exceed positive cleanup reserve')
    if Path(args.output).exists():
        raise FileExistsError('output must be new; never append to a prior run')
    read_fd,write_fd=os.pipe()
    proc=multiprocessing.get_context('fork').Process(target=_child,args=(target,args,read_fd,write_fd))
    deadline=time.monotonic()+timeout_s-cleanup_s
    buffer=b''; active=False; returned=False; failed=False
    try:
        proc.start(); os.close(write_fd); write_fd=None
        os.set_blocking(read_fd,False)
        while True:
            remaining=deadline-time.monotonic()
            if remaining <= 0:
                raise ExperimentDeadlineError('experiment deadline; server cancellation UNKNOWN; no retry')
            part=None
            readable,_,_=select.select([read_fd],[],[],min(.05,remaining))
            if readable:
                part=os.read(read_fd,65536)
                buffer+=part
                if len(buffer)>65536: raise RuntimeError('invalid supervisor signal stream')
                while b'\n' in buffer:
                    line,buffer=buffer.split(b'\n',1)
                    kind=json.loads(line)['kind']
                    if kind=='round_start':
                        if active or failed or returned: raise RuntimeError('invalid round lifecycle')
                        active=True;deadline=time.monotonic()+timeout_s-cleanup_s
                    elif kind=='round_complete':
                        if not active: raise RuntimeError('completion without start')
                        active=False;deadline=time.monotonic()+timeout_s-cleanup_s
                    elif kind=='returned': returned=True
                    elif kind=='failed': failed=True
                    else: raise RuntimeError('unknown supervisor signal')
            if not proc.is_alive():
                proc.join(timeout=0)
                if part:
                    continue  # drain buffered lifecycle signals before checking exit
                if proc.exitcode != 0 or failed or not returned or active:
                    raise RuntimeError('experiment child failed; inspect journal/stderr; no retry')
                return
    except BaseException as exc:
        # SIGKILL cannot be caught by a blocked Python worker/SDK cleanup thread.
        if proc.pid:
            try: os.killpg(proc.pid,signal.SIGKILL)
            except ProcessLookupError:
                if proc.is_alive(): proc.kill()  # child may not have reached setsid
            proc.join(timeout=cleanup_s)
        path=Path(args.output)/'journal.jsonl'
        path.parent.mkdir(parents=True,exist_ok=True)
        if not path.exists(): path.touch()
        if path.exists():
            # Child is stopped before touching its journal. Do not duplicate its
            # existing abort terminal. If cleanup failed, explicitly invalidate.
            if proc.is_alive():
                raise RuntimeError('client termination unconfirmed; no further dispatch') from exc
            lines=path.read_bytes().splitlines()
            try:
                last=json.loads(lines[-1]) if lines else {}
            except (json.JSONDecodeError, UnicodeDecodeError):
                last={}  # keep interrupted record intact; audit must reject it
            if last.get('kind')!='aborted':
                with path.open('a') as stream:
                    if path.stat().st_size and not path.read_bytes().endswith(b'\n'):
                        stream.write('\n')
                    stream.write(json.dumps({'kind':'aborted','body':{
                        'error_type':type(exc).__name__,'source':'process_supervisor',
                        'server_cancellation':'UNKNOWN','retry_allowed':False}})+'\n')
        raise
    finally:
        os.close(read_fd)
        if write_fd is not None: os.close(write_fd)
