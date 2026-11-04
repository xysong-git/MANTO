# 阶段 7：监控、决策与学习开销定位及修复

日期：2026-11-03。限于当前阶段，不进入阶段 8，不启用生产 Bandit。

## 定位与修改

上一轮完整端到端自适应均值 779.723ms，Adapter 均值仅 10.039ms。trace 中两个监控区间约 254.707/252.820ms，学习区间约 109.788ms。旧实验 CheckedMonitor 每次同步采两次并等待 0.2s，一条请求有前后两次该调用，固定等待约 400ms；这些是实验路径的同步等待，不能泛化为所有生产配置都固定耗费 400ms。

函数剖析的 30 个正式请求及其准备路径共约 1200 次 SQL 只读检查。原始文件 `data/phase7_overhead_before_20261103.prof` / `.profile.txt` 保留；该带剖析的 CPU 共租实验触发保护并中止，只定位调用热点，不用其受干扰的耗时认证收益，也不将并发/子进程累计时间当作独占函数耗时。驱动见 `docs/evidence/phase7_overhead_before_driver.py`。

本轮最小实现：

1. `request_memo.py`、`sql_guard.py`、`parsing/sql_parser.py`、`orchestration/orchestrator.py`：单同步请求内、精确 SQL 的确定性事实复用。每类至多 32 项、SQL 长度上限 65536；请求结束/异常立即清理。只读检查返回不可变文本；解析树保留私有深拷贝，每次返回新副本并绑定当前 query_id。解析实现/版本不同不会复用。未知/失败解析不缓存。没有跨请求 SQL 结果缓存，也没有缓存 Registry 资格、隔离状态、系统状态或策略建议；各层动态校验仍执行。
2. `learning/repository.py`：完整原始 JSON 为键，先通过原有 Experience Pydantic 校验，再缓存不可变统计投影。最多 128 条，超过 262144 字符的记录不缓存。记录/隔离内容改变即失效；模型发布仍重新计算统计并核对事务 revision。没有删除失败样本或绕过模型伪造检测。
3. `agents/performance_monitor.py`、`monitoring/system.py`：新增显式 latest 模式读取后台快照深拷贝；不伪造采样时间，不同步等待，不在请求内重新采硬件。采样线程未运行/快照缺失显式报错；过期值保留原时间戳，由现有 Orchestrator/Executor/学习质量闸门处理。线程停止超时保留线程身份，防止误认为已停而重复启动。
4. `bandit/model.py`：greedy/no-exploration 预测不再求解未使用的置信上界；均值、reward、模型参数、选择与 Registry 规则不变。
5. `scripts/phase7_controlled_e2e.py`：新的显式 `--background-snapshots`，后台周期 0.2s、快照有效期 500ms；准备与初始化独立记账。最终协议 controlled-e2e-v5，CPU 测量仍 cpu-activity-v3。最终 `--test-query-line 4` 使用另一条留出 SQL（此前未完成批次为第3条）；训练仍第 1 条且全部重新采集。其他样本、统计、安全门槛不变。

后台读取的 after 字段表示执行后读取到的最新快照，可能与 before 为同一 snapshot_id，也可能采样于查询完成之前；必须按 captured_at 解读，不能将其描述成查询结束后的专门硬件采样。它不产生查询级 IoTDB 指标。

## 安全问题与中止批次

初次后台模式产生 Python 多线程 fork 警告。主动 SIGINT 中止已核对身份的实验子进程，记录 KeyboardInterrupt/aborted，经上下文清理 CPU workers 与采样器。214 个已完成 trial 不用于最终认证，未查看留出收益后筛选批次。

修复启动顺序：每轮负载创建前停止并 join 采样线程，确认停止后 fork CPU workers，随后恢复后台采样，保持 3 秒负载稳定准备。控制线程在 CPU workers 创建后启动。相关生命周期回归覆盖停止失败、不可 fork、恢复次序；不以屏蔽 warning 处理问题。

两批中止证据见 `docs/evidence/phase7_overhead_aborted_20261103.json`，原日志全部保留。

另一次 `phase7_e2e_overhead_fixed_20261103` 已完成 480 条训练，但模型冻结后的首次 Native 准备命中客户端快照过期：client captured_at=01:10:39.101188 UTC，实际执行开始 01:10:39.685577 UTC（约584ms），超过500ms；DB snapshot 当时约468ms。正确回退并中止，原日志保留。补充阶段准备时最多2秒的就绪检查，要求两个来源最新快照年龄均≤250ms，单独记 monitor_ready；正式请求不等待、不重试、不放宽500ms TTL。

第三次 `phase7_e2e_overhead_ready_20261103` 在第25组（0基block=24）出现客户端快照过期，按预声明中止。before.client captured_at=01:21:27.122003 UTC，DB captured_at=01:21:27.413236 UTC，执行开始01:21:27.735515 UTC。先采client、再慢DB身份核验使发布时client已旧约291ms；加上轮次后固定等待0.2s，最坏年龄超过500ms。修复为DB先采、client后采、整体样本原子发布，并按monotonic固定周期调度（过载不追赶多个旧周期）。500ms TTL、两次DB身份核验不变。新协议明确 fixed-rate-db-first-v1。

第四次 `phase7_e2e_periodic_20261103` 出现执行前快照年龄越界：决策引擎6.962ms，但包含决策持久化的区间约286ms；该请求为S0，原始查询仍成功，学习明确 LOW_CONFIDENCE / UNKNOWN，不更新模型，整批中止。定位持久化长尾后增加下述WAL模式，不放宽TTL或样本门槛。

## L1 实际历史记录重放

```bash
PYTHONPATH=src /opt/anaconda3/envs/MANTO/bin/python scripts/phase7_profile_learning_cost.py data/phase7_e2e_v3_repeat_qualified_20261102/study.sqlite --output docs/evidence/phase7_learning_cost_20261103.json
```

四个最大真实经验组，每组两种实现交替 12 次；只读，不生成新的 reward，不更新历史模型。每次统计、样本 ID、revision 均完全一致。

| 组 | 原完整反序列化中位 ms | 内容缓存中位 ms |
|---|---:|---:|
| 1 | 28.317 | 3.149 |
| 2 | 28.636 | 3.204 |
| 3 | 22.390 | 2.899 |
| 4 | 21.576 | 2.765 |

这是统计重建开销对照，不是查询收益或自适应收益证据。

## 新的真实端到端验证

```bash
PYTHONPATH=src /opt/anaconda3/envs/MANTO/bin/python scripts/phase7_controlled_e2e.py --output data/phase7_e2e_wal55_20261103 --epoch heavy-complete-20261102-v2 --local-db-pid 3879777 --immutable-epoch-confirmed --cpu-cotenant-authorized --train-rounds 6 --test-blocks 30 --background-snapshots --wal-full --test-query-line 4 --cpu-target-percent 55
```

使用既有不可变数据与授权 CPU 共租负载；每轮 CPU 最多 30s、查询连接 1、查询 timeout 30s、进程组轮次 60s，80% CPU/90% 内存保护保持原样。共租指标是整机状态，不是 IoTDB 进程独占值。两种负载 × 两种客户端缓存状态；每训练动作至少 30 样本；每场景 30 块实际 S0/S1/Adaptive 随机对照。

收益仍要求相对两个固定策略的 99% 分层配对区间上界 <0，三个连续 10 块均值差均 <0。新查询、新采样方法下本轮与旧轮的绝对均值仅为描述性比较，不是同条件因果估计；本轮三策略才是同期实际收益对照。

## 回归

- 初轮解析/学习：165 passed in 10.68s。
- 请求缓存、监控、工作流、实验：43 passed in 5.90s。
- 线程启动与预测修复：25 passed in 1.53s。
- 完整回归：851 passed in 37.23s，包含真实 IoTDB 集成 13 项，无 skipped。

```bash
IOTDB_INTEGRATION=1 IOTDB_TEST_SQL='SELECT s_355 FROM root.small_scale.g_0.d_1 WHERE time >= 1640968700000 AND time < 1640978355000 LIMIT 5' /opt/anaconda3/envs/MANTO/bin/python -m pytest -q
```

最终实测已完成；结果与验收结论见下节。


## SQLite 持久化长尾修复

`storage/sqlite.py` 增加显式 `wal_full=True`，本机 ext4、SQLite 3.53.2 实测设置并回读 journal_mode=wal、synchronous=2。只在该模式启用请求内连接复用：每个 `_connect` 仍独立 commit/rollback，不把事务推迟到响应之后；连接最终关闭/检查点时间包括在外层 E2E 中。串行 owner 明确，嵌套 session 拒绝，跨线程不共享活动连接。进程启动前没有活动请求连接。默认存储行为保持兼容，现有生产数据库未切换模式。

依据 [SQLite WAL](https://www.sqlite.org/wal.html) 与 [synchronous](https://www.sqlite.org/pragma.html#pragma_synchronous) 官方说明选择 WAL + FULL，未采用 NORMAL/OFF。仍依赖文件系统和硬件正确实现同步，不能声称做过真实断电测试。新增测试验证外部连接可见已提交数据、异常回滚、连接关闭、完整性，以及清理异常不会丢失成功查询结果而是返回明确 warning。

合成的本机磁盘探针：每请求10次独立FULL提交、两模式交替12轮，中位数 LEGACY 42.721ms / WAL_FULL 16.372ms；各120条落盘与 integrity_check=ok。这是局部持久化测试，不是IoTDB或自适应收益。原始结果 `data/phase7_storage_cost_20261103/report.json`，复现：

```bash
PYTHONPATH=src /opt/anaconda3/envs/MANTO/bin/python scripts/phase7_profile_storage_cost.py --output data/phase7_storage_cost_NEW
```

WAL/工作流/学习/分析专项72 passed in19.39s；最终存储实现全量859 passed in38.04s（真实集成13项，无skipped）。


WAL第一次完整验证 `phase7_e2e_wal_20261103` 在block=11的MEDIUM/EMPTY准备期间触发CPU保护：控制器观测63.0%后到81.8%，当时worker duty约0.759，负载尚未进入本块查询。未定位突发竞争的具体进程，不作归因。按预声明中止，不复用其模型或样本进行认证。

最终验证预声明CPU设定点55%，仍使用50%–70%合格区间与80%停止线、30秒CPU上限，目的是保留突发竞争余量，不重新分类样本。若环境仍触发停止，就维持未完成/不认证，不用删除失败记录取得通过。


## 最终独立分析与验收

实验命令 exit 0；独立分析命令 exit 0：

```bash
PYTHONPATH=src /opt/anaconda3/envs/MANTO/bin/python scripts/phase7_analyze_controlled_e2e.py data/phase7_e2e_wal55_20261103/journal.jsonl --output docs/evidence/phase7_overhead_e2e_20261103.json
```

| 验收项 | 结论 | 实际证据 |
|---|---|---|
| 完整执行与质量 | PASS | 480训练+360留出，全部VALID/MATCHED；模型独立重建一致 |
| 稳定多负载 | PASS | LOW/MEDIUM各420观测，目标稳定率均99.524% |
| 充分训练样本 | PASS | 每实际负载×缓存×动作55–65条 |
| 充分留出样本 | BLOCKED | MEDIUM/READY/ADAPTIVE仅29；LOW/READY/ADAPTIVE为31，其余30 |
| 快照时效 | PASS | client最大290.493ms、DB最大359.910ms，均低于500ms |
| 实际自适应额外收益 | BLOCKED | 样本门槛未过，正式contrasts为空；120次均选择S1 |
| 阶段7总验收 | NOT ACCEPTED | 未证明优于两个固定策略，不启用部署 |

同期描述性结果（各120条）：

| 策略 | E2E均值 ms | 中位 ms | P95 ms |
|---|---:|---:|---:|
| S0 | 78.054 | 74.752 | 113.780 |
| 固定S1 | 71.147 | 60.070 | 130.233 |
| 自适应 | 73.379 | 61.760 | 144.062 |

自适应trace区间均值：解析14.533ms、前快照读取0.292ms、检索1.281ms、决策6.595ms、校验0.278ms、执行16.639ms、后监控0.838ms、学习27.228ms；Adapter本身10.403ms。区间包含对应阶段落盘，不等于纯函数CPU时间；总E2E另包含请求连接开启/关闭。监控的后台成本未被消灭。旧轮779.723ms与新轮73.379ms使用不同SQL/协议，不能宣称受控的90%以上因果收益。

未完成事项：当前学习reward仍使用Adapter延迟，完整工作流含解析、决策与学习成本；这是一项目标一致性限制。当前留出工作负载上模型全部选择S1，自适应比固定S1多出约2.232ms的描述性均值，尚无额外收益证据。下一步需在预声明的新实验中验证经Registry/Validator审计的动作收益交叉及E2E奖励一致性，而不是追加一个样本、扩大CPU或数据集来争取过门槛。

证据篡改回归：`PYTHONPATH=src /opt/anaconda3/envs/MANTO/bin/python -m pytest -q docs/evidence/phase7_overhead_audit_probes.py`：**8 passed in 35.84s**。覆盖真实报告的29条门槛，以及TTL、同步等级、缺失存储配置、越界CPU设定点、模型摘要、aborted终态、缺失完成终态的拒绝路径。仅修改日志副本。

最终工作树回归：**859 passed in 36.93s**（含13项真实IoTDB集成，无skipped）；`python -m compileall -q src scripts tests` exit 0。最终实验SQLite `integrity_check=ok`，10项运行源码摘要与实验启动时一致。摘要见 `docs/evidence/phase7_overhead_artifact_manifest_20261103.json`。
