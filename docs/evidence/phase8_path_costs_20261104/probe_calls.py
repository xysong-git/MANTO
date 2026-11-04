"""Synthetic SDK call-count probe. Not a performance or IoTDB experiment."""
import json,tempfile
from pathlib import Path
from contextlib import nullcontext
from unittest.mock import patch
import sqlglot
from tsdb_optimizer import sql_guard
from tsdb_optimizer.benchmark.protocol import schedule
from tsdb_optimizer.benchmark.runtime import Lane
from tsdb_optimizer.request_memo import request_scope
from test_phase8_benchmark import config,SyntheticEnvironment

results=[]
with tempfile.TemporaryDirectory() as tmp:
 for label,group,memo,connection in [('E1_existing','E1',False,False),
   ('E1_memo_only_SYNTHETIC','E1',True,False),
   ('E1_memo_session_SYNTHETIC','E1',True,True),('E2_existing','E2',False,False),
   ('E3_existing','E3',False,False)]:
  cfg=config(groups=[group],cache_conditions=['APPLICATION_WARM'])
  env=SyntheticEnvironment(cfg)
  alloc=next(a for a in schedule(cfg) if a.phase=='HOLDOUT')
  query=cfg.workloads[0].holdout
  reference=env.reference(query,None,None)
  output=Path(tmp)/label;output.mkdir()
  lane=Lane(config=cfg,allocation=alloc,lane=0,query=query,reference=reference,
     settings=env.settings,monitor=env.monitor,output=output,bank=None,adapter_factory=env.adapter_factory)
  try:
   lane.prepare(lambda row: None)
   if memo:lane.service.run=request_scope(lane.service.run)
   before=sum(len(s.executed) for s in env.sessions)
   with patch.object(sql_guard,'parse',wraps=sql_guard.parse) as guard, \
        patch.object(sqlglot,'parse',wraps=sqlglot.parse) as parser, \
        patch.object(lane.store,'_open_connection',wraps=lane.store._open_connection) as opened:
    with lane.store.request_session() if connection else nullcontext():
     row=lane.request(0)
   assert row.correctness=='MATCHED' and row.success
   assert row.run.decision.selected_strategy_id=='S1_REPEAT_CACHE'
   dispatches=sum(len(s.executed) for s in env.sessions)-before
   assert dispatches==0
   results.append({'variant':label,'evidence_domain':'SYNTHETIC_TEST_ONLY',
      'guard_parse_calls':guard.call_count,'feature_sqlglot_parse_calls':parser.call_count,
      'sqlite_connections_opened':opened.call_count,'dispatches':dispatches,'correctness':row.correctness})
  finally:lane.close()
p=Path(__file__).with_name('call_counts.json');p.write_text(json.dumps(results,indent=2)+'\n')
print(p.read_text())
