# 阶段7：CPU活跃协议与现有数据负载筛选

日期：2026-11-02。**测量修复与有界筛选完成；阶段7仍 NOT ACCEPTED / NOT READY。** 未进入阶段8，未扩大数据集、修改IoTDB源码或调整服务配置、资源安全阈值。

## 已实现

- `src/tsdb_optimizer/schemas/learning.py:44`：新增显式`load_protocol`。历史`resource-max-v1`仍默认，序列化省略默认字段，旧JSON/context key不变。`cpu-activity-v2`只按CPU分档，阈值仍为50%/90%；内存与磁盘容量不参与CPU标签。新协议显式进入L1与Bandit分区。
- L1质量准入、Orchestrator检索/反馈、Bandit真实样本准入统一按上下文协议重算；未知协议拒绝，旧新协议不可混合。资源安全策略与奖励不变。
- `scripts/phase7_exploration.py`支持`--load-protocol cpu-activity-v2`，执行前在同一采样线程建立基线，等待0.2秒后采样，窗口写入协议。Analyzer验证每条Experience协议一致。
- `scripts/phase7_profile_load.py`通过Registry/Validator的S0路径筛选已有SQL，最多4连接、每查询30秒超时、每轮60秒预算，完整消费结果，独立SQLite记录。最终脚本按1→2→4连接递增，每阶段固定种子随机排序。失败停整批，无自动重试。
- `ActivityObservation`为Pydantic协议：DB整机快照、CPU标签、采样时间与经过秒数；内存压力、磁盘饱和度明确UNKNOWN。内存占用与磁盘I/O速率仍按原快照分别记录。筛选无LearningFeedbackAgent，不生成训练奖励。
- `scripts/phase7_summarize_load_profile.py`保留失败批与完整批差异，输出原始日志SHA；首版日志包含computed valid_until，读取时核验与真实时间戳/TTL一致后兼容，后续写入使用round_trip=True。

## 实测结果

均使用已认证的 `heavy-complete-20261102-v2` 数据epoch。SQL均取现有文件第1行，没有任意改写。

| 实验 | 实际结果 | 证据 |
|---|---|---|
| 首轮大规模q05、4连接 | 4次ERROR，IoTDB明确报查询内存不足；整批停止，未重试 | [失败轮](evidence/phase7_cpu_profile_failure_20261102.json)、[SQLite原始错误](evidence/phase7_cpu_profile_error_details_20261102.json) |
| 三规模COUNT/SUM，1/2/4连接，每组合2轮 | 36轮、84次成功执行；64个during CPU采样全部LOW，2.5%～49.8% | [COUNT/SUM报告](evidence/phase7_cpu_profile_counts_20261102.json) |
| 小/中规模q05/q06分组，1/2/4连接，每组合2轮 | 24轮、56次成功执行；53个during CPU采样中52 LOW/1 MEDIUM，最高50.9% | [分组报告](evidence/phase7_cpu_profile_groups_20261102.json) |
| v2 S0/S1随机冒烟，0/3背景连接 | 64条全部VALID，32训练/32留出；执行前CPU全部LOW；最长轮25.931秒，协议/随机/导入/DB身份校验PASS | [v2冒烟报告](evidence/phase7_cpu_v2_pilot_20261102.json) |

初轮真实错误示例：`301: Query is aborted since it requests more memory than can be allocated, bytesToReserve: 2148040B, maxBytesCanReserve: 1383523B`。不能将此解释为整机内存用尽，也不能推断未核实的IoTDB配置项。适配器保守记录request_completed=false/retry_safe=false；未盲重试原SQL。后续为不同SQL，启动时已超过30秒；不能声称拥有服务端活动查询数或取消确认。

首版筛选把所有连接级别统一打乱，因而首轮就是大规模4连接；失败后修正为逐级并发，原失败日志不覆盖。COUNT/SUM完成后才继续不同的小/中规模分组查询，未重新执行被拒绝的大规模q05。

上述140次成功筛选证明部分查询可执行，**不证明CPU负载由IoTDB独占造成，也不证明两个稳定档位**。采样包括客户端解析与共享主机其他活动；短查询可能落在采样间隔内。专项单元测试曾与v2冒烟部分时段重叠，不能声称独占受控主机。before标签与during峰值不得混用。v2的64条是入口冒烟，不满足每单元每arm≥30；旧v1的364条不得合并补足。

## 实际命令与测试

仓库根目录，Python `/opt/anaconda3/envs/MANTO/bin/python`；实际使用本机DataNode PID 3879777，重跑须重新确认身份并使用新输出目录。

```bash
# 第一批（初版全局打乱，失败退出1，保留）
PYTHONPATH=src /opt/anaconda3/envs/MANTO/bin/python scripts/phase7_profile_load.py --output data/phase7_cpu_profile_20261102 --epoch heavy-complete-20261102-v2 --immutable-epoch-confirmed --local-db-pid 3879777 --repeats 2
# 修正为逐级并发后的两个批次，均exit0
PYTHONPATH=src /opt/anaconda3/envs/MANTO/bin/python scripts/phase7_profile_load.py --output data/phase7_cpu_profile_counts_20261102 --epoch heavy-complete-20261102-v2 --immutable-epoch-confirmed --local-db-pid 3879777 --repeats 2 --types q03_count_aggregation q04_sum_aggregation
PYTHONPATH=src /opt/anaconda3/envs/MANTO/bin/python scripts/phase7_profile_load.py --output data/phase7_cpu_profile_groups_20261102 --epoch heavy-complete-20261102-v2 --immutable-epoch-confirmed --local-db-pid 3879777 --repeats 2 --scales small_scale medium_scale --types q05_group_by_time q06_group_by_time_value_filter
# 新协议随机冒烟，exit0
PYTHONPATH=src /opt/anaconda3/envs/MANTO/bin/python scripts/phase7_exploration.py --output data/phase7_cpu_v2_pilot_20261102 --epoch heavy-complete-20261102-v2 --immutable-epoch-confirmed --sql-file query/heavy_scale/small_scale/q03_count_aggregation.sql --background-sql-file query/heavy_scale/medium_scale/q06_group_by_time_value_filter.sql --local-db-pid 3879777 --trials 8 --rounds-per-cell 1 --background --load-protocol cpu-activity-v2
PYTHONPATH=src /opt/anaconda3/envs/MANTO/bin/python scripts/phase7_analyze_exploration.py data/phase7_cpu_v2_pilot_20261102/journal.jsonl --output docs/evidence/phase7_cpu_v2_pilot_20261102.json
# 三个筛选批次分别调用同一汇总脚本，输出对应evidence JSON
PYTHONPATH=src /opt/anaconda3/envs/MANTO/bin/python scripts/phase7_summarize_load_profile.py data/phase7_cpu_profile_counts_20261102/journal.jsonl --output docs/evidence/phase7_cpu_profile_counts_20261102.json
PYTHONPATH=src /opt/anaconda3/envs/MANTO/bin/python -m pytest -q tests/unit/test_load_protocol.py tests/unit/test_exploration.py tests/unit/test_exploration_model.py tests/unit/test_linucb.py tests/unit/test_learning.py tests/unit/test_orchestrator.py
# 106 passed in 13.05s
IOTDB_INTEGRATION=1 IOTDB_TEST_SQL='SELECT s_355 FROM root.small_scale.g_0.d_1 WHERE time >= 1640968700000 AND time < 1640978355000 LIMIT 5' /opt/anaconda3/envs/MANTO/bin/python -m pytest -q
# 796 passed in 31.23s，含13项真实集成，无skipped
/opt/anaconda3/envs/MANTO/bin/python -m compileall -q src tests scripts
# exit0
```

开发失败记录：首次专项命令引用不存在的test_learning_feedback.py，pytest exit4无测试运行；纠正为test_learning.py。新增测试首次3失败来自合成SystemSnapshot漏填缺失字段原因，修正测试fixture后通过，未放松Pydantic约束。首版汇总遇到computed字段extra_forbid错误，加入严格expiry一致性检查与回归后，三批真实日志均可汇总。所有既有测试保留。

## 验收与下一步

- PASS：CPU活动与容量占用拆分；旧新协议隔离；安全阈值不变；真实筛选和失败可追溯；Registry/Validator、完整消费、无默认ANALYZE、无错误样本正向学习路径保持。
- BLOCKED：两个稳定且样本充分的执行前负载状态；v2充分策略样本；实际执行的自适应端到端收益；部署认证。仍online_enabled=false。
- 本轮不扩大数据集。现有规模已能触发查询内存配额拒绝，单纯加数据并不能解决测量与可学习场景问题。
- 下一步需验证DataNode进程/查询级资源观测（或独立实例与明确资源范围采样），区分数据库执行、客户端完整拉取以及查询内存限制；然后预先确定可重复场景。不能只把阈值调低、将唯一峰值命名HIGH，或放宽质量门槛。尚未形成合格场景，因此没有执行充分自适应收益对照；不以冒烟或离线IPS替代该步骤。

改动范围：schemas/learning、agents/learning_feedback、orchestration/orchestrator、bandit/model；两个既有实验脚本、两个新筛选/汇总脚本；test_load_protocol；指标/学习/状态/README文档及本报告和5个证据JSON。没有Git提交整个既有未跟踪项目。
