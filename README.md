# Multi-Agent TSDB Optimizer

代码入口已整理为`agents/query_analysis/agent.py`、`optimization_decision/agent.py`、`performance_monitor/agent.py`、`learning_feedback/agent.py`四个逻辑Agent；旧导入兼容，决策复用唯一DecisionEngine。目录与手册§5的完整职责映射见[代码导航](docs/CODE_LAYOUT.md)。

扩展实验co-tenant安全停止：已修复失败时控制器样本丢失及错误缺少触发值，相关69项回归通过；没有放宽安全阈值或改变负载算法，历史峰值原因仍UNKNOWN。[本次定位与最小修复](docs/PHASE8_COTENANT_STOP_FIX.md)。

13:35真实运行已核验：指纹复用后当前源码匹配，原日志独立重放一致；E4决策降至约15ms，但仍慢于E1/E3，整体NOT ACCEPTED。下一步由用户执行预声明多负载/并发扩展。[结果与边界](docs/PHASE8_HASH_REUSE_RESULTS.md)。

最新最小修复：移除E4/A1同次推断重复计算整库指纹，算法和验证不变；完整离线回归1025项通过。当前源码须重新取得真实证据，整体仍NOT ACCEPTED。[修复证据及用户实验命令](docs/PHASE8_INFERENCE_COST_FIX.md)。

阶段0–8整体审查：**NOT ACCEPTED**。当前非IoTDB回归1021项、独立验收探针8项通过；阶段7自适应额外收益未通过，阶段8工具与LOW/并发1复现通过，扩展真实覆盖仍待完成。以下进展按各自日期保留历史，不代表重复待办。详见[整体逐项验收](docs/OVERALL_REVIEW_20261104.md)。

最新进展：公平性修复后的两次LOW/并发1独立会话均完成，同源码/配置/计划/产物结构一致，日志独立重放通过。固定策略收益复现，E4相对固定策略的负收益也复现；扩展真实验证待完成，阶段仍NOT ACCEPTED。[第二轮复现结果](docs/PHASE8_FAIRNESS_REPEAT_RESULTS.md)。

首轮真实结果（11月4日12:04）：F1修复后5940请求全部MATCHED、独立重放一致，E1/E2/E3决策开销已对齐。E4仍比固定策略慢27–30ms，未证明学习收益；此后独立重复已完成，扩展验证仍待执行。[本轮结果](docs/PHASE8_FAIRNESS_RUN_RESULTS.md)。

面向 Apache IoTDB 的查询优化研究工程。当前按用户明确指令实现 **阶段 8：Benchmark 与消融实验工具**。
阶段 7 原验收仍 **NOT ACCEPTED**，生产部署 **NOT READY**；阶段8已有一次完整真实实验且分析资格通过，整体验收尚未完成。

阶段8严格验收：**NOT ACCEPTED**，基础设施不对称导致公平性FAIL，扩展真实验证BLOCKED；本轮离线专项64 passed。[逐项结论与最小修复清单](docs/PHASE8_REVIEW_20261104.md)。

上述F1已完成最小代码修复：旧服务对齐请求内解析/连接复用，专项28 passed、完整非IoTDB回归1021 passed。随后两次真实复跑已完成，原不对称问题在LOW/并发1范围关闭。[修复范围与实验命令](docs/PHASE8_FAIRNESS_FIX.md)。

11月4日10:12运行：5940请求全部MATCHED，离线重放一致。E4比Native快，但比无学习E3慢14–16ms；各非Native组动作/命中相同，尚未证明学习增益，需核对执行路径成本差。[完整结果与限制](docs/PHASE8_FIRST_COMPLETE_RESULTS.md)。

离线成本核对已确认：旧服务未使用Orchestrator已有的请求内解析/SQLite连接复用，造成组间基础设施不对称。E1与E3差额主要在决策及执行器验证，不能归因于数据库或学习收益。[逐请求账本与审计结论](docs/PHASE8_PATH_COST_AUDIT.md)。

最新定位：第二次运行DB主机CPU82.809%触发80%停止线，原快照离线复现，未发现可证实的运行逻辑错误。已补回归测试；峰值进程与采样窗口原因UNKNOWN，下一步先取证而非盲目重跑。[事实、测试与取证命令](docs/PHASE8_CPU_STOP_20261103_211545.md)。首次client CPU97.009%、DB主机CPU100%的失败记录和此前诊断修复保留于[首次定位](docs/PHASE8_RESOURCE_ADMISSION_FIX.md)。

11月4日09:50运行再次CPU超限。已补齐Benchmark拒绝时的原始CPU计数、窗口和MANTO/DataNode进程证据，绑定原快照ID；阈值与算法不变。历史峰值来源尚未证实，新运行会自动留存归因依据。[最小修复与验证](docs/PHASE8_CPU_EVIDENCE_FIX.md)。

阶段8新增固定workload/数据集协议、E0–E4及A1/A2、随机/ABBA顺序、并发独立执行通道、原始JSONL、独立重放、CSV/JSON、图表和报告模板。非IoTDB回归988项通过，最后分析产物调整后专项30项通过；真实收益未测，不以模拟测试替代实验。[实验协议与完整边界](docs/EXPERIMENT_PROTOCOL.md)、[实施与测试](docs/PHASE8_IMPLEMENTATION.md)。

用户在独占窗口、不可变数据epoch下运行并自动生成报告：

运行时会立即显示启动提示，并在前两个参考阶段及每10个完成阶段输出进度。Ctrl+C会中止并保留证据，以130退出，不自动生成部分图表。`conda run -n MANTO`无需先activate；已激活MANTO时可直接使用`PYTHONPATH=src python ...`。不要用`/opt/anaconda3/bin/conda activate MANTO`代替shell函数激活。

```bash
PYTHONPATH=src conda run --no-capture-output -n MANTO python benchmarks/run_benchmark.py --config benchmarks/workloads/small_count.json --output "benchmarks/results/phase8_small_count_$(date +%Y%m%d_%H%M%S_%N)" --immutable-epoch-confirmed --exclusive-window-confirmed
```

输出目录必须新建；同配置复现改用新的`--output`目录。默认只运行自然LOW/并发1，其他档位和并发须预声明配置。使用`--plan`可离线检查计划；数据库实验仍由用户执行。以下保留阶段7历史进度。

最新修复：消除同一SQLiteStore重复初始化时的迁移工作，仍核对数据库身份及schema版本；针对性124 passed、全部单元863 passed。未调用IoTDB，未宣称F2收益通过。[修复与测试结果](docs/PHASE7_SQLITE_INIT_FIX.md)。

重新验收：完整离线重放与v7原报告逐字段一致；F1/F3及初始化工程修复PASS，F2真实额外收益仍未通过，修复后的真实收益缺新证据。[重新验收结论](docs/PHASE7_REACCEPT_AFTER_INIT_FIX.md)。

最新验收：v7四策略独立验证完成，5400条请求全部VALID/MATCHED，专项179 passed、全量966 passed。F1冻结模型恢复限定范围通过，F3同证据L1对照缺项已补齐；F2相对固定S1额外收益仍未通过（差-3.083ms，99%区间跨零），相对L1也未证明更优。**阶段7 NOT ACCEPTED。** [v7结果](docs/PHASE7_V7_RESULTS.md)。助手可自行执行不调用IoTDB的测试；数据库集成、训练和压测仍由用户执行。
导入与路径核验已完成：320万序列各3000点，总计96亿点；使用新 epoch `heavy-complete-20261102-v2`。
历史v6结果：用户执行的v6已完成360批训练、540批独立留出，4500条请求全部VALID/MATCHED，360次延迟更新可重放；专项154 passed，全量942 passed。自适应完整批次成本相对Native平均减少57.124ms；相对固定S1平均减少0.525ms，但99%区间[-7.846,+6.826]跨零，额外收益门槛仍FAIL。**阶段7仍NOT ACCEPTED，生产部署关闭。** 请求内解析修复和新计时已通过本次流程。[v6结果与剩余阻塞](docs/PHASE7_V6_RESULTS.md)。该轮由用户执行测试，助手读取完成日志分析。

最新工作（2026-11-03）：实现已声明有限批次的填充/后续命中收益归因与持久化延迟反馈，完整回归 **918 passed**，证据审计测试 **9 passed**。生产Bandit仍关闭。

重新训练两次触发保护：v1完成201批/1008请求后快照准入失败；v2完成98批/492请求（96次模型更新、2批负载变化拒绝）后，实测DB主机整机CPU **83.183%** 超过80%停止线。已提交模型均可独立重建，但两次均未进入冻结后的留出验证，**本轮收益UNKNOWN，独立多负载验证尚未完成**。详见[批次反馈实现与失败证据](docs/PHASE7_DELAYED_BATCH_FEEDBACK.md)。

此前完整序列实验中长度8固定S1优于单请求冻结模型；本轮改为完整批次实际成本标签，不把未发生的未来收益当作即时反馈。仅支持已声明的同SQL长度2/8批次，不能外推未知长度流或其他SQL族。

此前验证：阶段7显式模型采用完整请求奖励`-end_to_end_ms/1000`，覆盖同步学习与持久化，旧Adapter奖励隔离。小/大规模各完成240条训练、216条真实留出，模型独立重建一致。

自然LOW且缓存已命中时，S1分别比S0平均快 **27.729ms / 103.235ms**，配对99%区间均低于0；缓存未命中时两种规模均无稳定差异。未证明自适应稳定优于固定S1；人工CPU负载预检触发保护，本轮自然负载实验不能替代多负载验收。**阶段7仍NOT ACCEPTED，生产部署关闭。**

此前全量 **882 passed**（含13项真实集成），本轮最终测试见[当前状态](docs/IMPLEMENTATION_STATUS.md)。原READY实验的显式准备成本单列；新序列实验已把填充计入，但未证明自适应额外收益。生产L1仍保留历史Adapter目标，新的完整请求目标仅在显式阶段7实验中使用。

阶段6四Agent闭环保留，不要求LLM；未进入阶段8。DB_HOST是核验后的数据库所在整机指标，不是IoTDB独占指标。所有失败批保留。

## 当前能力

| 模块 | 已实现边界 |
|---|---|
| Agent 1 | fixture 验证的解析子集、QueryFeatures；未知语法禁止改写 |
| Registry / Validator | 外部 configs/strategies.yaml；有限策略、能力/版本/只读校验 |
| Agent 2 | baseline / rule_based / adaptive；版本化资源闸门、保守默认、建议不能绕过验证 |
| Executor / Adapter | 原始 S0 基线；只执行已选候选；完整消费结果；超时/完成未知不重试 |
| Agent 3 | client psutil、真实 ExecutionRecord、显式 EXPLAIN；默认 DB 指标 UNKNOWN，实验可显式验证本机 DB 主机采样 |
| Agent 4 | SQLite 经验、质量过滤、严格 context 检索、L1 统计与建议、版本隔离 |
| SQLite | v8；保留既有查询/决策/执行/学习记录，新增 workflow_traces |
| Orchestrator | 四 Agent 串行闭环、完整 trace、失败隔离；run/workload CLI |
| 阶段 7 离线实验 | 17 维 LinUCB、单 arm 更新、原子模型快照/回滚、固定种子与独立 Benchmark |
| 可选 LLM | 默认关闭；受限 strategy_id JSON 接口；实际 provider/真实收益对照未接入 |
| 未开放 | 真实 Bandit/在线探索部署；DB 主机指标代理 |

策略仍只有 `S0_NATIVE` 与 `S1_REPEAT_CACHE` 启用；所有 `CAND_*` 禁用。
S1 是调用方声明不可变 dataset epoch 下的进程内 exact SQL 完整结果缓存，
不是 SQL 改写或服务端物理计划优化。无 epoch 时不启用；同进程重复 exact SQL
达到 occurrence 门槛后可 miss/hit。进程退出即清空，不能声称通用 freshness。
认证、收益与 fallback 的历史证据见 [策略目录](docs/STRATEGY_CATALOG.md)。

## 四 Agent 入口

```bash
conda activate MANTO
PYTHONPATH=src python -m tsdb_optimizer run \
  --sql "SELECT s_1666, s_1766 FROM root.db800.g_0.d_0, root.db800.g_0.d_1 WHERE time = 1640966450000" \
  --mode baseline --server-version 2.0.6 --database-model tree --timeout-ms 30000
PYTHONPATH=src python -m tsdb_optimizer workload --file configs/workload.example.json --mode adaptive
```

workload 是 SQL 字符串 JSON 数组；逐条输出 JSONL，拒绝单条不停止后续查询。
缺少数据 epoch、缓存条件或可比历史上下文时仍可运行 Native，但不会伪造性能奖励或建议。
`--expected-result-rows` 只接受独立已知结果规模；未提供不猜测。`query` / `optimize`
保留旧入口行为，不自动学习。真实脱敏十阶段证据见
[phase6_workflow_2026-11-02.json](docs/evidence/phase6_workflow_2026-11-02.json)。

## 环境与 CLI

**默认使用 Conda 环境 `MANTO`**：统一用 `conda run -n MANTO python ...`，
或先 `conda activate MANTO`。Python 版本以该环境实际检测结果为准，历史版本号不代表环境名称或固定解释器路径。
2026-11-03 检测该环境为 Python 3.12.13、apache-iotdb 2.0.6，对应已审计 server
2.0.6 / tree。本仓库当前没有必要另建虚拟环境。`.env` 是本地连接配置/密码，
不是 Python 环境；`.venv-iotdb206/` 是可选环境，只有需要隔离时才运行
`scripts/setup_iotdb206_env.sh` 创建。

```bash
conda activate MANTO
python -c "import importlib.metadata as m; print(m.version('apache-iotdb'))"
# 输出必须为 2.0.6。首次配置时从 .env.example 复制并填写 .env；不要覆盖现有凭据。
chmod 600 .env

PYTHONPATH=src python -m tsdb_optimizer doctor
PYTHONPATH=src python -m tsdb_optimizer query \
  --sql "SELECT s_1666, s_1766 FROM root.db800.g_0.d_0, root.db800.g_0.d_1 WHERE time = 1640966450000" \
  --timeout-ms 5000
PYTHONPATH=src python -m tsdb_optimizer optimize \
  --sql "SELECT s_1666, s_1766 FROM root.db800.g_0.d_0, root.db800.g_0.d_1 WHERE time = 1640966450000" \
  --mode baseline --timeout-ms 5000
```

示例路径来自当前真实 fixture，其他部署须换成实际存在的树路径。CLI 可使用
`--file`（文件中只含一条 SQL）、`--preview-rows` 和已观测的 `--server-version` /
`--database-model`；`--cache-epoch` 是调用方数据不变契约。单次 CLI 进程一般只看到
occurrence 1 的 Native，不能用它证明连续缓存命中。

所有 SQL 入口只接受单条只读 SELECT。SQLGlot 无 IoTDB dialect，守卫只对已验证的
通配路径和 interval GROUP BY parse gap 做内存解析归一化，不改变执行 SQL；其他
未知 ParseError fail-closed。预览限制不缩短全结果消费。timeout 单位 ms，客户端
观测超时不等于服务端取消证明；服务端 timeout=0 的语义仍 UNKNOWN。
普通路径不调用 EXPLAIN/EXPLAIN ANALYZE。

## 阶段 5 使用方式

详见 [LEARNING_DESIGN.md](docs/LEARNING_DESIGN.md)。Agent 4 是显式库接口，
接收完成的 run、Agent 3 实际 record、真实 context 和 before 快照，绝不再执行查询：

```python
from tsdb_optimizer.learning.repository import ExperienceRepository
from tsdb_optimizer.agents.learning_feedback import LearningFeedbackAgent

store.initialize()
learner = LearningFeedbackAgent(ExperienceRepository(store), registry,
                                min_samples=3, min_improvement=0.05)
feedback = learner.observe(run, context, record=record, before=before)
advice = learner.recommend(analysis, context)  # None 或交给下一次 Agent 2 的 AdaptiveAdvice
```

`VALID` 才能产生奖励 `-log1p(client_wall_ms / 1000.0)`；`LOW_CONFIDENCE` 和
`INVALID` 只记录事实。认证来自策略版本独立回归，不以一次成功或 MATCHED 自证。
按模板、具体特征、数据库身份/版本/模型、epoch/规模、缓存、fetch、实际返回行数和负载来源/区间
严格分组。SERVER/CACHE_HIT 不混算；没有执行的策略没有 reward。

每策略输出 observed/valid n、均值/中位延迟 ms、总体方差 ms²、成功率和 mean reward。
Native 和候选均达到 min_samples、成功率为 1 且候选中位数严格改善超过 5% 时，
才给出建议；建议仍须通过当前 Registry/Validator。没有同组参照就不给建议。
质量版本 2 与资源策略指纹随经验和模型保存；旧规则经验仅保留审计，
不继续参与当前奖励统计，旧模型和不同资源策略下的模型不能用于建议。
MISMATCHED 隔离策略版本并撤销其历史 reward；已隔离的旧决策也不可执行。
隔离持久化故障返回 pending 标记，恢复后可重试原证据。
SQLite 事务原子发布模型与指针，保留前版；stale 模型不用。学习写库失败只返回
警告，不改变查询结果，也不重执行。CLI run/workload 已通过 Orchestrator 自动调用这些接口，旧 query/optimize 保持兼容。

## 测试与实际证据

下列 python 使用 Conda MANTO；阶段 7 复审修复后实际结果（阶段 5/6 结果见归档）：

```bash
IOTDB_INTEGRATION=1 PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=src python -m pytest -q -p no:cacheprovider # 750 passed in 35.83s
PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=src:tests/unit python -m pytest -q -p no:cacheprovider docs/evidence/phase6_review_probes.py docs/evidence/phase5_review_probes.py docs/evidence/phase5_rereview_probes.py docs/evidence/phase7_review_probes.py # 11 passed in 2.53s
# 本轮另用 ast.parse 检查 src/tests/scripts 的 106 个 Python 文件，exit 0
```

模拟数据测试验证已知 reward/均值/中位数/方差、LOW/MEDIUM 不同偏好、门槛、隔离、
重复、超时/fallback、写库失败和模型事务。13 个真实集成用例包含 Agent 4 和阶段 6 Orchestrator，
实际请求、ExecutionRecord、SQLite 与统计相连，普通记录保持 NOT_CHECKED。

另运行显式只读诊断 Benchmark（与在线路径分离）：

```bash
PYTHONPATH=src python scripts/phase5_learning_evidence.py \
  --dataset-epoch phase5-static-20261102 --repeats 4 \
  > docs/evidence/phase5_learning_fixes_2026-11-02.json
```

[修复后原始证据](docs/evidence/phase5_learning_fixes_2026-11-02.json)：q4/q5 共 24 条观测，
16 次测量 server dispatch，另有 2 次预热和 doctor。12 VALID；12 因未支持模板而
LOW_CONFIDENCE，无 reward。全部实际完整结果与 Native 参照一致。缓存命中单独分组，
客户端负载 LOW；没有伪造 DB 指标或“模型已带来生产加速”结论。

## 限制与工程边界

- 不修改 IoTDB 源码，不猜测 API/SQL/配置/EXPLAIN 字段；能力未知标 UNKNOWN。
- DB 主机与服务端指标没有代理；客户端 psutil 无论是否同主机，都不能冒充 DB 指标。
- Agent 4 仅对已支持模板和 exact SQL 非改写契约更新奖励；不同 context 不推广。
- epoch、缓存条件和环境身份必须由调用方按实际声明；未知数据规模保持 null。
- min_samples/5% 门槛是保守规则，不是显著性检验；无滑动窗口、漂移处理或在线探索。
- S1 缓存仍是进程内，没有 single-flight；原缓存键未扩展为 server/策略版本键。
  L1 经验上下文隔离不代表缓存自身新增了这些能力。
- 运行时隔离只适用于绑定 learner 的 Registry；磁盘故障时不能保证隔离跨重启持久化。
- 旧 HistoricalCostEstimator 的简单历史查询未变；Agent 4 使用独立的严格上下文统计，
  不能把旧成本估计器当成阶段 5 的可靠检索接口。
- 四 Agent 入口为 run/workload；旧 optimize 保留阶段 4 接口。学习采用执行后同步小批次，可能增加响应延迟。
- 严格检索所需结果规模未知时不给预测建议；未知 epoch/cache 条件不更新奖励。
- 未提交 Git。阶段 5 库接口已验收，阶段 6 已验收；阶段 7 仅离线分支，禁止真实启用，未进入阶段 8。

工程依据：[IMPLEMENTATION_GUIDE.md](IMPLEMENTATION_GUIDE.md)。
当前验收清单：[IMPLEMENTATION_STATUS.md](docs/IMPLEMENTATION_STATUS.md)。
历史阶段 4 报告：[PHASE4_IMPLEMENTATION_STATUS.md](docs/PHASE4_IMPLEMENTATION_STATUS.md)。
能力审计：[DB_CAPABILITY_AUDIT.md](docs/DB_CAPABILITY_AUDIT.md)。
指标口径：[METRICS_DICTIONARY.md](docs/METRICS_DICTIONARY.md)。


## 阶段 7 离线实验

```bash
PYTHONPATH=src python scripts/phase7_offline_evidence.py \
  --output docs/evidence/phase7_offline_2026-11-02.json \
  --model-store /tmp/manto-phase7-models.sqlite --benchmark
```

这是显式只读受控 Benchmark，普通在线请求不双执行。训练/测试 IDs 和时间隔离，未执行
arm 没有 reward；LLM 开关对照仅本地注入接口，实际 LLM 质量/时延 UNKNOWN。
没有向 Orchestrator 注入 Bandit 或 LLM；原 CLI 行为不变。


阶段 7 本轮复审修复了留出样本重复计数、隔离策略令审计/评估中断的问题；拒绝样本保留
逐条原因，新增 9 项正式回归。离线数值实现与原始真实记录重算一致，但真实部署条件
仍未满足，不进入阶段 8；完整证据与命令见 [复审报告](docs/PHASE7_REVIEW.md)。


三规模重查询工作负载已生成：每规模 8 类型、每类型 1000 条，共 24000 条。文件说明、重建命令与验证范围见 [工作负载文档](query/heavy_scale/README.md)。


此前heavy_scale真实实验发现源CSV与数据库不一致（现已修复，见下方报告）：抽查窗口1931点、DB COUNT为390。新的阶段7实验入口会在学习前拒绝这类不一致；当前本机DB主机采样已补齐，但部署仍NOT READY。修复、实际命令与复审见 [heavy_scale阶段7报告](docs/PHASE7_HEAVY_EXPLORATION.md)。


三规模导入已修复：IoT-Benchmark的设备到单文件映射导致漏掉前两批，现已补入64亿点；全量COUNT确认320万序列各3000点，总计96亿点。使用新数据epoch `heavy-complete-20261102-v2`，旧heavy_scale epoch的缓存和模型不得混用。真实命令、失败记录、786项回归结果及剩余阶段门槛见 [导入修复报告](docs/HEAVY_IMPORT_REPAIR.md)。
