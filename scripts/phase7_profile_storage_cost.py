"""Synthetic local persistence workload, not IoTDB/query benefit evidence."""
import argparse,json,statistics,time
from pathlib import Path
from tsdb_optimizer.storage import SQLiteStore

def run(output):
    output.mkdir(parents=True,exist_ok=False)
    stores={name:SQLiteStore(output/(name+'.sqlite'),wal_full=wal) for name,wal in [('legacy',False),('wal_full',True)]}
    for store in stores.values():
        with store._connect() as c:c.execute('CREATE TABLE probe(id INTEGER PRIMARY KEY, body TEXT)')
    samples={k:[] for k in stores};body='x'*16384
    for block in range(12):
        order=['legacy','wal_full'] if block%2==0 else ['wal_full','legacy']
        for name in order:
            start=time.perf_counter()
            with stores[name].request_session():
                for i in range(10):
                    with stores[name]._connect() as c:c.execute('INSERT INTO probe(body) VALUES(?)',(body,))
            samples[name].append((time.perf_counter()-start)*1000)
    for store in stores.values():
        with store._connect() as c:
            assert c.execute('SELECT count(*) FROM probe').fetchone()[0]==120
            assert c.execute('PRAGMA integrity_check').fetchone()[0]=='ok'
    report={'scope':'synthetic local SQLite persistence, 10 FULL commits/request, not query benefit','raw_ms':samples,
            'median_ms':{k:statistics.median(v) for k,v in samples.items()}}
    (output/'report.json').write_text(json.dumps(report,indent=2)+'\n');print(json.dumps(report['median_ms']))
if __name__=='__main__':
    p=argparse.ArgumentParser();p.add_argument('--output',type=Path,required=True);run(p.parse_args().output)
