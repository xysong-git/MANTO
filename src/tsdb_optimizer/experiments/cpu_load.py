"""Operator-authorized bounded co-tenant CPU load, not IoTDB query load."""
import multiprocessing
import threading
import time
import psutil


def _burn(stop, duty, end):
    value=1
    while not stop.is_set() and time.monotonic()<end:
        start=time.monotonic(); busy=start+.05*duty.value
        while time.monotonic()<min(busy,end):
            value=(value*1664525+1013904223)&0xffffffff
        stop.wait(max(0,.05-(time.monotonic()-start)))


def pulse(now, origin, index, count, duty):
    """Fixed 20ms clock, evenly distributed phases; no per-worker clock drift."""
    phase=((now-origin)/.02+index/count)%1
    active=phase<duty
    return active, .02*((duty-phase) if active else (1-phase))


def _staggered_burn(stop, duty, end, origin, index, count):
    value=1
    while not stop.is_set() and time.monotonic()<end:
        now=time.monotonic()
        active, duration=pulse(now,origin,index,count,duty.value)
        until=min(now+duration,end)
        if active:
            while time.monotonic()<until:
                value=(value*1664525+1013904223)&0xffffffff
        else:
            stop.wait(max(0,until-time.monotonic()))


class CpuLoad:
    """Workers self-expire by 30s even if the controller or DB client stalls."""
    def __init__(self, enabled, *, duration_s=30, target_percent=60, profile='legacy'):
        if not 0<duration_s<=30 or not 50<=target_percent<=70:
            raise ValueError('CPU load bounds exceeded')
        self.enabled=enabled;self.duration=duration_s;self.target=target_percent
        if profile not in ('legacy','staggered-v1','staggered-spawn-v2'):
            raise ValueError('unknown CPU profile')
        # A live adapter may already own sockets/SQLite handles. Only the
        # explicit spawn profile isolates workers from those parent handles.
        self.ctx=multiprocessing.get_context('spawn' if profile=='staggered-spawn-v2' else 'fork')
        self.stop=self.ctx.Event();self.duty=self.ctx.Value('d',.65)
        self.processes=[];self.samples=[];self.error=None;self.thread=None
        self.count=min(48,len(psutil.Process().cpu_affinity()))
        self.profile=profile
        if profile!='legacy':
            self.count=min(self.count,int(psutil.cpu_count()*.60))
            if self.count<1 or self.count/psutil.cpu_count()*100<target_percent:
                raise ValueError('CPU budget cannot reach requested load')
            self.duty.value=min(1,target_percent/100*psutil.cpu_count()/self.count)

    def __enter__(self):
        self.start=time.monotonic();self.end=self.start+self.duration
        if self.enabled:
            try:
                for i in range(self.count):
                    target=_burn if self.profile=='legacy' else _staggered_burn
                    args=(self.stop,self.duty,self.end)
                    if self.profile!='legacy':args+=(self.start,i,self.count)
                    p=self.ctx.Process(target=target,args=args);p.start();self.processes.append(p)
                self.thread=threading.Thread(target=self.control,daemon=True);self.thread.start()
            except BaseException:
                self.__exit__(None,None,None);raise
        return self

    def control(self):
        try:
            psutil.cpu_percent(None)
            while not self.stop.wait(.25) and time.monotonic()<self.end:
                cpu=psutil.cpu_percent(None);memory=psutil.virtual_memory().percent
                self.samples.append({'elapsed_s':time.monotonic()-self.start,'cpu_percent':cpu,
                    'memory_percent':memory,'worker_duty':self.duty.value})
                if cpu>80 or memory>90:
                    violations=[]
                    if cpu>80:violations.append(f'cpu_percent={cpu} > 80')
                    if memory>90:violations.append(f'memory_percent={memory} > 90')
                    self.error='CO_TENANT_SAFETY_STOP: whole_host '+ '; '.join(violations)
                    self.stop.set();return
                correction=(self.target-cpu)/100*psutil.cpu_count()/self.count*.4
                self.duty.value=max(0,min(1,self.duty.value+correction))
        except BaseException as exc:
            self.error=type(exc).__name__;self.stop.set()

    def check(self):
        if self.error: raise RuntimeError(self.error)
        if self.enabled and time.monotonic()>=self.end:
            raise RuntimeError('CPU load expired; no further dispatch in this round')

    def __exit__(self,*args):
        self.stop.set()
        # One shared cleanup budget, not a separate timeout multiplied by N.
        until=time.monotonic()+1
        for p in self.processes:p.join(timeout=max(0,until-time.monotonic()))
        for p in self.processes:
            if p.is_alive():p.kill()
        for p in self.processes:p.join(timeout=.1)
        if self.thread:self.thread.join(timeout=.5)
        if any(p.is_alive() for p in self.processes):raise RuntimeError('CPU worker termination unconfirmed')
