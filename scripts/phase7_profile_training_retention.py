"""Read-only historical object-retention probe, not query performance evidence."""
import argparse
import gc,json,time,statistics
from pathlib import Path
from tsdb_optimizer.orchestration.models import WorkflowResult
from tsdb_optimizer.schemas.learning import Experience
from tsdb_optimizer.bandit.completed_request import CompletedRequest
parser=argparse.ArgumentParser()
parser.add_argument('--journal',type=Path,required=True)
parser.add_argument('--output',type=Path,required=True)
args=parser.parse_args()
if args.output.exists():raise FileExistsError('do not overwrite probe evidence')
path=args.journal
retained={};experiences=[]
with path.open() as stream:
 for line in stream:
  obj=json.loads(line)
  if obj['kind']=='trial' and obj['body']['phase']=='train':
   b=obj['body'];e=Experience.model_validate(b['experience']);experiences.append(e)
   retained[e.execution_id]=(CompletedRequest.model_validate(b['completed_request']),WorkflowResult.model_validate(b['workflow']))
  if obj['kind']=='frozen_models':break
obj=b=line=e=None
measurements=[]
for phase in ['retained_training_workflows','released_training_workflows']:
 if phase.startswith('released'):retained.clear();gc.collect()
 times=[]
 for _ in range(8):
  t=time.perf_counter_ns();gc.collect();times.append((time.perf_counter_ns()-t)/1e6)
 measurements.append({'phase':phase,'retained_workflows':len(retained),'retained_experiences':len(experiences),'tracked_objects':len(gc.get_objects()),'gc_full_ms':times,'median_gc_full_ms':statistics.median(times)})
report={'scope':'offline historical object-retention probe; NOT causal proof of the live pause; NOT query benefit','measurements':measurements}
args.output.write_text(json.dumps(report,indent=2)+'\n')
print(json.dumps(report))
