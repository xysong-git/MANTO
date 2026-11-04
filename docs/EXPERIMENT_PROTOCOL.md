# 阶段 8：可复现 Benchmark 与消融协议

2026-11-04后续最小修复：E4/A1日志复用同次建议携带的模型指纹，避免重复整库哈希；无跨请求缓存，算法、协议、统计与完整计时不变。此前两次公平性修复后真实复现已完成，但本次源码再次变化，不能据此认证新性能。当前复跑与扩展执行顺序见[指纹成本修复](PHASE8_INFERENCE_COST_FIX.md)。

协议版本：`phase8-request-e2e-v1`，2026-11-03。

2026-11-04公平性修复：E0/E1/E2旧服务与Orchestrator均采用相同请求内解析复用及既有SQLite连接会话；全部验证、逐操作提交和完整外层E2E保持，内部ExecutionReport历史截点不变。旧真实结果保留但不认证新实现性能。新实验/重复会话与显式CPU加压扩展配置见[修复与验证步骤](PHASE8_FAIRNESS_FIX.md)；尚未取得修复后的真实收益证据。

用户明确指定推进阶段 8，本轮交付独立实验工具。阶段 7 的 **NOT ACCEPTED**、F2 额外收益未通过及生产 **NOT READY** 原结论保留。阶段 8 工具可运行不等于真实实验或研究结论通过。助手只执行非 IoTDB 测试；真实实验由用户运行。

## 1. 固定实验输入

入口为 `benchmarks/run_benchmark.py`；JSON 配置为 `benchmarks/workloads/small_count.json`。
配置由 Pydantic 校验并随运行保存，任何未声明字段均拒绝。

- 数据集：`heavy-complete-20261102-v2`，树模型 IoTDB 2.0.6；绑定完整 manifest SHA-256、导入证书和 endpoint。用户声明实验期间数据不可变。
- SQL：现有 small-scale COUNT 工作负载第 101 行训练、第 102 行留出。完整 SQL 模板、整数字面量参数和解析后的 SQL SHA-256 固定在配置中。只允许整数参数替换，不接受标识符或 SQL 片段参数；最终仍通过现有只读守卫。
- `expected_rows=1`，`fetch_mode=FULL`。运行前独立 Native 参考查询必须完整返回预声明行数；所有测量与已保存参考比对，不为每个请求再次执行 Native。
- 不承诺全部 SQL 方言。每条 SQL 必须通过当前 Parser；未支持语法不进入该学习实验。完整结果最多 1000 行，超出/预览截断则拒绝正确性认证。
- 训练和留出 SQL hash 全局不相交；训练先完成、模型冻结后才进入留出。参考查询不用于训练。
- 训练/测试轮数、warmup、每通道请求数、seed、random/ABBA、并发 1–4、负载条件均进入配置与 plan hash。运行时保存源码/策略配置摘要、实际依赖版本，并在结束前检查源码未变化。

当前示例只覆盖一个 COUNT 查询对、自然 LOW、并发 1。其他工作负载、并发和负载需新预声明配置及实际验证，不能外推。

## 2. 比较组及消融含义

| 组 | 实际执行路径 | 历史/学习 |
|---|---|---|
| E0 Native | 现有 DecisionExecutionService baseline；原 SQL 经 S0 提交 IoTDB | 无模型、无四 Agent 监控/反馈；仍包含统一只读/Registry/执行审计成本 |
| E1 fixed strategy | 同服务，偏好已注册 S1_REPEAT_CACHE | 不适用、首次访问、安全约束仍可选 S0；固定偏好不绕过 Validator |
| E2 rule-based | 同服务，现有 rule_based | 不提供历史估计或学习建议 |
| E3 multi-agent no learning | 完整 Orchestrator、监控、质量过滤和经验记录 | `record_only`，不发布模型，不提供历史建议 |
| E4 adaptive learning | 完整 Orchestrator + 已冻结 LinUCB 建议 | 使用先前真实训练数据；留出期间不更新 |
| A1 no system state | E4 相同算法、相同训练证据和门槛 | 拟合及推断同时屏蔽 client/DB CPU、memory；查询特征保留 |
| A2 no history/learning | E4 移除已训练模型和历史建议，走现有规则 | 保留相同质量/记录流程；不更新模型 |

E3 与 A2 在当前确定性实现中等价，是有意保留的重复对照；不得将其相同决策说成独立算法增益。E2/E3 的差异主要用于观察完整工作流成本。E0 不是关闭 IoTDB 优化器，也不是零成本裸 SDK。

A1 只移除**学习模型输入**中的系统状态：17 维编码的 5–8 位设为 0，13–16 位缺失标记设为 1（0 基索引）。不按负载建隐含模型分区；两种模型按 workload、应用缓存条件、并发隔离。实际指标、样本准入、Registry/Validator 和资源保护都保留。原 LinUCB 的 A/b 更新、alpha=.1、lambda=1、reward 数值实现未改。

E4/A1 的 reward 为 `-完整请求毫秒/1000`，包含真实所选路径和同步记录成本；只有通过原 CompletedRequest、Experience、策略认证及资源门槛的 MATCHED 真实样本更新实际执行的 arm。两种模型原子式地提交同一证据的更新，分别保存屏蔽/完整特征结果。最少 30 合格样本/arm；不足时保留冷启动 Native 行为并阻止分析认证。未执行 arm 没有标签。

本版本采用请求级奖励，与阶段 7 的有限批次奖励属于不同实验协议，不能合并训练。E4 证明的是冻结学习策略的留出行为，不代表持续在线训练收益；未知未来请求的缓存填充收益归因仍是限制。

## 3. 随机顺序、缓存与并发

每个 block 包含预声明 workload × cache × target-load × concurrency 单元。固定 seed 随机排列单元和组。TRAIN 每单元包含 S0/S1 两个固定偏好序列，HOLDOUT 包含所有配置组。

`order=ABBA` 仅接受两个组，随机确定 A/B 后执行 ABBA 或 BAAB；同组的两个 repeat 在统计时归入同一 block，不能当成独立样本。七组完整比较使用 random。

每个 allocation 的每条并发 lane 独占 Adapter/连接、Registry、缓存及 SQLite 文件，不共享 mutable SDK Session 或模型更新。warmup 后重置 occurrence 和应用缓存；测量前 barrier 同步各 lane。模型训练在该 allocation 所有 lane 完成后单线程按 lane/index 顺序消费，保证日志可重放。留出 bank 只读。

缓存报告分区：

- `APPLICATION_EMPTY`：每条序列开始前清空应用结果缓存；首次 S0、后续填充和命中的所有测量请求都计入本组。
- `APPLICATION_WARM`：每 lane 在测量前显式执行两条准备请求（首次 S0，第二次经验证 S1 填充）。所有组使用相同准备流程；准备成本单列，不能从完整服务成本中消失。
- IoTDB、OS page cache、存储层缓存均为 **UNKNOWN**。没有执行清缓存、重启或服务配置修改；应用缓存为空不能称为服务端冷缓存。不生成混合“冷/热”总体加速比。

warmup 默认 2、每 lane 测量 8 请求、训练/留出各 30 blocks。配置示例合计 540 allocations、4320 次测量、1620 次 warmup/准备，另有两次 Native 参考和显式 doctor 能力探测。实际 SQL dispatch 因缓存命中而不同，均保留在日志。

## 4. 实测负载与执行安全

默认 `load_driver=ambient, loads=[LOW]`；LOW/MEDIUM 以 `cpu-activity-v3` 的真实 DB_HOST CPU 分类，不按 worker 数或连接数命名。LOW <50%，MEDIUM ≥50%；实验 CPU 停止线为 >80%，内存和磁盘容量使用率 >90% 停止。磁盘容量不称为磁盘 I/O 饱和度。

可使用新配置 `load_driver=cpu_cotenant, loads=[LOW,MEDIUM]`；显式传 `--cpu-cotenant-authorized`。复用已有 `staggered-spawn-v2`，MEDIUM 目标 55%，worker 自限 30 秒；标记为整机 CPU 共租负载，不冒充 IoTDB 查询负载。每条分组观测中至少 95% 落入其目标档位才允许分析认证；漂移样本保留真实档位及成本，不重标或丢弃。训练仍依据真实合格上下文。

真实入口仅支持经验证的本机 Linux DataNode；PID 可由监听端口唯一识别，也可传 `--local-db-pid`。DataNode 主类、端口、进程身份及 namespace 由现有 sampler 检查。每 allocation 前后检查外部连接，不结束外部任务；用户需提供独占窗口。该边界检查不能证明两次观测之间没有短时外部客户端。

两类主机指标分别记录；它们即使来自同一物理机器也不互相改名。每请求 dispatch 前两类快照必须有效且不超过 250ms，后台采样 .1s、TTL 500ms。资源拒绝保存原快照。查询 timeout 默认 30000ms；进程组 supervisor 默认每阶段/round 60 秒（预留 1 秒清理）。超时停止客户端不代表服务端取消；取消状态 UNKNOWN、不自动重试。

2026-11-04起，Benchmark监控额外保留最近32张快照的原始CPU前后计数及MANTO/DataNode进程CPU累计值，在resource_rejected.cpu_counter_evidence按原snapshot_id写入。证据协议benchmark-cpu-counter-evidence-v1独立于SystemSnapshot，不影响80%准入；缺失为null。进程/整机窗口并非原子同步，PID创建时间必须一致才能比较；其他主机活动仍UNKNOWN。额外监控开销不扣除，所有组一致，源码摘要区分新旧实验，详见PHASE8_CPU_EVIDENCE_FIX.md。

一个失败/错误结果导致停止新请求；并发中已发出的其他请求保留实际结果，客户端堵塞由 supervisor 有界终止。优化器已有的安全 Native fallback 保留，实际尝试数与耗时全计入，不主动双执行所有策略。

## 5. 原始产物与独立核验

每次运行必须使用全新目录，禁止覆盖/追加旧实验：

CLI启动及完成阶段有进度提示。Ctrl+C经supervisor停止子进程并保存aborted后，以130退出；为及时响应中断，不自动重放/绘图。需要部分报告时另行执行下方离线分析命令，输出到新的分析目录。进度打印位于round_complete信号之后，不进入单请求计时；启动/阶段外开销仍可能影响总体运行环境，需保留源码版本记录。

- `config.json`、`plan.json`、`implementation.json`；
- `journal.jsonl`：带 index、前序 hash 的原始事件；参考、分配、warmup/准备/测量、完整工作流/实际尝试、CPU 来源、学习接纳/拒绝及失败终态；
- `lanes/*.sqlite`：各 lane 独立执行/经验审计；
- `models.json`：完整参数、观测账本、冻结摘要、实际 evidence domain；
- `analysis/requests.csv`、`summary.csv`、`report.json`、`report.md`、`structure.json`；
- `analysis/latency.{svg,png}`、`resources.{svg,png}`；超过28个分组时按固定顺序分页（`-02`等后缀），不遗漏分组。

独立分析器重建计划、绑定 SQL、参考结果与完整结果匹配、缓存填充来源、query/execution ID、训练接纳/拒绝、两种模型参数、冻结指纹及推断建议。缺少整个 allocation、重复/冲突 ID、未登记缓存填充、伪造完成、终态后事件和日志损坏拒绝认证。停止/失败日志不删除；能解析的部分记录仍输出描述统计，禁止收益区间。模拟测试产物必须明确为 `SYNTHETIC_TEST_ONLY`，绝不并入真实实验。

正确性：ORDERED 逐列逐行，数字按预声明 abs/rel tolerance；NULL/非数字按类型和值。UNORDERED 比较完整行 multiset，保留重复次数，目前只支持精确值比较。列名与数据类型必须一致。普通生产请求保持 NOT_CHECKED；MATCHED 只来自该显式参考对照路径。

## 6. 指标与统计方法

- `end_to_end_ms`：围绕单次请求外层计时，含快照等待/安全准入、选定服务或完整 Orchestrator 返回、结果消费及该路径同步持久化。连接/初始化/关闭、warmup、缓存准备、离线拟合和外部日志序列化另列；同时报告含准备/清理的 allocation 吞吐。
- `client_wall_ms / execute_call_ms / result_fetch_ms`：全部真实尝试求和，包括 fallback。
- `decision_overhead_ms`：实验包装层计时，覆盖模型建议计算及现有决策器完整返回；不冒充解析、监控和学习等全部 Agent 开销。
- 服务端纯执行时间：**UNKNOWN/null**。不调用 EXPLAIN ANALYZE，也不把客户端计时充当服务端计时。
- CPU/memory：请求前快照的样本加权统计，client 和 DB 分列；不宣称查询级资源归因或时间积分。
- 吞吐：成功请求数 / 各不重叠 allocation 的实际测量起止窗口之和，保留并发重叠；不使用累计请求延迟作为并发墙钟时间。
- 吞吐CI按整块重采样，并在每次抽样中重新计算总成功数/总窗口时长，与点估计口径一致，不对不同持续时间的速率直接平均。
- 每 workload/cache/target-load/concurrency/group 分区输出 n、均值、样本标准差（ddof=1）、P50/P95/P99（`(n-1)*q` 线性插值）、CI、成功/正确性/回退/选择与命中计数。
- CI：以 block 为 cluster，lane、序列请求和 ABBA repeat 不作为独立样本。固定 seed、默认 4000 次 percentile bootstrap、99% 区间；每组至少 30 blocks。比较为同 block 的 candidate−baseline 均值差后重采样。
- 配对对照：E1−E0、E4−E0/E1/E2/E3、A1−E4、A2−E4。区间为探索性未校正多重比较，不自动宣布获益或阶段通过。未做反事实 regret，不虚构其他 arm 的耗时。

所有已分配测量结果均保留，包括失败、回退、低可信样本；学习资格和性能成本统计分开。计划/正确性/负载/样本门槛失败则 CI=null，不能删掉不利样本重新计算通过。置信区间不能代替独立会话复现。

## 7. 可复制命令

在仓库根目录运行，默认 Conda `MANTO`。`matplotlib==3.11.1` 在本轮环境已存在；新增 benchmark optional dependency 及实际运行依赖快照 `benchmarks/requirements.lock`，未安装/升级环境。

离线配置/计划检查（不连接 IoTDB）：

```bash
PYTHONPATH=src conda run -n MANTO python benchmarks/run_benchmark.py --config benchmarks/workloads/small_count.json --plan > /tmp/manto-phase8-plan.json
```

用户运行真实实验并自动导出结果，一条命令：

```bash
PYTHONPATH=src conda run --no-capture-output -n MANTO python benchmarks/run_benchmark.py --config benchmarks/workloads/small_count.json --output "benchmarks/results/phase8_small_count_$(date +%Y%m%d_%H%M%S_%N)" --immutable-epoch-confirmed --exclusive-window-confirmed
```

命令用时间戳区分每次输出；目标目录必须尚不存在，不要提前 mkdir。若出现 `output already exists` / `output must be new`，应保留旧目录并指定新路径，不能删除旧证据或追加续跑。CLI 在启动前拒绝已有目录，且不会因拒绝而重新导出旧报告；supervisor 的重复检查仍保留。模型、计划、统计定义和结果文件结构应相同；真实计时、ID 和模型数值不承诺逐字节相同。不可在观察留出结果后调参再沿用原留出作认证。

独立离线重放并输出到新目录：

```bash
PYTHONPATH=src conda run -n MANTO python benchmarks/analyze_results.py benchmarks/results/phase8_small_count_v1 --output benchmarks/results/phase8_small_count_v1_reanalysis
```

离线测试命令和本轮实测结果见 `docs/PHASE8_IMPLEMENTATION.md`。真实实验当前未运行，收益、真实并发和多负载结果均待用户日志验收。
