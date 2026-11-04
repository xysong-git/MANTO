"""Versioned, pre-execution-only features. No fitted test-set scaler."""
import math
from datetime import datetime

from ..schemas.learning import Experience, digest

ENCODER_VERSION = 'linucb-context-v1'
NAMES = ('device_count', 'series_count', 'filter_count', 'time_range_ms',
         'client_cpu_percent', 'client_memory_percent', 'db_cpu_percent', 'db_memory_percent')
DIMENSIONS = ('bias', *NAMES, *(name + '_missing' for name in NAMES))
CAPS = (1000., 1000., 100., 86400000., 100., 100., 100., 100.)


def encode(features, sample, *, at: datetime) -> tuple[float, ...]:
    values = [getattr(features, name).value for name in NAMES[:4]]
    for source in ('client_host', 'db_host'):
        snapshot = getattr(sample, source) if sample is not None else None
        usable = snapshot is not None and snapshot.source_type.value == source and snapshot.is_usable(now=at)
        values.extend([getattr(snapshot, field) if usable else None
                       for field in ('cpu_percent', 'memory_percent')])
    scaled, missing = [], []
    for i, (value, cap) in enumerate(zip(values, CAPS)):
        if value is None:
            scaled.append(0.); missing.append(1.)
        else:
            if not math.isfinite(value) or value < 0:
                raise ValueError('feature must be finite and nonnegative')
            clipped = min(float(value), cap)
            scaled.append(math.log1p(clipped)/math.log1p(cap) if i < 4 else clipped/cap)
            missing.append(0.)
    return (1., *scaled, *missing)


def partition_key(experience: Experience) -> str:
    # Do not pool cache hits with server latency or different environments.
    # Load bucket and SQL shape are modeled, not hard partition keys.
    context = experience.context.model_dump(mode='json')
    context.pop('result_rows')  # outcome, never an input feature
    context.pop('load_bucket')
    return digest({'context': context, 'policy': experience.resource_policy_hash,
                   'encoder': ENCODER_VERSION})
