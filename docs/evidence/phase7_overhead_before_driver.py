import argparse,cProfile,pstats,runpy
from pathlib import Path
from tsdb_optimizer.experiments.supervisor import supervise
namespace=runpy.run_path('scripts/phase7_controlled_e2e.py')
def measured(args,*,_signal=None):
    profiler=cProfile.Profile();profiler.enable()
    try:namespace['run'](args,_signal=_signal)
    finally:
        profiler.disable();profiler.dump_stats(str(args.output)+'.prof')
        with open(str(args.output)+'.profile.txt','w') as stream:
            pstats.Stats(profiler,stream=stream).strip_dirs().sort_stats('cumulative').print_stats(70)
args=argparse.Namespace(output=Path('data/phase7_overhead_before_20261103'),epoch='heavy-complete-20261102-v2',local_db_pid=3879777,immutable_epoch_confirmed=True,cpu_cotenant_authorized=True,pilot_only=False,train_rounds=1,test_blocks=1)
supervise(measured,args)
