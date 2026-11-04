# 学习设计：可靠经验、L1 统计与阶段 7 离线扩展

2026-11-03补充：阶段7新增实验专用L1_BATCH完整批次均值对照，与LinUCB使用同一APPLIED训练证据，按实际负载/长度/策略版本分组，min_samples=30、平局Native、冻结后不更新。生产L1保持原样。v7还报告训练真实奖励和更新前预测；不造反事实regret。实现/协议待用户测试和真实验收，详见[阶段7 F2/F3 v7](PHASE7_F2_F3_V7.md)。

日期：2026-11-02。主体保留阶段 5 验收设计；阶段 7 新增离线扩展见文末及 PHASE7_DESIGN.md，真实 Bandit/LLM 未部署。
当前为 **ACCEPTED（阶段 5 L0/L1 库接口）**，阶段 6 已通过 Orchestrator 接入，阶段 6 已修复 F-1/F-2 并复验 ACCEPTED，详见 PHASE6_REVIEW.md；阶段 5 验收结论不变。生产路径未启用 Bandit、向量数据库、LLM 总结学习或自动探索；阶段 7 离线扩展见文末。

修复前的三个失败反例及修复后证据见 [PHASE5_REVIEW.md](PHASE5_REVIEW.md)。
无 I/O 隔离前置校验、所有来源资源准入、持久化模型版本核对已通过严格复审；
验收范围和真实 DB 负载证据限制见报告最后一节。

## 1. 边界与调用

`LearningFeedbackAgent.observe()` 接收已经完成的 `OptimizationRun`、Agent 3
生成的 `ExecutionRecord`、显式 `LearningContext` 和 before/during/after 快照。
它不执行 SQL，不修改传入的查询结果，不触发 EXPLAIN/EXPLAIN ANALYZE，不要求
普通请求做差分比对。`runtime_result_check=NOT_CHECKED` 是正常状态。

`recommend()` 只返回已执行且统计合格的策略 ID、模型版本和理由，或者 `None`。
调用者将建议交给 Agent 2；Registry、适用性检查、资源闸门、Validator、Executor
仍然生效。它不能生成 SQL，不能使未知策略或已隔离版本可执行。

这是显式库接口。CLI `optimize` 尚未自动采样、检索、调用学习器或组织闭环；
这些调度现由阶段 6 Orchestrator 实现，旧显式库接口继续保留。用同一个已初始化 Store、Registry 构建组件：

```python
from tsdb_optimizer.learning.repository import ExperienceRepository
from tsdb_optimizer.agents.learning_feedback import LearningFeedbackAgent

store.initialize()  # SQLiteStore，已有 DecisionExecutionService 使用同一个 store
repository = ExperienceRepository(store)
learner = LearningFeedbackAgent(repository, registry, min_samples=3, min_improvement=0.05)
# run 是已完成并持久化的 OptimizationRun；record 是其中一个真实 attempt 的 ExecutionRecord。
feedback = learner.observe(run, context, record=record, before=before)
advice = learner.recommend(analysis, context)  # 可为 None；下一次决策仍走 Agent 2
```

构造 learner 时为这个 Registry 绑定持久化隔离检查器。重启后重新绑定即可恢复
隔离；未绑定的独立 Registry/CLI 不会自动读取学习库，不能声称全系统闭环隔离。

## 2. 协议、关联与 SQLite v7

`schemas/learning.py` 定义 Pydantic 协议，`learning/repository.py` 负责事务与检索，
`agents/learning_feedback.py` 负责质量判定、更新与建议。协议 schema_version=1；
SQLite user_version=7。v1–v6 数据库原位增量迁移，不删除既有审计记录。

| 表 | 关系与内容 |
|---|---|
| queries / decisions / executions / execution_reports | 保留阶段 4 真实审计证据；decision.model_version 记录产生该次建议的模型 |
| experiences | experience_id；query/decision/execution 外键；execution_id 唯一；策略 ID/版本；模板、特征、context/group key；质量、reward、完整经验协议 JSON |
| strategy_quarantine | (strategy_id, strategy_version) 唯一；触发 execution_id、原因、UTC 时间 |
| learning_group_state | 每个严格 context group 的 revision，防止相互无关的负载分组使对方模型失效 |
| learning_state | 全局写入 revision，仅供审计 |
| learning_models | 不可变 model_version；group/revision、统计、阈值和全部源 execution IDs |
| learning_heads | 每个 group 的当前模型指针，外键关联 learning_models |

经验 JSON 保存查询模板和 QueryFeatures、实际执行记录、before/during/after 快照、
策略认证测试套件、决策模型版本、fallback 和 cache 来源。缺少 during/after 可为
空，不虚构采样。保留源 execution IDs 可回查每个统计的真实成员；不保存结果行或凭据。

写入前同时核对 run、record 和数据库中已持久化的 query/decision/execution：
ID、实际策略、版本、状态、时间戳、wall 时间、fetch/result source、epoch 必须一致。
调用方提供的分析必须与重新运行确定性 Agent 1 得到的特征一致。decision_model_version 必须与同一事务读取的 decisions.model_version 精确一致，
包括 None/非空差异，否则拒绝写入。当前仅支持
实际执行 SQL 等于原 SQL 的已认证策略；未来改写策略需要独立契约，不能默默准入。
Repository 接收 Agent 4 已校验的 Experience，不是外部任意 JSON 训练入口。

相同 execution 重试不重复计数；上下文冲突拒绝。迟到的 MISMATCHED 证据允许撤销
原样本，不增加 execution 数量。SQLite 写入使用参数绑定；JSON round_trip 序列化
排除计算属性，避免重读协议时出现额外字段。

## 3. 可比上下文与检索

group_key = SHA256([template_key, feature_key, context.key])，采用规范化 JSON。
先做严格相等检索，不做相似度扩展：

- 模板来自已验证 Agent 1；不支持的语法使用 exact SQL hash 作为记录检索身份，
  normalized_template 保持 null，且不产生奖励。
- feature_key 保留具体查询特征、时间边界、路径等，排除 query_id/hash、模板文本和
  warnings 等实例字段；特征版本为 agent1-v1。当前分组偏保守，不跨不同数据范围推广。
- context 包含 server_id/version、tree/table 模型、dataset_epoch、可选 dataset_size、实际 result_rows、
  cache_condition、FULL fetch、SERVER/CACHE_HIT、负载 source/host/bucket。
- 策略 ID/版本在分组内分别统计，可指定版本检索；版本不同不能共享奖励。
- 数据集 epoch、server identity、cache_condition 由调用方显式声明，必须对应真实
  观测或受控实验。没有虚构 IoTDB 数据版本 API；未知 epoch 标 LOW_CONFIDENCE。
  dataset_size 未测量时为 null，保持该维度隔离，不猜测数据规模。
  result_rows 必须与实际返回行数一致；未知则 LOW_CONFIDENCE，不同实际行数不混算。
  检索/建议时调用方需提供已知的结果规模上下文；未知时不推广已有模型。
- SERVER 与 CACHE_HIT 完全隔离。命中时间是客户端缓存服务时间，不能称 DB 延迟。
  命中样本仍有实际 client wall reward，但不会与 Native SERVER 直接比较。

负载只取执行开始时仍可用的 before 快照：source 匹配、valid 且
`captured_at <= started_at < valid_until`。客户端与 DB 来源不能互相替代。
固定分桶（协议 schema 1）：max(CPU、内存、磁盘容量百分比) <50 为 LOW，
50–90 为 MEDIUM，>90 为 HIGH；缺失/失效/未来/来源不匹配为 UNKNOWN。
UNKNOWN 分组不参与正常奖励。分桶只用于分组，不决定硬约束是否越限。
Agent 4 默认加载与 Agent 2 相同的 configs/resource_policy.yaml，也可显式传入
resource_policy；调用方使用自定义策略时应向二者传入同一对象。准入通过
assess_resource_pressure 在执行开始时刻检查 client_host、db_host、db_metrics
所有有效且新鲜的来源，任一指标严格大于对应阈值即拒绝奖励。过期/未来/未知来源
不伪造越限；等于阈值允许。自定义阈值不改变固定负载分桶。HIGH 是否可准入由
显式资源策略决定（默认 90% 上限会拒绝）；I/O 和 active_queries 无通用上限。

## 4. 样本质量

| 标签 | 条件 | 用途 |
|---|---|---|
| VALID | 策略版本经独立认证且与实际 DB 版本/模型相容；执行成功、未超时、FULL fetch；上下文已知可比；负载合格；没有 fallback/MISMATCHED | 延迟/reward 统计和模型更新 |
| LOW_CONFIDENCE | 未支持模板、缺少可用负载、UNKNOWN epoch/cache/server identity、任一有效来源违反资源策略、fallback 条件样本 | 保存事实与原因，不产生 reward，不训练 |
| INVALID | 执行失败/超时、MISMATCHED、未认证环境、部分结果、非法延迟、实际 fetch/cache/epoch 与声明矛盾、版本隔离 | 保存审计；无 reward；不得训练 |

INVALID 优先于 LOW_CONFIDENCE。若 record 与实际执行不符、源执行未持久化或
同一 execution 被改写上下文，拒绝写入并返回 LEARNING_FAILED，不能补造证据。
认证来自实际 Registry/spec/独立套件；一次成功、调用方写 PASSED 或摘要不构成认证。
NOT_CHECKED 不降级为 LOW_CONFIDENCE，也不能被自动改成 MATCHED。

MISMATCHED：先无 I/O 核对 record 与 run 的实际 attempt、身份、字段和策略版本，
再立刻在当前 Registry 隔离对应 ID/版本，最后做认证查询与经验构造；事务中持久化隔离，撤销该
版本所有历史经验的 reward、标 INVALID，递增受影响所有 context revision。
旧模型保留审计，但读取时判 stale，不再用于建议；Executor 重新查 Registry，
连隔离前签发的旧决策也不能继续执行该版本。新版本没有自动继承旧认证/奖励。
没有自动解除隔离。若 S0 本身被证实不一致，优化执行 fail-closed，不能继续信任
该基线。读取或写入故障仍保留当前 Registry 内存隔离；feedback 中
quarantine_persistence_pending=true 并返回 QUARANTINE_PERSISTENCE_PENDING。恢复后
用原证据重试 observe，确认持久化隔离后清除 pending，不重执行 SQL。磁盘故障时
不能声称隔离已跨重启持久化，没有后台自动重试或持久化成功的假保证。

## 5. 固定奖励与统计

唯一奖励版本：negative-log1p-ms-v1。

`reward = -log1p(client_wall_ms / 1000.0)`

输入单位 ms，除以 1000 转为秒；输出无量纲，越接近 0 越好。只使用已实际执行
的策略，未执行的策略无 reward/样本；不构造 counterfactual。目标是 Adapter
完整消费的 client_wall_ms，不是服务端算子时间，也不是 API 端到端时间。
资源保留为约束/审计，不随意加权。正式路径不为奖励额外执行 Native。

每个 group × strategy/version 统计：

- observed_count：所有已记录样本；valid_count：VALID、quality_version=2 且
  resource_policy_hash 与本次模型一致的样本。旧准入规则的记录仅保留审计。
- mean/median_latency_ms、总体方差 variance_ms2（分母 n）及 mean_reward：仅上述当前规则下合格的 VALID。
  无 VALID 时为 null，不能填 0。n=1 时总体方差 0 不代表已达到训练门槛。
- success_rate：所有观测中 execution.success 的比例，失败样本只影响此比率，不进入
  延迟/reward 均值。它是执行成功率，不是语义正确率；语义隔离优先。
- eligible：valid_count >= min_samples 且 success_rate == 1。保守采用全历史统计，
  未实现窗口、衰减或失败后自动恢复，不声称应对非平稳性。

默认 min_samples=3（可调但 >=2）。建议要求 Native 与候选都达到门槛、版本仍可选、
上下文严格相同；候选中位数必须严格低于 Native 中位数的 95%（默认
min_improvement=0.05）。基线胜出、并列、样本不足、只有缓存命中组、没有 Native
参照时返回 None。单次快不构成策略有效的证据；门槛不是显著性检验或因果证明。

## 6. 版本、原子保存与失败行为

build_model 在 SQLite 一致读事务中获取 group revision、源 execution IDs、统计。
publish 先从真实经验重算核对统计，再在 BEGIN IMMEDIATE 中检查 revision；插入
不可变模型和更新 head 在一个事务提交。失败全部回滚，前一个版本保留。
经验改变与隔离使旧模型失效；读取 stale head 返回 None，不能把旧成功模型继续用。
不同 group 的 revision 独立。模型包含 min_samples/min_improvement 与创建时间，
查询 decision_model_version 指向实际用过的建议版本。没有 pickle 或部分覆盖文件。

修复后经验记录 quality_version=2、resource_policy_version 和完整策略规范 JSON
的 SHA-256 指纹；模型记录 quality_version 与同一指纹。旧 JSON 缺少字段时按
quality_version=1 读取，保留原始审计与历史 reward，但不加入当前延迟/reward 统计，
旧模型不再作为 current_model 返回。即使误复用版本标签，不同阈值/开关的指纹也
不同，recommend 不复用不同策略下的模型。该阶段修复时 SQLite 为 v7；阶段 6 增量升级为 v8，仅新增 workflow_traces，不改动学习表。
自定义策略的报表调用 build_model(..., resource_policy_hash=learner.resource_policy_hash)。

写经验/模型失败：记录带 query/execution ID 的异常日志，feedback 返回
LEARNING_FAILED/MODEL_UPDATE_FAILED；传入 run 原样保留，不重试 SQL。
检索异常返回 None 并记录 learning_retrieval_error；由 Agent 2 继续安全决策。
SQLite 隔离检查不可读时 Registry fail-closed，不能把存储故障解释为“未隔离”。

## 7. 验证与真实证据

模拟数据只存在 tests/unit/test_learning.py，明确 TEST ONLY，不混入真实证据。
覆盖已知均值/中位数/方差/reward、最小样本、5% 门槛、LOW/MEDIUM 不同偏好、
版本/特征/context 隔离、NOT_CHECKED、失败/超时/fallback、重复/冲突、伪造 record/analysis、
迟到 MISMATCHED、跨 context 撤销、重启隔离、内存隔离、旧决策阻断、写库故障、
原子回滚/过期 revision、伪造模型、:memory: 和 v6 迁移。
新增 tests/unit/test_learning_review_regressions.py 将三个审查反例纳入正式套件，
并覆盖跨来源各百分比、阈值边界、时间有效性、读写故障恢复、错误身份、旧规则模型
与策略指纹隔离、None/外部模型版本。真实测试见 tests/integration/test_learning_feedback.py：真实 Adapter/服务/Agent 3
记录进入 SQLite 和 L1，3 次请求对应 3 条 execution，均为 NOT_CHECKED。

实际 Benchmark 原始数据见 evidence/phase5_learning_2026-11-02.json，脚本为
scripts/phase5_learning_evidence.py。IoTDB/server/client 2.0.6 tree，调用方声明静态
phase5-static-20261102 epoch；q4/q5 各 1 次 Native 预热对照，再按固定种子打乱
4 次 S0 + 4 次 S1 miss；每次 miss 后单独测一次 hit。共 24 条观测、16 次测量
server dispatch，另有 2 次未进入奖励的预热和 doctor 探测。这是显式诊断实验，
不影响普通请求默认单执行。只有完整结果与预热参照实际一致时标 MATCHED。

本次 12 VALID（q4）、12 LOW_CONFIDENCE（q5，UNSUPPORTED_TEMPLATE），全部差分一致。
q5 仍能执行精确 SQL 缓存，但 Agent 1 无受支持模板，故所有 reward 为 null。
q4 LOW/SERVER：S0 n=4 中位 4.254 ms，S1 miss n=4 中位 4.478 ms，候选未优于 Native；
q4 LOW/CACHE_HIT：S1 n=4 中位 0.112 ms，与 SERVER 隔离，无 Native 同组参照。
实际结果不支持声称统计建议已在该 Benchmark 中提升生产性能。
不同负载下建议不同的功能由明确模拟数据验证，真实实验未制造 DB 负载，不能替代。
JSON 保留全部经验上下文、指标来源、质量原因、真实 IDs、奖励和统计成员，便于复核。

## 8. 当前限制

DB 主机/服务端指标没有真实采集源，始终 UNKNOWN；只有客户端真实快照。
没有跨进程在线探索、Bandit、漂移检测、自动调参、RAG 或向量检索。
经验记录是显式调用，CLI/Orchestrator 闭环、独立评估集和生产学习曲线均未实现。
S1 原有固定 epoch、进程内缓存和并发限制不因 L1 实现而解除，见 STRATEGY_CATALOG.md。
本阶段结果是可靠记录和 L1 决策支持，不是完整系统完成声明。

## 9. 修复后真实复核（2026-11-02）

新增独立证据 evidence/phase5_learning_fixes_2026-11-02.json，保留原 Benchmark 文件
作为修复前历史，不给旧经验补造质量证明。仍为 24 条真实观测、16 次测量 dispatch，
12 VALID / 12 LOW_CONFIDENCE；全部经验记录 quality_version=2 和资源策略指纹。
q4 LOW/SERVER：S0 n=4，中位 6.627 ms；S1 miss n=4，中位 6.683 ms。
q4 LOW/CACHE_HIT：S1 n=4，中位 0.087 ms。没有达到策略切换证据，不声明生产收益。
q5 仍因 UNSUPPORTED_TEMPLATE 不训练；DB 负载仍 UNKNOWN。

## 阶段 7 补充：离线 Bandit 实验边界（2026-11-02）

L0/L1 的生产逻辑与阶段 6 链路保持原契约。新增离线 disjoint LinUCB、17 维固定编码、
真实/合成域隔离、单 arm 更新、去重、冻结时间窗、独立 SQLite 原子模型保存与回滚。
详细参数/公式/准入/LLM 接口/真实留出评估见 [PHASE7_DESIGN.md](PHASE7_DESIGN.md)。
当前真实 SERVER 每 arm 仅 4 个合格训练样本，小于 30 门槛，缺少负载覆盖和概率日志；
模型固定 online_ready=false，不产生线上 AdaptiveAdvice。新 Benchmark 24 条记录只用于
冻结评估，不反哺训练；未执行 arm 的奖励保持 null。真实 LLM 质量和时延 UNKNOWN，
只有本地受限接口对照。阶段 7 三项离线缺陷已修复复审 PASS（见 PHASE7_REREVIEW.md），真实启用仍 BLOCKED，不代表可启用真实自适应决策。

## 阶段7受控随机试验增量（2026-11-02）

新协议 `randomized-cache-state-v1` 详见 [PHASE7_EXPLORATION.md](PHASE7_EXPLORATION.md)。使用执行前EMPTY/READY缓存状态和背景线程配置建立独立实验分区，真实Experience先通过原准入检查，再仅更新实际arm。随机分配概率先于执行落盘，训练/留出严格隔离；离线IPS明确不等于真实部署或端到端收益。未更改旧reward/encoder或Experience schema、未将CACHE_HIT混入旧SERVER分区、未开启生产策略。当前770条均为LOW实测负载，后续仍需隔离实例及监控证据。

## CPU活跃协议隔离（2026-11-02）

阶段7新增显式`cpu-activity-v2`，默认仍为历史`resource-max-v1`。LearningContext序列化在历史协议下省略新增字段，保持旧JSON与SQLite context key；v2显式携带标识，使L1 group key、legacy Bandit partition及随机试验partition与v1隔离。L1质量准入、Orchestrator前后上下文及Bandit真实样本准入均使用context中的协议重新核算标签。Analyzer拒绝协议与trial不一致。

负载分档变化不修改reward、资源闸门、语义认证、完整消费要求或部署锁。新筛选数据只用于选择可执行的实验查询，不直接转成Experience或模型学习。0.2秒CPU窗口仍是短时间整机采样，不应被称为稳定压力或IoTDB进程独占状态。阶段7真实收益仍需后续独立验证。


## 2026-11-02：冻结模型的实际端到端受控验证

CPU v3 使用每个采样器独立计数，拒绝无测量标识的旧快照，避免同线程 client/DB 基线互相覆盖。v3 不与历史 v1/v2 模型混训。

`phase7_controlled_e2e.py` 是显式实验入口，不启用生产 Bandit advice。采用 LOW/MEDIUM 实测 CPU × EMPTY/READY 缓存条件，S0/S1 随机训练，准入要求真实 VALID、完整结果与原始 CSV/Native 基准匹配。模型分别按已知缓存条件建模，CPU 为上下文特征；不能把两个缓存模型的不同选择冒充单模型 CPU 自适应。每个实际负载/缓存/策略训练单元至少 30 样本，保留随机序列而不事后平衡删样本。

冻结训练截止时间和参数后，另一条 SQL 做留出，S0、固定 S1、自适应分别实际执行，每种负载/缓存至少 30 个配对块，块内随机顺序。实验建议仍经过 Registry/Validator，安全降级优先。原有 L1 反馈属于实测开销，但不会更新冻结 Bandit。错误或低质量结果中止整批；不删失败记录后认证。

端到端收益预声明与两个实际固定策略分别比较：按负载/缓存分层配对 bootstrap 4000 次，99% 区间上界必须小于 0，连续三个 10 块批次均需负均值。单轮实验不证明跨会话复现，单个 COUNT 模板不证明全工作负载泛化；预测误差、缓存命中或模型参数变化都不能替代实际收益。部署锁保持关闭。

EMPTY/READY 只描述客户端 S1 结果缓存，不表示 IoTDB 内部冷/热缓存；数据库内部缓存状态未控制。实验三种策略均经过完整 Orchestrator，其 S0 是原始 SQL 的工作流基线，不冒充裸 Adapter 的 E0。


## 2026-11-03：保持准入语义的开销优化

L1 build_model 复用按完整 body_json 验证的不可变统计投影（最多128条，单条超过262144字符不缓存）。仍读取数据库当前行；隔离或更正改变body即失效，Pydantic验证不跳过；publish仍重算并核对revision。无样本删除、无持久化省略，无奖励公式变更。Greedy LinUCB 不再计算未使用的UCB项；实际均值和选择规则不变。

Orchestrator 同步请求内允许精确SQL的确定性验证事实复用，生命周期结束即清理；动态资源、Registry资格、隔离和LLM建议不缓存。私有AST深拷贝避免调用者篡改传播。

真实验证重新训练，使用另一条COUNT查询留出；旧失败轮和此次中止轮不复用。更低的框架开销不能替代与实际S0及固定S1的独立对照，生产模型仍锁定。详见 PHASE7_OVERHEAD_FIX.md。

2026-11-03最终复测：480训练、360留出；120次自适应均选S1，没有CPU档位策略切换证据。MEDIUM/READY/ADAPTIVE仅29条，禁止认证。Adapter延迟reward与完整E2E目标的一致性仍待解决，生产online_enabled仍false。详见PHASE7_OVERHEAD_FIX.md。

## 2026-11-03：完整请求目标对齐

阶段7实验可显式使用`--e2e-reward`，由`CompletedRequest`在Orchestrator.run返回后绑定完整耗时、UTC、query/decision/execution ID与原工作流/经验摘要。`OfflineLinUCB.update_completed_request`先执行原Experience策略认证、资源、质量及版本准入，再用完整请求延迟更新被执行arm。旧`update_experience`拒绝更新新目标模型；反向使用新receipt更新旧模型同样拒绝。新模型分区及reward_version隔离，重复execution不重复训练，冲突证据与请求完成时间越过训练截止线均拒绝。

采用线性`reward=-end_to_end_ms/1000`，与平均E2E验收一致；原log变换并不严格保留平均延迟偏好。新测量包含同步L1持久化成本，不能在同一请求结束前训练其完整延迟。当前在独立训练阶段结束后离线拟合并冻结，再实际执行后续留出策略；无生产在线回调或异步队列声明。旧L1默认Adapter统计仍作历史兼容，不能说已迁移全部生产学习目标；显式实验决策只使用新模型，生产online_enabled=false。

策略适用性按实际S0/S1成对执行报告，只有完整结果匹配、原始上下文合格且数量达标才形成区间。新自然负载协议v8仅供适用性验证，强制不得满足多负载阶段验收。详见PHASE7_E2E_REWARD_SCENARIOS.md。

本轮实际结果：小/大规模自然LOW各240训练+216留出，完整请求模型参数独立重建；S1在READY均有条件收益，EMPTY均无稳定差异。大规模v9在模型冻结并写入审计日志后释放无用训练工作流，保留原始journal；释放前后模型摘要一致，不关闭GC或删除失败经验。多负载与自适应额外收益仍未认证，详见PHASE7_E2E_REWARD_SCENARIOS.md。
# 2026-11-03 补充：L1 事实缓存与序列反馈边界

默认Python运行环境为Conda `MANTO`。`learning/repository.py` 的统计缓存只保存完整Pydantic校验后不可变字段，使用完整body的SHA-256作为键，内容变更（包括隔离、质量修正）重新校验。最多4096条、4MiB估计字段/容器预算，超过262144字符的body不缓存；估计预算不是RSS上界。该优化不改变任何经验、统计公式、样本门槛、奖励或模型revision。冷缓存仍需完整验证；大于容量的工作集仍可发生淘汰，统计重建和样本ID持久化仍有线性成本。

完整请求序列实验记录首次访问/填充/命中及安全回退总成本，保留低质量成功请求的测量但不把它们用于学习。当前冻结单请求模型在真实序列中没有形成有效填充和复用；即使固定S1在长度8序列中收益成立，也不能宣称模型已学会序列收益。新的序列级/延迟反馈协议尚未实现，不把未来未发生请求的收益当作即时标签。详见[阶段7序列验证](PHASE7_SEQUENCE_VALIDATION.md)。


## 2026-11-03：有限批次归因与持久化延迟反馈

新增`BatchContract`/`BatchFeedback`协议，只支持调用方事先声明的同SQL有限批次（2或8次），不是对未知未来流量的预测。S1选项包含首次Native安全前缀、实际填充及实际命中；使用`negative-batch-service-seconds-v1`，对完整已结束批次分配一次真实负服务耗时标签。未发生的命中、未执行选项没有标签；同一query/execution不能跨批重复归因。奖励与旧请求奖励/分区隔离，原按请求更新接口不能更新新模型。

`DelayedBatchStore`使用独立SQLite WAL/FULL：完成后入队，下一个批次前显式消费，原子提交模型版本和队列ACK。重复相同反馈幂等，冲突拒绝；质量不合格明确REJECTED，非预期错误事务回滚并传播。重启从持久化head恢复，冻结前要求队列排空，冻结后拒绝反馈。它是可恢复的延迟调度接口，当前实验没有后台学习线程或生产异步吞吐承诺。

显式批次路径使用`LearningFeedbackAgent(statistics_mode='record_only')`，保留质量校验和经验持久化，仅停止本路径不使用的同步L1统计发布；默认路径仍为`synchronous`。请求计时包含这些工作，队列提交与学习更新另计。新CLI在每批开头清空客户端结果缓存，由Registry/Validator逐请求约束，生产Bandit保持禁用。完整实施协议、失败记录和实际验证见[阶段7批次反馈](PHASE7_DELAYED_BATCH_FEEDBACK.md)。

阶段7后续验证改为预声明45块固定预算（每单元最低30有效批次不变），保留全部预定留出成本，不事后补样或删掉跨档成本。v5的spawn压测进程隔离与TCP占用检查只属于实验基础设施，不改变BatchFeedback奖励或默认生产学习路径。最新实现尚待用户执行测试；测试命令与结果边界见PHASE7_STAGGERED_VALIDATION.md。
