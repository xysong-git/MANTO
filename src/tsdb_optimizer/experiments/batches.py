"""Predeclared delayed-batch study schedule and actual paired comparisons."""
from collections import Counter, defaultdict
import random
import statistics
from dataclasses import dataclass
from pydantic import BaseModel, ConfigDict, Field, model_validator

LOADS = ('LOW', 'MEDIUM')
SIZES = (2, 8)
POLICIES = ('S0_NATIVE', 'S1_REPEAT_CACHE', 'ADAPTIVE')
L1_POLICIES = (*POLICIES, 'L1_BATCH')


class SelectionTiming(BaseModel):
    model_config = ConfigDict(extra='forbid', frozen=True)
    setup_ns: int = Field(ge=0, strict=True)
    features_ns: int = Field(ge=0, strict=True)
    admission_ns: int = Field(ge=0, strict=True)
    prediction_ns: int = Field(ge=0, strict=True)
    fingerprint_ns: int = Field(ge=0, strict=True)
    finalize_ns: int = Field(ge=0, strict=True)
    total_ns: int = Field(gt=0, strict=True)

    @model_validator(mode='after')
    def accounted(self):
        if sum(v for k, v in self.model_dump().items() if k != 'total_ns') != self.total_ns:
            raise ValueError('selection timing does not reconcile')
        return self


@dataclass(frozen=True)
class BatchSummary:
    size: int
    policy: str
    allocated_strategy: str
    load_bucket: str
    consistent: bool
    service_ms: float
    adapter_ms: float
    hits: int

    @classmethod
    def from_feedback(cls, f):
        return cls(f.contract.size, f.policy, f.allocated_strategy, f.load_bucket,
            all(e.context.load_bucket == f.load_bucket and e.sample_status.value == 'VALID'
                for r in f.requests for e in r.experiences),
            f.service_ms, sum(a.execution.metrics.client_wall_ms for r in f.requests
                for a in r.workflow.run.execution.attempts),
            sum(a.execution.served_from_cache for r in f.requests for a in r.workflow.run.execution.attempts))


def plan(phase, rounds=30, *, include_l1=False):
    if phase not in ('TRAIN', 'HOLDOUT'): raise ValueError('unknown phase')
    if rounds not in (30,45):raise ValueError('undeclared study size')
    rng = random.Random(20261107 if phase == 'TRAIN' else 20261108)
    for block in range(rounds):
        loads = list(LOADS); rng.shuffle(loads)
        for load in loads:
            policies = L1_POLICIES if include_l1 else POLICIES
            batches = [(n,p) for n in SIZES for p in (POLICIES[:2] if phase == 'TRAIN' else policies)]
            rng.shuffle(batches)
            yield {'phase': phase, 'block': block, 'load': load,
                'batches': [{'size': n, 'policy': p} for n,p in batches]}


def paired_contrast(blocks, comparator, rounds=30):
    groups = defaultdict(list)
    for (load, size, block), rows in sorted(blocks.items()):
        groups[(load,size)].append((block, rows['ADAPTIVE'].service_ms-rows[comparator].service_ms))
    if set(groups) != {(l,n) for l in LOADS for n in SIZES} or any(len(v) != rounds for v in groups.values()):
        raise ValueError('incomplete paired blocks')
    rng = random.Random(20261109)
    boot = sorted(statistics.mean(delta for group in groups.values()
        for _,delta in rng.choices(group,k=rounds)) for _ in range(4000))
    width=rounds//3
    batches = [statistics.mean(delta for group in groups.values() for b,delta in group if i*width <= b < (i+1)*width) for i in range(3)]
    return {'mean_ms': statistics.mean(d for group in groups.values() for _,d in group),
        'ci99_ms': [boot[20],boot[3979]], 'three_batch_means_ms': batches,
        'within_session_benefit': boot[3979] < 0 and all(v < 0 for v in batches)}


def summarize(outcomes, training_counts, rounds=30, *, include_l1=False):
    policies = L1_POLICIES if include_l1 else POLICIES
    blocks = defaultdict(dict)
    actual_counts = Counter()
    for target, block, f in outcomes:
        key = (target, f.size, block)
        if f.policy in blocks[key]: raise ValueError('duplicate held-out batch')
        blocks[key][f.policy] = f
        if f.consistent and f.load_bucket == target:
            actual_counts[(f.load_bucket,f.size,f.policy)] += 1
    expected = {(l,n,b) for l in LOADS for n in SIZES for b in range(rounds)}
    if set(blocks) != expected or any(set(v) != set(policies) for v in blocks.values()):
        raise ValueError('incomplete held-out experiment')
    training_gate = all(training_counts[(l,n,p)] >= 30 for l in LOADS for n in SIZES for p in POLICIES[:2])
    test_gate = all(actual_counts[(l,n,p)] >= 30 for l in LOADS for n in SIZES for p in policies)
    cells = {}
    for load in LOADS:
        for n in SIZES:
            rows = [f for l,b,f in outcomes if l == load and f.size == n]
            cells[f'{load}/{n}'] = {
                'mean_service_ms': {p: statistics.mean(f.service_ms for f in rows if f.policy == p) for p in policies},
                'mean_adapter_total_ms': {p: statistics.mean(f.adapter_ms for f in rows if f.policy == p) for p in policies},
                'adaptive_choices': dict(Counter(f.allocated_strategy for f in rows if f.policy == 'ADAPTIVE')),
                'cache_hits': {p: sum(f.hits for f in rows if f.policy == p) for p in policies}}
            if include_l1:
                cells[f'{load}/{n}']['l1_choices'] = dict(Counter(f.allocated_strategy for f in rows if f.policy == 'L1_BATCH'))
    contrasts = {p: paired_contrast(blocks,p,rounds) for p in POLICIES[:2]} if training_gate and test_gate else {}
    return {'training_sample_gate': training_gate, 'holdout_sample_gate': test_gate,
        'actual_holdout_counts': {'/'.join(map(str,k)):v for k,v in actual_counts.items()},
        'cells': cells, 'contrasts': contrasts,
        'within_session_benefit_gate': bool(contrasts) and all(v['within_session_benefit'] for v in contrasts.values()),
        **({'l1_contrast': paired_contrast(blocks, 'L1_BATCH', rounds) if training_gate and test_gate else None}
           if include_l1 else {}),
        'online_ready': False, 'stage_accepted': False}
