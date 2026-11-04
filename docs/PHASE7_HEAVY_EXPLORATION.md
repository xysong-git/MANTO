# 阶段7 heavy_scale 阻塞修复与真实实验

后续导入修复已完成，当前状态见 [HEAVY_IMPORT_REPAIR.md](HEAVY_IMPORT_REPAIR.md)。本文保留修复前的真实结果。

日期：2026-11-02。范围仅阶段7证据完善，部署保持关闭，不进入阶段8。

## 修改

- `scripts/phase7_exploration.py`：精确选择 SQL 文件物理行，使用现有只读守卫，记录路径/行号/文件及SQL SHA-256；允许背景查询单独指定。支持每单元1–8个有界轮次，跨轮保持连续随机序号；采样后再次检查发起窗口。
- `monitoring/local_db.py`：经过进程、监听与命名空间核验的本机 DB 主机采样器；不支持远程目标，不以 client 数据代替 DB 数据。
- `monitoring/system.py`：允许经过身份约束的子类直接构造独立 DB 快照，默认来源不变。
- `scripts/phase7_analyze_exploration.py`：审计多轮完整性、轮次身份唯一性和工作负载哈希；根据真实快照判定本轮 DB 主机指标是否可用。
- 无策略改写、阈值放宽、未知语法支持扩大或线上模型启用；S0/S1仍经过Registry/Validator，只有成功合格事实参与学习。

## 协议与限制

沿用用户确认的数据不变条件和只读授权：最多4个连接，单查询SDK超时30000ms，每轮60秒，前30秒允许发起，停止新增后等待自有请求结束。超时不等于服务器取消；一旦失败或超时中止，不自动开始下一轮。每轮不强行凑齐数量。

只使用三规模 `q03_count_aggregation.sql` 第一行，未执行全部24000条。选择聚合是为了完整保存参考结果及缓存结果，避免原始宽结果预览截断。Native参考与缓存准备单独记录，不算随机测量或额外反事实奖励。EMPTY只清客户端结果缓存，不声称IoTDB/OS冷缓存。

训练全部先于留出；分配p=0.5，PRNG逐次重放；训练/留出以及三规模、client/db来源和epoch不混合。多轮不是独立跨会话复现。延迟对比是固定动作对照，离线IPS不是实际部署自适应收益。

## 实测前置证据

当前默认目标127.0.0.1:6667，IoTDB 2.0.6 tree；监听为Java DataNode，PID3879777，主类org.apache.iotdb.db.service.DataNode。与实验进程net/mnt/pid命名空间相同。PID仅是本次快照，复跑需重新识别，不能盲用旧PID。无需部署远端代理即可采集本实例的整机指标；不宣称该实例资源隔离，也不代表远程监控已实现。

小、中、大各32条pilot全部成功且VALID。小/中采集client_host，大规模启用独立db_host；不得将前两组追溯补为db_host。大规模DB CPU范围2–35%，内存64.3–64.4%，磁盘容量21.7%，32条均MEDIUM。当前load_identity取三者最大值，阈值50/90不变；因此背景线程数不同仍不构成多个实测负载桶。

原始日志和SQLite分别位于 `data/phase7_heavy_{small,medium,large}_pilot_20261102/`；可分享报告为 `docs/evidence/phase7_heavy_{small,medium,large}_pilot_20261102.json`。数据目录被gitignore，不得删除原始事实只留汇总。

## 正式分轮采样命令

```bash
PYTHONPATH=src /opt/anaconda3/envs/MANTO/bin/python scripts/phase7_exploration.py --output data/phase7_heavy_small_dbhost_20261102 --epoch heavy-small-dbhost-immutable-20261102 --immutable-epoch-confirmed --sql-file query/heavy_scale/small_scale/q03_count_aggregation.sql --local-db-pid 3879777 --background --trials 64 --rounds-per-cell 3
PYTHONPATH=src /opt/anaconda3/envs/MANTO/bin/python scripts/phase7_analyze_exploration.py data/phase7_heavy_small_dbhost_20261102/journal.jsonl --output docs/evidence/phase7_heavy_small_dbhost_20261102.json
```

## 新发现：源数据与数据库不一致

三规模均核实一个查询设备的全部三个CSV；每设备3000个唯一时间戳、步长5000ms。CSV每100行重复一次Sensor表头（每文件10个表头），审计按真实格式处理，不把表头当作数据。查询首列s_355在[1640968700000,1640978355000)范围内实际非空1931点，而对应真实Native COUNT为390。原因尚未确定，不能据此猜测是导入工具、过滤参数或其他数据库内容变化。

可复现只读审计：

```bash
PYTHONPATH=src /opt/anaconda3/envs/MANTO/bin/python scripts/phase7_check_heavy_import.py --output docs/evidence/phase7_heavy_import_audit_20261102.json
# exit 1；small/medium/large全部MISMATCH（1931 vs 390）
```

证据包含源CSV SHA-256、完整记录数、所查列、窗口、真实reference execution ID及journal SHA-256。只核查首列，不把抽样一致当作全库导入认证。原始CSV未修改。

`bandit/workload_audit.py` 和实验入口新增前置核验：位于标准heavy_scale目录的工作负载在Native参考之后、随机分配与学习之前比较源文件；不一致直接aborted，零学习样本。当前实现只认识生成的COUNT范围形式，其他形式安全拒绝，不能用未经核实的数据集标签进行认证。

这轮分轮采样在发现不一致前已启动，按原定有界轮次收尾。记录仍是当前不变数据库的真实S0/S1观测，不能用于“完整3000点/序列heavy_scale导入成功”的结论。不改写历史VALID记录；额外的数据集验收门槛明确失败，线上部署保持关闭。

## 开销调查边界

一次只读诊断实测身份检查5次为8.179/7.313/7.470/7.214/7.208ms，不能据此认定它是采样瓶颈。对正在增长的经验库做三次只读build_model观测为118.65/110.94/109.88ms（338条记录）；它可能贡献流程开销，但未做完整归因，不修改算法或统计校验以凑样本。诊断会扰动同机资源，不能把本轮视作纯粹无干扰的最终收益试验。

正式结果和最终回归在下方记录。


## 分轮真实结果

24轮全部完成，最长30.722秒；888条测量（train483、holdout405），全部符合既有VALID准入与结果一致性检查。888条均为独立db_host来源的MEDIUM；随机分配重放通过。背景在途计数分布为{'0': 777, '1': 41, '2': 42, '3': 28}，不等同于数据库总并发。

| 单元 | S0 | S1 | 每arm≥30 |
|---|---:|---:|---|
| train/EMPTY/background=0 | 85 | 107 | PASS |
| train/READY/background=0 | 96 | 96 | PASS |
| train/EMPTY/background=3 | 20 | 31 | BLOCKED |
| train/READY/background=3 | 27 | 21 | BLOCKED |
| holdout/EMPTY/background=0 | 70 | 83 | PASS |
| holdout/READY/background=0 | 85 | 90 | PASS |
| holdout/EMPTY/background=3 | 11 | 25 | BLOCKED |
| holdout/READY/background=3 | 17 | 24 | BLOCKED |

无背景训练/留出均满足数量门槛；有背景单元仍不足，未强制补齐或降低门槛。加上三规模pilot，本轮共984条真实随机测量；不同规模、epoch和采样来源分开报告，不合并训练。无完整源数据导入认证，因此不能用这些数字宣布heavy_scale规模验收通过。

原始记录：`data/phase7_heavy_small_dbhost_20261102/journal.jsonl`及同目录experiences.sqlite；报告：[phase7_heavy_small_dbhost_20261102.json](evidence/phase7_heavy_small_dbhost_20261102.json)。离线模型只更新被执行arm；留出冻结，仍未部署。

源数据守卫真实验证：输出`data/phase7_heavy_source_guard_20261102/`，参数为上述命令将`--trials`改为2、不加background/rounds参数；实际exit1，事件仅protocol/reference/source_reference_audit/aborted，assignment=0、experience=0。证据：[phase7_heavy_source_guard_20261102.json](evidence/phase7_heavy_source_guard_20261102.json)。数据不一致是FAIL；守卫成功阻断是PASS，两者不能混淆。

## 复审结论与下一步

| 项目 | 结论 | 证据/剩余工作 |
|---|---|---|
| heavy_scale接入、哈希与多轮随机审计 | PASS | 888条分配逐次重演、24轮完整性 |
| 当前本机DB主机指标来源 | PASS（显式实验范围） | 大规模pilot32条和正式888条有独立db_host快照；默认生产/远程仍UNKNOWN |
| 源CSV与数据库一致 | FAIL | 三规模抽查均1931 vs390；先修复导入或连接映射 |
| 不一致样本进入新heavy学习实验 | PASS：已阻断 | 真实guard运行零assignment、零experience |
| 全部单元样本充足 | BLOCKED | 背景单元部分arm不足30 |
| 多实测负载覆盖 | BLOCKED | DB来源只有MEDIUM；旧client LOW不混入 |
| 可重复自适应端到端收益 | BLOCKED | 仍是固定动作随机对照与离线IPS，未部署 |

阶段7整体仍 **NOT ACCEPTED / NOT READY**。最小后续步骤：确认CSV对应目标实例和导入流程（包括重复Sensor表头）；在独立测试数据epoch内补齐导入；全量或足够覆盖的源/库计数核验通过后，再补背景单元和隔离负载实验。当前只读授权及不变数据契约不允许本轮擅自重导入/删除已有数据库。未修改IoTDB源码、数据或配置，不进入阶段8。


## 实际测试与失败记录

```bash
PYTHONPATH=src /opt/anaconda3/envs/MANTO/bin/python -m pytest -q tests/unit/test_heavy_workload_audit.py tests/unit/test_local_db_sampler.py tests/unit/test_exploration.py tests/unit/test_exploration_model.py tests/unit/test_system_monitor.py tests/unit/test_monitoring_schema.py
# 54 passed in 0.73s

IOTDB_INTEGRATION=1 /opt/anaconda3/envs/MANTO/bin/python -m pytest -q
# 4 failed, 772 passed in 32.74s
# 四项失败均为tests/integration/test_iotdb_connection.py的旧默认
# root.db800.g_0.d_0/d_1查询返回0行；未弱化row_count > 0断言。

IOTDB_INTEGRATION=1 IOTDB_TEST_SQL='SELECT s_355 FROM root.small_scale.g_0.d_1 WHERE time >= 1640968700000 AND time < 1640978355000 LIMIT 5' /opt/anaconda3/envs/MANTO/bin/python -m pytest -q
# 776 passed in 34.37s；含13项真实只读集成，无skipped。

PYTHONPATH=src /opt/anaconda3/envs/MANTO/bin/python -m compileall -q src scripts tests
# exit 0
```

测试配置复用既有IOTDB_TEST_SQL入口，未修改原集成测试。新增13项回归覆盖文件/行号/SQL安全拒绝、重复轮次与工作负载身份、远程/代理/端口/命名空间/PID重启拒绝、DB来源与未知并发数、重复CSV表头、NULL、重复时间戳、截断结果、错误SQL身份和源库计数不一致。首次新增测试调试出现1 failed/14 passed（测试错误期望UnsafeSQLError属于ValueError），后续3 failed/51 passed（测试hash未按既有守卫去分号）；均修正测试构造，未放宽业务安全检查。

最终代码位置：实验文件选择scripts/phase7_exploration.py:31、源数据守卫:164、多轮:185；本机身份核验monitoring/local_db.py:25；源CSV计数bandit/workload_audit.py:9；独立审计scripts/phase7_analyze_exploration.py:16。未新增依赖。仓库业务目录原本均未跟踪，本轮没有将既有整仓工作混入提交；阶段未通过，未宣称阶段提交完成。
