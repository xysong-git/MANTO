import json,time,datetime,psutil
from pathlib import Path

def read():
 t=time.monotonic(); cpu=psutil.cpu_times()._asdict(); processes={}
 for p in psutil.process_iter(['pid','name','create_time','cpu_times']):
  try:
   d=p.info;c=d['cpu_times'];processes[(d['pid'],d['create_time'])]=(c.user+c.system,d['name'])
  except (psutil.NoSuchProcess,psutil.AccessDenied):pass
 return t,cpu,processes
out=Path('/tmp/manto-host-cpu-20261103-2123/windows.jsonl')
a=read()
with out.open('x') as f:
 for i in range(100):
  time.sleep(.1);b=read();dt=b[0]-a[0]
  delta={k:b[1][k]-a[1][k] for k in b[1]}
  total=sum(delta.values())-delta.get('guest',0)-delta.get('guest_nice',0)
  busy=total-delta['idle']-delta.get('iowait',0)
  procs=[]
  for key,(v,name) in b[2].items():
   if key in a[2]:
    used=v-a[2][key][0]
    if used>0:procs.append({'pid':key[0],'name':name,'cpu_seconds':used,'host_share_percent':100*used/total if total else None})
  procs.sort(key=lambda v:v['cpu_seconds'],reverse=True)
  f.write(json.dumps({'at':datetime.datetime.now(datetime.timezone.utc).isoformat(),'interval_s':dt,'host_cpu_percent':100*busy/total if total else None,'cpu_delta':delta,'processes':procs[:12]})+'\n');f.flush();a=b
print(str(out))
