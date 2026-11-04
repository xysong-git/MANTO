"""Read-only component replay of old/new fact caches; never a query benchmark."""
import argparse
from functools import lru_cache
import json
from pathlib import Path
import sqlite3
import time

from tsdb_optimizer.learning.repository import _FactCache, _read_fact


def profile(database):
    with sqlite3.connect(database.resolve().as_uri()+'?mode=ro', uri=True) as connection:
        group, count = connection.execute('SELECT group_key,count(*) FROM experiences GROUP BY group_key ORDER BY count(*) DESC LIMIT 1').fetchone()
        rows = [r[0] for r in connection.execute('SELECT body_json FROM experiences WHERE group_key=?', (group,))]
    output = {'scope': 'component replay only; not query speedup', 'rows': count, 'group_key': group}
    previous = None
    for name, cache in [('legacy_128_body_lru', lru_cache(maxsize=128)(_read_fact)), ('compact_content_facts', _FactCache())]:
        times = []
        for _ in range(3):
            start = time.perf_counter()
            facts = [cache(body) if len(body) <= 262144 else _read_fact(body) for body in rows]
            times.append((time.perf_counter()-start)*1000)
        if previous is not None and facts != previous:
            raise ValueError('fact cache changes validated statistics inputs')
        previous = facts
        output[name] = {'passes_ms': times, 'cache_info': cache.cache_info()._asdict()}
        if isinstance(cache, _FactCache): output[name]['retained_bytes'] = cache.retained_bytes
    output['exact_facts_equal'] = True
    return output


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--database', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    result = profile(args.database)
    with args.output.open('x') as stream: stream.write(json.dumps(result, indent=2)+'\n')
    print(json.dumps(result, indent=2))
