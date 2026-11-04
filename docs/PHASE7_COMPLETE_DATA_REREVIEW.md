# 完整 heavy_scale 数据上的修复复审

日期：2026-11-02。阶段7整体 **NOT ACCEPTED / NOT READY**。本轮修复两个分析器缺陷，补齐一个查询、无背景并发条件下的真实训练/留出样本；没有启用生产模型或进入阶段8。

## 修复与证据

1. **FAIL → PASS：整组实验单元缺失可漏报。** 原分析器由已出现的 cells 推导预期轮数，无法识别整组删除。`scripts/phase7_exploration.py:146` 现在在分配前写入 `background_workers_planned`；`scripts/phase7_analyze_exploration.py:52` 起将分配种子绑定协议，且要求协议先于分配；`:82` 起核验完整 train/holdout × EMPTY/READY × 预声明背景数。旧日志没有预声明字段时明确报告 `PLANNED_EXPERIMENT_COVERAGE_NOT_ATTESTED`，不补写历史日志。回归：`tests/unit/test_exploration.py:203`、`:235`。
2. **FAIL → PASS：不合格或极少数负载样本可解除多负载门槛。** 第一批真实数据中出现1条 CPU=99.4%的HIGH记录，Learning质量闸门正确标为LOW_CONFIDENCE；原分析器却只数出现过几个load_bucket。现在 `scripts/phase7_analyze_exploration.py:17` 仅用VALID且非UNKNOWN样本核验每负载档位的训练/留出两臂各≥30，并保留全部原始观测。存在任何不合格随机试验时，报告明确阻止策略评估，不删除坏样本后运行IPS。回归：`tests/unit/test_exploration.py:258`。

未修改IoTDB源码、原SQL、资源阈值、学习奖励、Registry、Validator或线上策略开关。

## 本轮真实实验

数据epoch均为 `heavy-complete-20261102-v2`；工作负载为 `query/heavy_scale/small_scale/q03_count_aggregation.sql` 第1行；证书与源CSV/Native COUNT校验通过。单次超时30秒、每轮60秒上限、30秒派发窗口；本轮未启用背景连接。

- 第一批：`data/phase7_heavy_complete_review_20261102/journal.jsonl`，580条，579 VALID/1 LOW_CONFIDENCE。MEDIUM579/HIGH1。启动于协议修复前，不追溯补认证；不合格样本使整批策略评估关闭。分析：[原批报告](evidence/heavy_complete_review_20261102.json)。
- 修复后独立重新采集：`data/phase7_heavy_planned_review_20261102/journal.jsonl`，**364条全部VALID，191训练/173留出**，全部MEDIUM；预声明单元覆盖、逐次p=0.5随机日志、时间隔离和冻结模型校验通过。最长一轮30.362秒。分析：[新协议报告](evidence/heavy_planned_review_20261102.json)。

| 单元（背景连接0） | S0 | S1 | 每arm≥30 |
|---|---:|---:|---|
| train/EMPTY | 46 | 50 | PASS |
| train/READY | 51 | 44 | PASS |
| holdout/EMPTY | 42 | 51 | PASS |
| holdout/READY | 42 | 38 | PASS |

不将两批拼接，也不与旧epoch或client LOW混算。样本门槛PASS仅适用于上述单查询/单负载/无背景四单元，不代表24,000条SQL或不同规模均已通过。IPS为离线估计，不是实际执行自适应策略的端到端收益；service_wall_ms不包含全部monitor/learner开销。共享主机存在其他活动，本轮短单元测试也与采集时间重叠，不能声称环境完全受控或独占。

## 逐项验收

以下源文件路径以仓库根目录为基准；测试为本轮全量回归及上述专项。

| 检查 | 判定 | 代码位置/证据与边界 |
|---|---|---|
| 阶段所有任务真实实现 | BLOCKED | `IMPLEMENTATION_GUIDE.md:967`；新协议报告仍仅一个合格负载档位，部署认证与实际自适应收益未完成 |
| TODO/pass/mock假实现 | PASS（离线范围） | `src/tsdb_optimizer/bandit/model.py:251`为显式部署锁；`decision/llm_advisor.py:42`无provider返回UNAVAILABLE；未将模拟作为真实样本 |
| 字段、单位、时间戳、source | PASS | `bandit/exploration.py:18`、`:40`、`:46`，`bandit/context.py:7`；ms与UTC验证，前置分配及cache/result_source一致性测试 |
| 异常吞掉 | PASS（已审路径） | `bandit/evaluation.py:38`仅捕获预期拒绝；`bandit/store.py:35`回滚重抛；实验aborted保留失败，未通过删除坏样本认证 |
| 未验证IoTDB API/SQL | PASS（2.0.6/tree） | `adapters/iotdb/tree_adapter.py:539`；原Native COUNT真实执行，导入证书通过；不推广未知版本 |
| client冒充DB | PASS | `monitoring/local_db.py:25`、`:64`验证DataNode、监听端口、命名空间并独立采样；整机CPU不是IoTDB独占CPU，active_queries为UNKNOWN |
| LLM绕过Registry/Validator | PASS | `decision/llm_advisor.py:45`、`:58`、`:60`；`execution/executor.py:304`中央验证；未知ID/额外SQL字段/超时测试 |
| 错误样本正向学习 | PASS；分析器门槛缺陷已修 | `bandit/model.py:202`质量准入；真实HIGH坏样本被排除；分析报告明确禁止该批IPS评估；质量拒绝回归 |
| 普通查询调用EXPLAIN ANALYZE | PASS | `adapters/iotdb/tree_adapter.py:512`、`:790`；普通路径禁止，真实workflow及never_analyze测试 |
| 双执行、缓存污染、未消费结果 | PASS（不可变epoch契约内） | `adapters/iotdb/tree_adapter.py:614`、`:645`完整消费并关闭；实验reference/READY预热单独记录，不冒充正式单次查询或奖励样本；超时不盲重试 |
| 测试真实运行/失败覆盖 | PASS | 新增3项测试覆盖缺单元、seed偏离、协议顺序、旧日志限制、不合格/稀疏负载；全量包含13项真实集成；实际结果见下 |

业务文件省略的共同前缀为 `src/tsdb_optimizer/`。未接入实际LLM，开关收益对照未做；LLM保持关闭，属于可选增强限制，不以此单独阻塞无LLM系统。

## 实际命令

```bash
# 第一批（启动于协议修复前）
PYTHONPATH=src /opt/anaconda3/envs/MANTO/bin/python scripts/phase7_exploration.py --output data/phase7_heavy_complete_review_20261102 --epoch heavy-complete-20261102-v2 --immutable-epoch-confirmed --sql-file query/heavy_scale/small_scale/q03_count_aggregation.sql --local-db-pid 3879777 --trials 96 --rounds-per-cell 2
# 第二批（新协议）
PYTHONPATH=src /opt/anaconda3/envs/MANTO/bin/python scripts/phase7_exploration.py --output data/phase7_heavy_planned_review_20261102 --epoch heavy-complete-20261102-v2 --immutable-epoch-confirmed --sql-file query/heavy_scale/small_scale/q03_count_aggregation.sql --local-db-pid 3879777 --trials 96 --rounds-per-cell 1
# 两次运行均exit 0；重跑应使用新的output目录并重新验证PID。
PYTHONPATH=src /opt/anaconda3/envs/MANTO/bin/python scripts/phase7_analyze_exploration.py data/phase7_heavy_complete_review_20261102/journal.jsonl --output docs/evidence/heavy_complete_review_20261102.json
PYTHONPATH=src /opt/anaconda3/envs/MANTO/bin/python scripts/phase7_analyze_exploration.py data/phase7_heavy_planned_review_20261102/journal.jsonl --output docs/evidence/heavy_planned_review_20261102.json
# 两次分析exit 0；exit 0仅表示报告生成，不表示阶段通过。
PYTHONPATH=src /opt/anaconda3/envs/MANTO/bin/python -m pytest -q tests/unit/test_exploration.py tests/unit/test_exploration_model.py tests/unit/test_heavy_workload_audit.py tests/unit/test_heavy_import_repair.py
# 33 passed in 0.72s
IOTDB_INTEGRATION=1 IOTDB_TEST_SQL='SELECT s_355 FROM root.small_scale.g_0.d_1 WHERE time >= 1640968700000 AND time < 1640978355000 LIMIT 5' /opt/anaconda3/envs/MANTO/bin/python -m pytest -q
# 789 passed in 34.58s；包含13项真实集成，无skipped
/opt/anaconda3/envs/MANTO/bin/python -m compileall -q src tests scripts
# compileall exit 0
```

## 未完成与单机约束

剩余：`CONTROLLED_PREACTION_MODEL_NOT_DEPLOYMENT_CERTIFIED`、`ADAPTIVE_POLICY_BENEFIT_NOT_ESTABLISHED`、`INSUFFICIENT_MEASURED_LOAD_BUCKET_COVERAGE`。不能直接进入阶段8。

用户已明确仅当前电脑及现有数据，允许自行安排资源；不再要求另一台机器。只读核实本机64逻辑CPU、125GiB内存，现有DataNode RSS约63.46GB，位于浏览器启动的用户scope；用户级cgroup有cpu/memory/pids控制器，配置正确总线环境后可查询systemd。未移动、限制或重启现有DataNode。初次沙箱内看不到PID、未设置总线环境时报No medium found，均未误判为服务不存在。

当前负载算法（`schemas/learning.py:58`）取CPU/内存/磁盘占用率最大值，整机内存约64%使LOW难以出现；HIGH>90又触发既有质量闸门。因此增加并发并不能证明可学习的多负载。下一步须在单机上设计独立实例及**明确资源范围的监控协议**，验证数据隔离、指标分母及安全阈值后再采集；不能直接把cgroup使用率改名为现有DB整机指标，也不能为通过验收修改阈值。当前可用控制器不等于已完成隔离实验。

改动文件：两个phase7实验脚本、`tests/unit/test_exploration.py`、本报告、两个JSON证据、README、IMPLEMENTATION_STATUS及历史验收页的最新报告链接。未将整个既有未跟踪项目一并提交Git。
