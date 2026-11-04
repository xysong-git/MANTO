"""Predeclared conditional S1/S0 applicability analysis, not deployment approval."""
import math
import random
import statistics

PLAN={'contrast':'S1 minus S0 per target load/cache','confidence':.99,
      'replicates':4000,'seed':20261105,'minimum_blocks':30,
      'repeats':'three consecutive equal batches; consistent sign required',
      'scope':'conditional request cost; cache preparation separately reported; exploratory across scenarios'}


def scenario_report(blocks, plan):
    if plan != PLAN:
        raise ValueError('changed scenario analysis plan')
    report={}
    for load in ('LOW','MEDIUM'):
        for cache in ('EMPTY','READY'):
            rows=sorted((b,p) for (l,c,b),p in blocks.items() if (l,c)==(load,cache))
            n=len(rows)
            qualified=n>=30 and all(sum(p[k].experience.context.load_bucket==load for _,p in rows)>=30
                                    for k in ('S0_NATIVE','S1_REPEAT_CACHE','ADAPTIVE'))
            values=[p['S1_REPEAT_CACHE'].e2e_ms-p['S0_NATIVE'].e2e_ms for _,p in rows]
            item={'paired_blocks':n,'sample_gate':qualified,'conclusion':'UNKNOWN',
                  'mean_s1_minus_s0_ms':statistics.mean(values) if values else None,
                  'ci99_ms':None,'three_batch_mean_differences_ms':[],
                  'cache_hit_counts':{k:sum(p[k].experience.context.result_source=='CACHE_HIT' for _,p in rows)
                                      for k in ('S0_NATIVE','S1_REPEAT_CACHE','ADAPTIVE')}}
            if qualified:
                rng=random.Random(plan['seed'])
                samples=sorted(statistics.mean(rng.choices(values,k=n)) for _ in range(plan['replicates']))
                lo,hi=samples[math.floor(.005*len(samples))],samples[math.ceil(.995*len(samples))-1]
                batches=[statistics.mean(values[i*n//3:(i+1)*n//3]) for i in range(3)]
                item.update(ci99_ms=[lo,hi],three_batch_mean_differences_ms=batches)
                item['conclusion']=('S1_FASTER_WITHIN_SESSION' if hi<0 and all(v<0 for v in batches)
                    else 'S0_FASTER_WITHIN_SESSION' if lo>0 and all(v>0 for v in batches)
                    else 'NO_STABLE_DIFFERENCE')
            report[load+'/'+cache]=item
    return {'cells':report,'deployment_ready':False,
            'limitations':['Conditional on explicit cache state; READY preparation is not free.',
                'Pointwise 99% intervals across scenarios are exploratory, not simultaneous confidence.',
                'One session per workload; serial dependence and cross-session reproducibility remain open.']}
