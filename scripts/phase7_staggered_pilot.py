"""No-query bounded test of staggered CPU load with the real sampling path."""
import argparse
import json
from pathlib import Path
import time
from phase7_controlled_e2e import CheckedMonitor, round_load
from phase7_delayed_batch import admit
from tsdb_optimizer.config import load_settings
from tsdb_optimizer.experiments.supervisor import supervise
from tsdb_optimizer.monitoring.local_db import VerifiedLocalDbHostSampler
from tsdb_optimizer.monitoring.system import SystemMonitor, PsutilSystemSampler
from tsdb_optimizer.experiments.exclusive_instance import inspect_clients


def run(args, *, _signal=None):
    if not args.cpu_cotenant_authorized:raise ValueError('bounded co-tenant authorization required')
    args.output.mkdir(exist_ok=False,parents=True)
    with (args.output/'journal.jsonl').open('x') as stream:
        def emit(kind,body):
            if _signal and kind=='round_start':_signal(kind)
            stream.write(json.dumps({'kind':kind,'body':body})+'\n');stream.flush()
            if _signal and kind=='round_complete':_signal(kind)
        settings=load_settings()
        sampler=VerifiedLocalDbHostSampler(host=settings.iotdb_host,port=settings.iotdb_port,
            pid=args.local_db_pid,stale_after_ms=500)
        monitor=CheckedMonitor(snapshot_mode='latest',latest_max_age_ms=200,latest_wait_timeout_s=.5,
            monitor=SystemMonitor(db_host_sampler=sampler,interval_s=.1,
                client_sampler=PsutilSystemSampler(stale_after_ms=500)))
        emit('protocol',{'profile':'staggered-spawn-v2','start_method':'spawn','repeat':3,'seconds_per_round':8,
            'warmup_s':3,'cpu_target':55,'cpu_stop':80,'memory_stop':90,'disk_stop':90,
            'snapshot_max_age_ms':250,'minimum_unique_samples':20,'minimum_target_fraction':.8,
            'identity':sampler.identity.model_dump(),'query_dispatches':0})
        try:
            observation=inspect_clients(args.local_db_pid,settings.iotdb_port)
            emit('instance_occupancy',observation.model_dump(mode='json'))
            if not observation.passed:raise RuntimeError('foreign/unknown IoTDB clients; no load')
            monitor.start()
            for repeat in range(3):
                for load in ('LOW','MEDIUM'):
                    emit('round_start',{'repeat':repeat,'load':load})
                    observation=inspect_clients(args.local_db_pid,settings.iotdb_port)
                    emit('instance_occupancy',observation.model_dump(mode='json'))
                    if not observation.passed:raise RuntimeError('foreign/unknown IoTDB clients; no load')
                    samples={}
                    with round_load(load=='MEDIUM',monitor,target_percent=55,profile='staggered-spawn-v2') as stress:
                        time.sleep(3);start=time.monotonic()
                        while time.monotonic()-start<8:
                            stress.check();s,at=admit(monitor,emit)
                            emit('observation',{'at':at.isoformat(),'sample':s.model_dump(mode='json')})
                            samples[s.db_host.snapshot_id]=s.db_host
                            time.sleep(.05)
                        stress.check()
                    cpus=[s.cpu_percent for s in samples.values()]
                    fraction=sum(c<50 if load=='LOW' else 50<=c<=70 for c in cpus)/len(cpus)
                    passed=len(cpus)>=20 and fraction>=.8
                    summary={'repeat':repeat,'load':load,'unique_samples':len(cpus),'fraction':fraction,
                        'cpu_min':min(cpus),'cpu_max':max(cpus),'passed':passed,'controller':stress.samples}
                    emit('round_complete',summary)
                    print(json.dumps({k:v for k,v in summary.items() if k!='controller'}),flush=True)
                    if not passed:raise RuntimeError('unstable pilot; no study authorized by pilot')
            emit('complete',{'passed':True,'query_dispatches':0})
        except BaseException as exc:
            emit('aborted',{'error_type':type(exc).__name__,'retry_allowed':False});raise
        finally:
            monitor.stop()
            if monitor.is_monitoring:raise RuntimeError('monitor termination unconfirmed')


if __name__=='__main__':
    p=argparse.ArgumentParser();p.add_argument('--output',type=Path,required=True)
    p.add_argument('--local-db-pid',type=int,required=True);p.add_argument('--cpu-cotenant-authorized',action='store_true')
    supervise(run,p.parse_args())
