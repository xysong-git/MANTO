# 阶段 7：稳定多负载与实际自适应端到端验证

日期：2026-11-02。此报告仅针对当前阶段受控验证，不进入阶段 8。最终结论：稳定多负载与充分样本 PASS，实际端到端收益 FAIL；阶段 7 **NOT ACCEPTED / NOT READY**。

## 范围与预声明

已有不可变数据 epoch `heavy-complete-20261102-v2`；不写数据库、不改 IoTDB 源码或配置。用户授权每轮最多 30 秒、目标整机 CPU 50%–70% 的同机 CPU 共租负载；它模拟其他应用竞争，不能视为查询自身或 IoTDB 进程独占负载。单查询连接，query timeout 30000ms，进程组监护每轮 60 秒，CPU 控制器超过 80% CPU / 90% 内存停止。

工作负载为 heavy_scale/small_scale/q03_count_aggregation.sql：第 1 条训练，第 2 条留出。Native 完整结果与原始 CSV 统计核对后作为语义基准。仅一个 COUNT 模板，不能推广到全部 24000 条工作负载。

LOW/MEDIUM × EMPTY/READY 四个单元，每单元 6 个随机训练轮次、每轮最多 20 个请求，实际负载/缓存/动作每格至少 30 样本。校准每负载两轮，每轮 20 个观测且至少 80% 落入预设区间。模型冻结后，每单元 30 个随机顺序配对块，每块实际执行 S0、固定 S1、自适应三种工作流。种子 20261102/20261103；训练、测试时间隔离。

每个正式请求仅执行选中策略，实验参考和 READY 准备单独记账。EMPTY/READY 只控制客户端结果缓存，IoTDB 内部缓存状态未控制。三种策略均经过完整 Orchestrator，S0 是 Native SQL 的工作流基线，并非裸 Adapter E0。E2E 包含解析、两次监控窗口、决策、完整消费、学习及 trace 持久化；不含显式准备与外层审计 JSON 写入。

与 S0、固定 S1 分别计算配对耗时差，负数有利于自适应。按四单元分层 bootstrap 4000 次，99% 区间上界小于 0，且连续三组 10 块差值均为负，才通过本会话收益门槛。即便通过，也尚未证明独立会话复现。

## 本轮发现与修复

旧 psutil.cpu_percent(None) 按线程共享基线；同线程 client 后 DB 采样造成近零 DB 窗口。旧正式批次在 LOW 场景出现 91.7% CPU，产生 LOW_CONFIDENCE，reward=null，未更新模型并中止。未删除该记录、未筛掉失败继续认证。

修复为每采样器独立 cpu_times 累积差值，并通过 cpu_measurement 标识及 cpu-activity-v3 协议与旧证据隔离。首样本、零间隔、计数回退/异常为 UNKNOWN。Linux guest/iowait 口径测试及交错采样回归已加入。原资源阈值保持不变。

CPU 生成器首轮因内循环共享 Event 锁争用不能形成目标负载；移除忙循环中的共享锁检查，保留每 50ms 周期停止检查及 30s 自行到期。失败校准日志保留。

## 原始批次（均保留）

- `data/phase7_e2e_cpu_pilot_20261102`：首次校准失败，无模型学习。
- `data/phase7_e2e_cpu_pilot_fixed_20261102`：单采样器校准通过，尚无工作流证据。
- `data/phase7_e2e_admission_smoke_20261102`：80 条训练冒烟，样本不足，未开始留出。
- `data/phase7_e2e_qualification_20261102`：旧共享 CPU 基线导致 LOW_CONFIDENCE，整批中止，不用于新模型。
- `data/phase7_e2e_v3_qualification_20261102`：修复后 466 条训练全部合格，首条留出因新 SQL 首次访问被安全规则改选 S0，整批中止。

- `data/phase7_e2e_v3_repeat_qualified_20261102`：各阶段增加显式 Native 首次访问后从头重新采样；旧批次不复用。

S1 首次访问回退表明安全规则生效；修复实验准备，不放宽 Registry/Validator。首次访问走同一个 Orchestrator，单独记录，不纳入 Bandit 拟合或请求对照；留出首次准备在模型冻结之后。

## 实际命令

```bash
PYTHONPATH=src /opt/anaconda3/envs/MANTO/bin/python scripts/phase7_controlled_e2e.py --output data/phase7_e2e_v3_repeat_qualified_20261102 --epoch heavy-complete-20261102-v2 --local-db-pid 3879777 --immutable-epoch-confirmed --cpu-cotenant-authorized --train-rounds 6 --test-blocks 30
```

## 改动范围

监控与协议：`monitoring/system.py`、`monitoring/activity.py`、`schemas/monitoring.py`、`schemas/learning.py`；旧字段序列化与历史 context key 保持兼容。

实验：`experiments/cpu_load.py`、`experiments/controlled_policy.py`、`experiments/profile_audit.py`、`scripts/phase7_controlled_e2e.py`、`scripts/phase7_analyze_controlled_e2e.py`；既有筛选/探索 CLI 支持新协议。实验模型不发布生产 advice，Registry/Validator 与安全降级仍生效。

回归：CPU 独立基线、v3 隔离、错误样本拒绝、未知策略拒绝、安全 baseline 优先、CPU 有界退出、比较两个实际基线和批次稳定性。文档同步状态、README、METRICS_DICTIONARY 和 LEARNING_DESIGN。


## 实测结果与复审

正式批次 `phase7_e2e_v3_repeat_qualified_20261102` exit 0；分析器独立重建模型与随机分配序列，exit 0。全部 839 个正式请求 VALID/MATCHED：479 训练 + 360 留出，无失败结果过滤。另有显式 reference、缓存准备和首次 Native 访问，均不纳入上述请求数。

| 验收项 | 结论 | 代码位置与实际证据 |
|---|---|---|
| 独立 CPU 测量与 source | PASS | `src/tsdb_optimizer/monitoring/system.py:204`；交错采样回归；v3 不接受无测量标识的旧快照 |
| 稳定多负载 | PASS（本次整机共租场景） | `scripts/phase7_controlled_e2e.py:119`；四轮校准通过；正式请求 LOW 420、MEDIUM 419，目标区间符合率均 100% |
| 充分真实训练样本 | PASS（本模板预声明门槛） | `scripts/phase7_controlled_e2e.py:215`；八个实测负载/缓存/动作单元各 55–65，均 ≥30 |
| 充分实际留出样本 | PASS | `scripts/phase7_analyze_controlled_e2e.py:213`；四场景 × 三策略各 30，共 360 |
| 时间隔离与冻结参数复现 | PASS | `experiments/controlled_policy.py:27`、实验脚本:219/238；独立从 479 记录重建参数与指纹一致，测试无模型更新 |
| Registry/Validator、安全 S0 | PASS（本次路径） | 实验脚本 ExperimentalEngine；未知策略及 baseline 优先回归通过；首次访问真实回退未被规避 |
| 错误/低质量样本不进入模型 | PASS | `experiments/controlled_policy.py:27`；旧批次 LOW_CONFIDENCE reward=null/model_updated=false；新批次不复用旧记录 |
| 单模型 CPU 档位自适应 | BLOCKED：证据未建立 | EMPTY 在 LOW/MEDIUM 均选 S0；READY 在两档均选 S1；不能把两个缓存分区模型的不同选择说成 CPU 自适应 |
| 实际端到端收益 | FAIL | 分析器:251；下表与预声明 99% 配对区间、三批次差值均不支持通过 |
| 独立会话复现与部署 | BLOCKED / NOT READY | 单会话、单模板，无跨会话复现；生产 online_enabled=false，未进入阶段 8 |

| 实际策略 | n | E2E 均值 ms | 中位数 ms | P95 ms |
|---|---:|---:|---:|---:|
| S0 Native 工作流 | 120 | 755.377 | 737.268 | 887.684 |
| 固定 S1 工作流 | 120 | 775.545 | 745.957 | 943.908 |
| 自适应工作流 | 120 | 779.723 | 745.033 | 928.707 |

- 自适应 − S0：均值 **+24.346ms**；99% 配对 bootstrap 区间 **[+0.835,+48.723]ms**；三批次均值差 **+2.194,+18.422,+52.421ms**。本次更慢，不是收益。
- 自适应 − 固定 S1：均值 **+4.179ms**；99% 区间 **[-22.688,+29.897]ms**；三批次 **-14.401,+3.457,+23.481ms**。不能证明稳定改善。
- 序列相关性、共享主机扰动与未知 IoTDB 内部缓存仍是限制；不能将此区间视为跨场景因果保证。

## 耗时定位与下一步最小清单

实际 trace 显示自适应 Adapter client_wall 均值 10.039ms，S0 19.535ms，局部查询计时确实降低；但自适应两个监控区间均值约 254.707ms 与 252.820ms，学习区间约 109.788ms，S0 学习区间约 82.979ms。区间差分包含相邻事件之间的工作，不是 CPU profiler 的独占函数耗时；这些是定位线索，不足以断言全部差异由某一函数造成。

1. 在阶段 7 内，对采样与学习持久化做专项性能剖析，保留来源、新鲜度、完整消费和质量闸门，避免为追求收益删除正确性工作。
2. 检查学习 reward（Adapter 延迟）与实际优化目标（完整请求 E2E）不一致的问题；若改 reward 或策略实现，必须新版本、新训练与留出，不复用本次测试调参后再称独立验证。
3. 在现有数据上寻找可审计动作随运行状态确实发生收益交叉的查询类型；目前没有证据需要扩大数据集。新增动作仍须 Registry/Validator 和语义认证。
4. 上述修复完成后重新预声明多模板、独立会话验证，保持生产部署锁。当前不能进入下一阶段。

## 测试与证据

- 全量（真实 IoTDB 集成开启）：**841 passed in 39.03s**，13 项真实集成，无 skipped。
- 专项：**19 passed in 1.57s**，包含新 CPU 计数、v3 隔离、有界退出、Registry/首次访问/安全回退、实际比较与批次门槛。
- 真实日志副本篡改审计：**5 passed in 19.61s**；删除终态、改随机种子、重复首次访问、伪造实际策略、伪造模型指纹均被拒绝。原始日志未改。
- `python -m compileall -q src scripts tests`：exit 0。
- 核心修复后的早期全量 **835 passed in 37.45s**；新增实验准备及统计回归后最终为上述 841。测试没有删除或降低断言。

```bash
IOTDB_INTEGRATION=1 IOTDB_TEST_SQL='SELECT s_355 FROM root.small_scale.g_0.d_1 WHERE time >= 1640968700000 AND time < 1640978355000 LIMIT 5' /opt/anaconda3/envs/MANTO/bin/python -m pytest -q
PYTHONPATH=src /opt/anaconda3/envs/MANTO/bin/python -m pytest -q tests/unit/test_controlled_study.py tests/unit/test_controlled_analysis.py tests/unit/test_cpu_counter_isolation.py tests/unit/test_load_protocol.py
PYTHONPATH=src /opt/anaconda3/envs/MANTO/bin/python -m pytest -q docs/evidence/phase7_controlled_audit_probes.py
PYTHONPATH=src /opt/anaconda3/envs/MANTO/bin/python scripts/phase7_analyze_controlled_e2e.py data/phase7_e2e_v3_repeat_qualified_20261102/journal.jsonl --output docs/evidence/phase7_controlled_e2e_20261102.json
```

[完整分析 JSON](evidence/phase7_controlled_e2e_20261102.json) 包含原始日志 SHA256、参数、每单元计数、实际策略选择、阶段耗时和区间；[历史尝试摘要](evidence/phase7_controlled_prior_attempts_20261102.json) 保留所有失败/不足尝试的路径与哈希。未改写历史报告，也未将整仓未跟踪项目提交为本轮改动。
