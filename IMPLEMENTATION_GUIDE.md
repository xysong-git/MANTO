# Multi-Agent TSDB Optimizer
## 代码实施与 AI 协同开发执行手册

**版本：** V1.0  
**编制日期：** 2026-11-01  
**目标平台：** Apache IoTDB（具体版本、树模型/表模型在阶段 0 确认）  
**主要语言：** Python 3.12  
**用途：** 交给 Cursor、Claude Code、Codex 等代码 AI，按阶段实现可运行、可测试、可复现实验的四 Agent 自适应查询优化系统。

---

# 使用说明：先读这一页

本手册不是让代码 AI 一次性生成整个项目，而是要求它按阶段实现、测试、提交结果。**每次只把“总控提示词 + 当前阶段提示词”交给代码 AI。** 阶段验收通过后，才进入下一阶段。

## 执行规则

1. 将本手册 Markdown 文件放入代码仓库根目录，命名为 `IMPLEMENTATION_GUIDE.md`。DOCX 版用于阅读，Markdown 版用于代码 AI 检索和复制提示词。
2. 首次启动时，先执行“总控提示词”和“阶段 0 提示词”。不要让 AI 直接创建四个 Agent 或生成全部代码。
3. 每个阶段要求代码 AI：先检查现有代码和环境，再给出实现计划，然后小步修改，最后运行测试并报告结果。
4. 代码 AI 不得伪造 IoTDB 接口、执行计划字段、SQL 方言、实验结果或策略效果。无法确认时必须标注 `UNKNOWN` 并给出验证步骤。
5. 每个阶段完成后，检查本手册列出的验收标准、测试结果和交付物。不能只看 AI 的文字总结。
6. 要求 AI 维护 `docs/IMPLEMENTATION_STATUS.md`，记录已完成阶段、待办、未解决问题、实际环境信息和测试命令。
7. 每个阶段完成后做 Git commit；出现大范围重构时先建立分支，避免覆盖已验证代码。

## 本项目的核心成功条件

最终系统必须证明：在具有至少两个经过验证、可实际执行且语义等价的策略时，系统能够结合查询特征和运行状态选择策略；执行后采集真实反馈；学习模块根据反馈更新后续决策；相较 Native 或固定策略基线，在受控实验中取得可重复的收益。

**如果阶段 0 无法找到任何有效的非 Native 优化策略，不得虚构策略继续宣称优化闭环已经成立。** 可以先完成框架、监控和实验工具，但必须把“缺少可控优化动作”列为研究阻塞项，并先解决策略入口问题。

---

# 1. 项目目标、研究边界与可行性结论

## 1.1 最终目标

实现一个由四个逻辑 Agent 协作的时序数据库自适应查询优化系统：

- **Agent 1：查询解析 Agent**——解析受支持的 SQL，提取查询结构和查询特征。
- **Agent 2：优化决策 Agent**——结合查询特征、数据库能力、当前系统状态和历史经验，在受约束的策略集合中选择可执行方案。
- **Agent 3：性能监控 Agent**——采集查询执行指标和数据库服务器资源状态，核验策略是否生效，并形成结构化执行反馈。
- **Agent 4：学习反馈 Agent**——保存执行经验、评估策略效果、更新经验统计和在线学习模型，为后续查询提供可量化的决策依据。

系统外部由 **Orchestrator（协同调度器）** 控制工作流；由 **Database Adapter（数据库适配器）** 统一访问 IoTDB；由 **Strategy Registry（策略注册表）** 管理经审计的优化策略。

四个 Agent 是四个逻辑组件，不要求四个独立 LLM。第一版必须优先保证确定性、可观测性和可复现性。

## 1.2 可行性结论

整体工程可行，但有四项前置条件：

1. 确认 IoTDB 服务版本、数据模型（树模型或表模型）、客户端版本和 SQL 能力。
2. 找到至少一个非 Native、具有真实执行入口的优化策略，并通过语义等价与生效验证。
3. 能获取足够可靠的查询耗时与运行状态。若数据库远程部署，不能只采集运行 Python 客户端的机器指标。
4. 在线学习必须建立在多个策略的可比样本和受控探索之上；单纯保存日志或 RAG 检索不能作为“在线学习已实现”的证据。

## 1.3 本项目不做什么

第一版明确不做：

- 不修改 IoTDB 源码。
- 不允许 LLM 任意生成物理执行计划或直接执行任意 SQL。
- 不要求四个 Agent 都由独立 LLM 驱动。
- 不一开始引入分布式 Agent 框架、消息队列、微服务或 Kubernetes。
- 不在正式查询路径中无条件重复执行 Native SQL 来验证结果。
- 不把一次运行更快直接判定为策略有效。
- 不在没有数据的情况下声称模型已经学会最优策略。
- 不把客户端 CPU 使用率误称为数据库服务器 CPU 使用率。

## 1.4 第一版目标与后续扩展边界

**MVP 必须完成：** 数据库连接、能力审计、SQL 解析、查询特征、资源采集、策略注册、受约束决策、执行记录、经验库、基本在线策略更新、完整工作流、Benchmark。

**后续扩展：** LLM 辅助分析、语义经验向量检索、复杂代价模型、更多数据库适配器、并发查询调度、多模型路由、分布式部署。

---

# 2. 对上一版方案的工程修正

以下修正是强制要求，代码 AI 不得退回到未经验证的简化设计。

| 项目 | 必须采用的修正 | 原因 |
|---|---|---|
| SQL Parser | 采用 Parser Adapter；先验证 IoTDB SQL 样例。SQLGlot 仅作为候选，不默认完整支持 IoTDB 方言 | SQLGlot 官方维护方言列表未列出 IoTDB；解析成功也不等于数据库一定接受 |
| IoTDB API | 根据版本和树/表模型选择客户端与 SQL 接口，做启动能力探测 | 客户端与服务端版本不匹配可能产生连接或行为问题 |
| EXPLAIN | 将 EXPLAIN / EXPLAIN ANALYZE 能力按模型和版本适配 | JSON 格式、语法和输出字段存在版本差异 |
| EXPLAIN ANALYZE | 仅放在诊断/Benchmark 路径，不能默认对每次正式查询额外执行 | 它会真实执行目标查询，影响耗时与缓存状态 |
| 资源监控 | 明确 `client_host` 与 `db_host`；只把数据库服务器指标标为 DB 指标 | psutil 默认采集的是运行 Python 的本机，不会自动监控远端数据库 |
| 查询计时 | 同时记录 execute 调用、完整结果拉取、客户端端到端耗时 | 结果集可能惰性读取；只计 execute 返回时间会低估实际查询时间 |
| 正确性验证 | 基于独立回归数据集做差分测试；在线请求默认不双执行 | 双执行增加开销并污染缓存，且 Native 重复执行并非总能安全/可比 |
| 策略集合 | 策略必须来自能力审计，不能预设 S1/S2 一定存在 | 理论优化方向不等于数据库对外有控制入口 |
| 学习算法 | 先有可靠日志和策略统计，再启用 Contextual Bandit | 缺少样本、动作探索和上下文质量时，Bandit 更新没有意义 |
| 目标指标 | 同时报 DB execution latency、Agent overhead、end-to-end latency | 数据库内变快不代表用户整体响应更快 |

---

# 3. 系统总体架构与执行链路

## 3.1 逻辑架构

```text
                    User / Benchmark Workload
                              |
                              v
                    Orchestrator / Controller
                              |
             +----------------+----------------+
             |                |                |
             v                v                v
      Agent 1             Agent 3          Agent 4
   Query Analysis     System Snapshot    Experience Query
             |                |                |
             +----------------+----------------+
                              |
                              v
                       Agent 2
                 Optimization Decision
                              |
                              v
                  Candidate Strategy Set
                              |
                              v
                Safety / Capability Validator
                              |
                              v
                    Database Executor
                              |
                              v
                         IoTDB
                              |
                              v
                    Agent 3 Feedback
                              |
                              v
                    Agent 4 Learning
                              |
                              v
           Experience Store + Policy/Model Update
                              |
                              +----> Future Agent 2 Decisions
```

Agent 3 还要在后台周期性采集系统资源状态。Orchestrator 在 Agent 2 决策前读取最新的、未过期的 `SystemSnapshot`。

## 3.2 单条查询工作流

1. 接收 SQL，创建 `query_id`、`request_id`，记录输入时间。
2. Agent 1 解析 SQL；不支持的语法标记为 `UNSUPPORTED`，禁止自动改写。
3. Agent 3/State Manager 读取最新系统快照，并附带采样时间和数据来源。
4. Agent 4 查询相似历史经验和各策略统计。没有历史经验时返回空经验，不得编造。
5. Agent 2 从已注册策略中生成候选，并依据硬约束、历史估计和策略模型选择方案。
6. Validator 检查策略是否启用、是否适用、SQL 是否属于只读范围、策略版本是否兼容。
7. Executor 执行一次被选中的查询；记录完整结果拉取过程和异常。
8. Agent 3 汇总实际指标，检查计划/策略是否生效；记录诊断可信度。
9. Agent 4 保存执行经验，满足更新门槛时更新策略统计或 Bandit 参数。
10. 返回查询结果及可选诊断信息。

## 3.3 工作流状态机

```text
RECEIVED
  -> PARSING
  -> CONTEXT_READY
  -> DECIDING
  -> VALIDATING
  -> EXECUTING
  -> MONITORING
  -> LEARNING
  -> COMPLETED
```

异常状态：`REJECTED`、`UNSUPPORTED`、`FAILED`、`TIMEOUT`、`FALLBACK`。

必须区分：

- `REJECTED`：安全策略拒绝执行。
- `UNSUPPORTED`：当前 Parser/数据库能力不支持自动优化。
- `FAILED`：查询或系统执行失败。
- `TIMEOUT`：达到超时限制；必须尽量确认服务器端请求状态，不能直接并发重试。
- `FALLBACK`：优化执行失败后，确认安全可重试，再回退到原始 SQL。

学习写库或模型更新失败不能改变已经成功取得的查询结果，也不能导致查询被重新执行。

---

# 4. 技术栈和环境约束

## 4.1 第一版技术选型

| 领域 | 推荐方案 | 约束 |
|---|---|---|
| Python | Python 3.12 | 与当前实验环境保持一致；依赖版本写入 lock 文件 |
| 数据验证 | Pydantic v2 | Agent 间只传结构化模型，不依赖自然语言传关键参数 |
| SQL 解析 | SQLGlot 候选 + IoTDB Parser Adapter | 对实际 SQL 语法做 fixture 测试；解析失败安全降级 |
| IoTDB 访问 | 官方 Python Native API 或 JDBC 适配器 | 严格匹配 server/client 版本和数据模型 |
| 经验库 | SQLite | 单机 MVP 足够；使用事务和索引 |
| 指标采集 | psutil + IoTDB 诊断接口/远端主机指标代理 | 区分客户端和 DB 服务器 |
| 学习 | NumPy + 自实现统计模型/LinUCB | 初期先跑通统计反馈，再启动 Bandit |
| LLM | OpenAI-compatible API，可选 | 初始默认关闭；无 LLM 时必须完整运行 |
| 测试 | pytest | 每阶段新增单元测试和集成测试 |
| CLI | argparse 或 Typer | 第一版只需 CLI，不强制 Web 服务 |
| 日志 | 标准 logging 或 Loguru | 所有记录带 query_id / decision_id |

不要在没有验证的情况下锁定“最新版”依赖。代码 AI 应先检查目标 Python 环境、IoTDB 版本和客户端兼容，再生成 `pyproject.toml`/`requirements.txt` 与 lock 文件。项目交付时必须记录实际安装版本。

## 4.2 IoTDB 适配器规则

定义统一抽象接口，业务模块禁止直接导入 IoTDB SDK：

```python
class TSDBAdapter:
    def connect(self): ...
    def close(self): ...
    def get_server_version(self): ...
    def get_database_model(self): ...
    def execute_read_query(self, sql, timeout_ms=None): ...
    def explain(self, sql): ...
    def get_capabilities(self): ...
```

`execute_read_query` 必须消费完整结果集或显式提供 streaming 计时模式。结果集关闭必须放在 `finally`/上下文管理器中。

对 IoTDB：

- 树模型和表模型分别实现能力适配；不要混用 SQL 语法、Session 类和 EXPLAIN 解析规则。
- 根据实际 server 版本决定安装哪一代 Python client；官方文档明确提示不要用高版本客户端连接低版本服务。
- 在启动时运行只读能力探测：连接、版本、模型、简单 SELECT、EXPLAIN（若支持）。探测失败时停止自动优化，而不是猜测。
- 若使用官方 Python API，依据模型选择相应 Session / TableSession；客户端 API 返回的结果集应按官方文档方式迭代，不要混用不同结果读取方式。

## 4.3 SQL Parser 适配规则

定义：

```python
class SQLParserAdapter:
    def parse(self, sql: str) -> ParseResult: ...
    def extract_features(self, parsed: ParseResult, metadata=None) -> QueryFeatures: ...
```

要求：

- Parser 只承诺对已通过测试的语法子集正确。
- 用至少 30 条真实 IoTDB 查询建立 `sql_fixtures/`，覆盖合法查询、边界条件、特殊标识符、函数、时间条件、聚合和不支持语句。
- 解析器输出原始 SQL、AST/解析对象、规范化模板、支持状态和错误原因。
- 不允许用正则表达式替代完整 SQL 解析。正则可用于辅助抽取，但不能决定 SQL 是否安全或语义等价。
- 对未知 AST 节点采用 fail-closed：可以 Native 执行（前提是安全分类可靠），但不能自动改写。
- LLM 不负责提供唯一 AST，也不得成为 SQL 安全校验器。

---

# 5. 建议代码目录与模块职责

采用模块化单体结构。不要一开始拆微服务。

```text
MultiAgent-TSDB-Optimizer/
├── README.md
├── IMPLEMENTATION_GUIDE.md
├── pyproject.toml
├── .env.example
├── configs/
│   ├── app.yaml
│   ├── database.yaml
│   ├── strategies.yaml
│   └── learning.yaml
├── docs/
│   ├── IMPLEMENTATION_STATUS.md
│   ├── DB_CAPABILITY_AUDIT.md
│   ├── STRATEGY_CATALOG.md
│   └── EXPERIMENT_PROTOCOL.md
├── src/tsdb_optimizer/
│   ├── main.py
│   ├── orchestrator/
│   │   ├── controller.py
│   │   ├── workflow.py
│   │   ├── state_manager.py
│   │   └── error_handler.py
│   ├── agents/
│   │   ├── base.py
│   │   ├── query_analysis/agent.py
│   │   ├── optimization_decision/agent.py
│   │   ├── performance_monitor/agent.py
│   │   └── learning_feedback/agent.py
│   ├── schemas/
│   │   ├── query_context.py
│   │   ├── query_features.py
│   │   ├── system_state.py
│   │   ├── strategy.py
│   │   ├── optimization_decision.py
│   │   ├── execution_record.py
│   │   └── learning_feedback.py
│   ├── adapters/iotdb/
│   │   ├── base.py
│   │   ├── tree_adapter.py
│   │   ├── table_adapter.py
│   │   └── capability_probe.py
│   ├── parsing/
│   │   ├── parser_adapter.py
│   │   ├── sqlglot_parser.py
│   │   └── feature_extractor.py
│   ├── optimizer/
│   │   ├── strategy_base.py
│   │   ├── strategy_registry.py
│   │   ├── candidate_generator.py
│   │   ├── plan_validator.py
│   │   └── cost_estimator.py
│   ├── executor/
│   │   ├── query_executor.py
│   │   └── fallback_manager.py
│   ├── monitoring/
│   │   ├── system_sampler.py
│   │   ├── query_metrics.py
│   │   ├── db_host_metrics.py
│   │   └── plan_analyzer.py
│   ├── learning/
│   │   ├── experience_store.py
│   │   ├── experience_retriever.py
│   │   ├── strategy_statistics.py
│   │   ├── reward.py
│   │   ├── contextual_bandit.py
│   │   └── model_manager.py
│   ├── llm/
│   │   ├── client.py
│   │   └── prompts/
│   └── storage/
│       ├── sqlite.py
│       └── repositories.py
├── tests/
│   ├── unit/
│   ├── integration/
│   ├── fixtures/sql/
│   └── fixtures/plans/
├── benchmarks/
│   ├── workload/
│   ├── run_benchmark.py
│   ├── compare_runs.py
│   ├── analyze_learning.py
│   └── results/
├── data/                 # 本地运行数据，不提交大文件
└── logs/                 # 运行日志，不提交敏感信息
```

代码 AI 可以按实际进度分批创建文件，不要在阶段 0 就创建大量空文件。目录结构是目标结构，不是一次性生成任务。

### 模块边界

- `agents/`：组织本 Agent 的输入、调用核心服务、生成结构化输出。
- `adapters/`：所有数据库客户端实现。
- `parsing/`：SQL 解析和特征提取。
- `optimizer/`：策略注册、候选生成、成本评估和决策验证。
- `executor/`：执行一次选定查询、记录耗时和处理安全回退。
- `monitoring/`：系统/数据库/查询级指标采集。
- `learning/`：经验检索、统计、奖励和学习算法。
- `orchestrator/`：管理调用顺序、状态机和异常隔离。
- `schemas/`：所有模块共享的 Pydantic 数据契约。

---

# 6. 四个 Agent 的精确定义

## 6.1 Agent 1：查询解析 Agent

**目标：** 将原始 SQL 转换为可信的、可供优化决策使用的结构化查询上下文。

输入：`QueryContext`（SQL、query_id、数据库模型和版本）。

处理：Parser 解析 -> 支持范围判断 -> AST 特征提取 -> 可选元数据补充 -> 查询类型多标签分类。

输出：`QueryAnalysisResult`，至少包含：

- `query_id`、`query_hash`、`normalized_template`；
- `parse_status`：`SUCCESS` / `UNSUPPORTED` / `ERROR`；
- `query_types`：允许多个标签，例如 `TIME_FILTER`、`AGGREGATION`；
- `time_range_ms`（无法静态计算时为空）；
- `device_or_series_count`（标记来源：SQL/metadata/unknown）；
- `filter_count`、`aggregation_types`、`has_group_by`、`has_order_by`、`has_limit`；
- `estimated_rows`（只有数据库统计信息可支持时填写）；
- `features_missing`、`unsupported_constructs`。

第一版不需要 LLM。Agent 1 不得推断不存在的统计信息；未知值必须为 `None`，并记录来源与可信度。

**验收：** 30～50 条 fixture 覆盖支持 SQL；特征字段与人工预期一致；未知语法安全降级；同输入和同元数据得到一致输出。

## 6.2 Agent 2：优化决策 Agent

**目标：** 从当前数据库版本可执行、已验证的候选策略中选择方案，不是任意生成 SQL。

输入：`QueryAnalysisResult`、有效 `SystemSnapshot`、Agent 4 的 `HistoricalAdvice`、当前 `StrategyRegistry`、数据库能力列表。

处理：

1. 过滤不适用策略。
2. 生成候选方案。
3. 对候选检查语法/能力/资源约束/历史可信度。
4. 预测或统计各候选在当前上下文下的效果。
5. 根据 `baseline` / `rule_based` / `adaptive` 模式选择。
6. 输出可执行计划与 fallback。

输出：`OptimizationDecision`，包含：

- `decision_id`、`query_id`；
- `selected_strategy_id`、`strategy_version`；
- `original_sql`、`candidate_sql`（若策略不改写 SQL，则与原 SQL 相同）；
- `execution_options`（只能是适配器已登记且已验证的参数）；
- `expected_latency_ms`（可空）、`confidence`（可空）、`decision_reason`；
- `fallback_strategy_id`；
- `decision_mode`、`model_version`。

决策原则：

- S0 Native 永远存在，永远是最后安全 fallback。
- 任何策略必须在 `StrategyRegistry` 里注册并通过启用条件。
- LLM（后续可选）只能在给定候选策略中提出建议；其输出不能绕过 Validator。
- 未知或过期系统状态不得作为“高置信度实时负载”输入；此时退化到安全决策。
- 没有足够经验时，采用基线或可解释规则，不假装有已训练模型。

**验收：** 至少两种已验证策略时，可在输入特征/负载变化时产生可解释的策略选择；未知策略 ID 无法下发；超阈值资源状态能触发保守策略。

## 6.3 Agent 3：性能监控 Agent

**目标：** 记录一次查询的真实执行过程，并将查询表现与当时系统状态关联。

指标至少分为：

**查询/客户端级：**

- `client_wall_ms`：从调用查询到完整结果集消费完毕的墙钟时间；
- `execute_call_ms`：发起执行到服务端返回结果集句柄/首个响应的时间（按 SDK 实际语义定义）；
- `result_fetch_ms`：完整拉取结果的时间；
- `planning_ms`：仅数据库能提供时记录；
- `rows_returned`、`success`、`error_code`、`timeout`。

**数据库服务端级：**

- DB 主机 CPU、内存、磁盘 I/O、进程资源；
- IoTDB 查询/算子统计（如果版本和模型支持）；
- 数据库并发查询数（只有来源可靠时记录）。

**客户端主机级：**

- 可额外记录客户端 CPU/内存，但字段必须带 `client_` 前缀，不能冒充 DB 主机指标。

输出：`ExecutionRecord` + `SystemSnapshot` 序列/汇总。

计划核验：优先使用无副作用的 `EXPLAIN`；`EXPLAIN ANALYZE` 归入诊断或 Benchmark 模式，因为它会执行查询。计划 hash 应剔除不稳定的 ID/时间字段，只保留可比较的结构特征。

**验收：** 能按 query_id 对应决策与执行记录；计时覆盖完整结果拉取；采集点带时间戳/来源/有效性；远程数据库不能错误使用客户端指标替代服务端指标。

## 6.4 Agent 4：学习反馈 Agent

**目标：** 将真实反馈转化为后续决策能够复用的数值经验，并在样本满足要求时更新策略模型。

Agent 4 的三个子模块：

1. **Experience Store：** 保存查询特征、执行前状态、选择策略、实际计划、执行结果、环境和版本信息。
2. **Experience Retriever：** 按查询类型、特征相似度、负载区间、数据库版本和策略版本检索历史样本。第一版使用 SQL 结构化检索，不需要向量数据库。
3. **Policy Learner：** 更新每个策略的效果统计；达到样本和探索门槛后启用 Contextual Bandit。

经验记录至少要有：

- `query_id`、`decision_id`、`execution_id`；
- query hash/template 与特征；
- 数据库版本/模型/数据规模；
- 系统状态（before/during/after）及来源；
- 选择策略、策略版本、执行计划 hash；
- client wall time、资源指标、行数、错误信息；
- 策略认证状态：`PASSED` / `FAILED` / `NOT_CERTIFIED`（关联策略版本和回归测试套件）；
- 运行时结果检查：`NOT_CHECKED` / `MATCHED` / `MISMATCHED`（普通在线请求通常为 NOT_CHECKED，不做双执行）；
- 样本质量：`VALID` / `LOW_CONFIDENCE` / `INVALID`；
- 学习模型版本、奖励值和更新时间。

**严禁：** 只保存自然语言总结；把一次成功直接视作有效经验；把 `runtime_result_check=NOT_CHECKED` 写成 `MATCHED`，或把 `MISMATCHED` 样本纳入正向奖励；把不同数据库版本、缓存实验条件和策略版本无差别混合。

---

# 7. 数据契约（Agent 间统一协议）

代码 AI 必须先实现并测试这些 Pydantic schema，再编写 Agent 业务逻辑。字段可扩展，但不允许各模块私自创建同义字段。

## 7.1 通用字段

所有核心记录至少带：

- `query_id`：一次逻辑查询的 ID；
- `decision_id`：一次优化决策的 ID；
- `execution_id`：一次物理执行尝试的 ID；
- `created_at`：UTC 时间；
- `schema_version`：协议版本。

一次逻辑查询可能有一次优化执行和一次安全回退执行，因此不能假定一个 query_id 只对应一条 execution record。

## 7.2 核心数据对象

```python
class QueryContext(BaseModel):
    query_id: str
    original_sql: str
    submitted_at: datetime
    database_model: Literal["tree", "table", "unknown"]
    server_version: str | None = None
    mode: Literal["baseline", "rule_based", "adaptive"]

class QueryFeatures(BaseModel):
    query_hash: str
    normalized_template: str | None = None
    query_types: list[str] = Field(default_factory=list)
    time_range_ms: int | None = None
    device_count: int | None = None
    filter_count: int | None = None
    aggregation_types: list[str] = Field(default_factory=list)
    has_group_by: bool | None = None
    has_order_by: bool | None = None
    has_limit: bool | None = None
    estimated_rows: float | None = None
    feature_sources: dict[str, str] = Field(default_factory=dict)
    missing_features: list[str] = Field(default_factory=list)

class SystemSnapshot(BaseModel):
    captured_at: datetime
    source_host: str
    source_type: Literal["db_host", "client_host", "db_metrics", "unknown"]
    cpu_percent: float | None = None
    memory_percent: float | None = None
    disk_read_bytes_per_sec: float | None = None
    disk_write_bytes_per_sec: float | None = None
    active_queries: int | None = None
    valid: bool = True
    stale_after_ms: int

class OptimizationDecision(BaseModel):
    decision_id: str
    query_id: str
    selected_strategy_id: str
    strategy_version: str
    candidate_sql: str
    execution_options: dict[str, str | int | float | bool] = Field(default_factory=dict)
    expected_latency_ms: float | None = None
    confidence: float | None = None
    decision_mode: str
    model_version: str | None = None
    fallback_strategy_id: str = "S0_NATIVE"
    reason: str

class ExecutionRecord(BaseModel):
    execution_id: str
    query_id: str
    decision_id: str
    strategy_id: str
    started_at: datetime
    finished_at: datetime | None = None
    execute_call_ms: float | None = None
    result_fetch_ms: float | None = None
    client_wall_ms: float | None = None
    rows_returned: int | None = None
    success: bool
    strategy_certification_status: Literal["PASSED", "FAILED", "NOT_CERTIFIED"]
    runtime_result_check: Literal["NOT_CHECKED", "MATCHED", "MISMATCHED"] = "NOT_CHECKED"
    actual_plan_hash: str | None = None
    error_type: str | None = None
    error_message: str | None = None

class LearningFeedback(BaseModel):
    query_id: str
    execution_id: str
    sample_status: Literal["VALID", "LOW_CONFIDENCE", "INVALID"]
    reward: float | None = None
    model_updated: bool
    model_version: str | None = None
    feedback_summary: str
```

代码 AI 可在实际实现中补充类型，但必须保持字段含义、单位、来源和空值语义一致。时间统一用 UTC；延迟统一用毫秒；CPU/内存百分比统一用 0–100，而不是有时用 0–1。

---

# 8. 优化策略注册与验证机制

## 8.1 策略不能靠命名创建

`S0_NATIVE` 必须实现。其他策略先由阶段 0 审计产生。策略目录中可以有模板，但不能把未验证策略设为 enabled。

每条策略必须有 `StrategySpec`：

```python
class StrategySpec(BaseModel):
    strategy_id: str
    name: str
    database_model: str
    compatible_server_versions: list[str]
    enabled: bool
    implementation_class: str
    applicability_rules: dict
    execution_control: str
    semantic_test_suite: str
    plan_effect_expectation: str | None = None
    known_limitations: list[str] = Field(default_factory=list)
    version: str
```

每条策略需提供统一接口：

```python
class OptimizationStrategy(Protocol):
    def is_applicable(self, query_features, system_state) -> ApplicabilityResult: ...
    def build_candidate(self, original_sql, context) -> CandidatePlan: ...
    def validate(self, original_sql, candidate, adapter) -> ValidationResult: ...
```

## 8.2 策略进入可用集合的门槛

每个非 Native 策略必须完成：

1. **入口证明：** 明确对应 SQL 改写/数据库参数/外部可控执行机制；附实际 API 或 SQL 文档及测试记录。
2. **正确性证明：** 使用同一固定数据集对比 Native 和优化查询结果；考虑时间边界、NULL、重复时间戳、浮点误差、排序、空结果等。
3. **生效证明：** 通过计划结构、扫描统计、执行参数或其他可观测证据，确认策略不是只改了 SQL 文本。
4. **收益证明：** 在受控 Benchmark 下重复执行，记录均值、分位数、方差和端到端开销。
5. **回退证明：** 失败或不适用时能回退 Native，且不会产生重复副作用。

如果策略只改变格式/空白/谓词顺序，而 IoTDB 最终计划不变且无可测收益，不要将其当作有效优化策略。

## 8.3 不能在文档里预设 S1/S2 的真实含义

先在 `docs/DB_CAPABILITY_AUDIT.md` 中记录实际发现，然后将已验证策略写入 `docs/STRATEGY_CATALOG.md` 和 `configs/strategies.yaml`。策略 ID 一旦用于经验记录，不得随意复用到不同实现；新实现要提升策略版本或创建新 ID。

---

# 9. 监控、计时与实验可信度

## 9.1 监控来源

- `client_host`：Python 进程所在主机，适合定位客户端开销。
- `db_host`：IoTDB 服务所在机器，需在 DB 主机运行采集代理、系统 exporter 或本机采集脚本。
- `db_metrics`：IoTDB 自身暴露的查询/算子/系统指标，具体以实际版本能力为准。

如果 IoTDB 在本机，仍要记录 source_type；如果远程部署，不能将本机 psutil 的 CPU 值当作 DB CPU。

## 9.2 查询计时

对每次查询至少分别记录：

- `execute_call_ms`：客户端提交 SQL 到获得结果句柄/首个响应所用时间；
- `result_fetch_ms`：读取完整结果集时间；
- `client_wall_ms`：从发起查询到所有结果读取并关闭的总时间；
- `decision_overhead_ms`：解析、状态读取、检索、决策、验证所用时间；
- `end_to_end_ms`：从 API/CLI 接收请求至返回结果的总时间。

数据库若返回惰性结果集，必须完整迭代才能得到一致的客户端观测时间。另设 `fetch_mode`（FULL/STREAMING/LIMITED），同一组 Benchmark 必须保持一致。

## 9.3 缓存和顺序控制

实验必须区分：

- 冷缓存实验（只有在确实能定义/控制冷启动边界时才能称“冷缓存”；无法清除操作系统/存储缓存时要明确说明）；
- 热缓存实验（固定预热过程后测试）。

Native 与优化策略按随机顺序或 ABBA 交替执行；记录执行顺序、预热轮次、数据集版本、数据库重启/缓存条件、并发负载和策略版本。不要将不同缓存条件的运行混算。单实例缓存复用要作为实验控制变量，而非仅靠重启声称清空所有缓存。

## 9.4 EXPLAIN 和 EXPLAIN ANALYZE

- `EXPLAIN` 用于计划观测，按实际版本和数据模型构造语句。
- `EXPLAIN ANALYZE` 会执行查询，应该在诊断或专门 Benchmark 中使用，不要默认在正式请求前后重复执行。
- 计划 hash 应通过稳定字段计算；不要直接对带有随机 ID、时间戳或地址的原始文本做 hash。
- EXPLAIN 不可用时，系统仍应能够记录外部 wall-clock 和系统监控，但 plan verification 标记为 `UNAVAILABLE`，不能伪造计划信息。

---

# 10. 学习反馈设计

## 10.1 分阶段实现，不要一开始直接上复杂算法

**L0：可靠记录。** 保存输入上下文、决策、真实执行、环境、版本和质量标签。

**L1：结构化策略统计。** 在相似查询模板和负载区间内，统计每个策略的样本量、延迟均值/中位数、方差、成功率和资源消耗。

**L2：Contextual Bandit。** 当至少两个可执行策略、足够有效样本和受控动作探索具备后，再实现 LinUCB 或同等级算法。

不允许把 RAG 检索或 LLM 总结当成 L2。

## 10.2 奖励与有效样本

第一版主目标建议聚焦**查询延迟**，资源使用先作为硬约束/辅助指标。不要一开始把 CPU、内存、IO、吞吐等全部随意加权，避免奖励定义不可解释。

建议延迟奖励采用单调变换，例如：

`reward = -log1p(client_wall_ms / 1000.0)`

注意：

- 普通在线请求不需要为了学习而双执行。只有当所选策略版本已经通过独立回归测试（`strategy_certification_status=PASSED`）、本次执行成功、样本质量合格且上下文可比时，才可用于正常策略奖励更新；这表示策略级语义认证通过，不代表本次请求做过差分比对。
- 若某次受控差分测试发现结果不一致（`runtime_result_check=MISMATCHED`），该样本必须判为 INVALID，并禁用/隔离对应策略版本。
- `NOT_CHECKED` 是普通在线请求的正常状态，不应误标成 `MATCHED`；但未通过策略级认证的策略根本不允许进入可执行 Registry。
- 资源超过安全阈值时可判定策略不满足约束；具体惩罚规则写入配置并保持版本化。
- 查询返回数据量差异、fetch_mode 不一致、不同缓存条件或不同负载实验不能直接混为一个可比样本。
- Benchmark 可使用配对对照计算相对收益；在线运行未实际执行的其他策略没有 counterfactual 标签，不能凭空推断其耗时。

## 10.3 探索问题

Bandit 只观察被选策略的反馈。若系统永远只选当前估计最优策略，就无法知道其他策略是否更好。

所以需要两个阶段：

1. **离线/受控 Benchmark 探索：** 对相同查询模板和相似状态，按随机或平衡顺序执行各个已验证策略，收集可比样本。
2. **在线自适应：** 默认保守；仅在安全条件满足时允许小比例探索。探索比例、CPU/内存阈值、策略白名单均可配置。若不允许在线探索，则模型只利用离线实验训练结果，不得声称在线持续探索。

## 10.4 LinUCB 的实现要求

只有 L1 运行正确、样本足够后才启用。实现前先写清楚上下文向量：查询类别编码、时间范围、设备/序列数量、过滤/聚合特征、当前 DB 主机 CPU/内存/IO 等；缺失值要有统一处理方式；数值特征需缩放；特征版本要记录。

每个策略 arm 维护自己的 `A_a`、`b_a` 参数，按选中策略的真实奖励更新。初始正则化参数、探索系数、更新门槛写入 `learning.yaml`。模型更新要原子保存、版本化，并保留上一稳定版本。

必须对照：固定策略、基于统计的策略选择、LinUCB。使用未参与更新的 workload/时间段做评估，报告学习曲线和 regret/累计收益（如果定义可靠）。

## 10.5 非平稳负载

数据库负载会随时间变化。至少记录状态时间戳和模型更新时间；可选实现滑动窗口或衰减更新，但必须经过对照测试。不要让过期模型无条件控制所有查询；可设置模型失效/回退策略。

---

# 11. Orchestrator 与容错要求

`QueryController` 负责单次查询生命周期，不允许 Agent 直接跨层访问数据库或改写数据库连接配置。

必须实现：

- 工作流状态机；
- query/decision/execution ID 贯穿日志；
- 超时配置；
- 资源快照过期检测；
- Parser 不支持时 Native 安全路径；
- 策略不可用时 S0 Native；
- 连接失败时清晰报错；
- 学习存储失败不影响已完成查询；
- 模型更新失败时使用上一稳定版本；
- fallback 前确认第一次执行是否已结束，避免超时后重复占用资源；
- 查询连接和结果集始终正确关闭。

## 11.1 只读安全边界

第一版仅支持单条只读 SELECT。DDL、DML、权限和系统管理语句直接拒绝自动优化。使用只读账号连接（如果数据库配置支持）；对 AST 做语句类型检查；不能只用字符串包含/正则来判断 SQL 安全。

LLM 不得持有数据库凭证，不得直接调用数据库执行工具。LLM 只能输出结构化的策略建议，由程序验证后执行。

## 11.2 结果正确性验证

正式查询默认不做双执行。使用独立测试集对 Native 与优化 SQL 做差分验证：列名/列类型、行数、排序语义、NULL、时间戳、数值容差等。对于没有明确排序的查询，不应简单用行顺序判定差异；应按语义定义比较方法。

每种策略提供自己的正确性测试集和边界案例。只有回归测试通过后策略才可启用。

---

# 12. 分阶段开发路线与阶段门槛

严格按以下顺序执行。每个阶段结束后生成一份阶段报告，并更新 `docs/IMPLEMENTATION_STATUS.md`。

## 阶段 0：仓库、环境与 IoTDB 能力审计（最高优先级）

**目标：** 先确定事实，不写虚构接口。

### 任务

1. 检查工作区、Python 版本、操作系统、依赖管理工具和现有代码。
2. 读取 IoTDB 服务端版本、部署模式、树/表模型、客户端版本和连接方式。
3. 测试最小连接、单条 SELECT、结果集完整读取、关闭连接。
4. 测试当前模型/版本支持的 EXPLAIN；若有 JSON 输出，保存原始响应 fixture；若无，保存文本 fixture。
5. 测试 EXPLAIN ANALYZE 是否可用及其是否会执行查询；只在专门测试库上验证。
6. 收集 30～50 条真实 workload SQL，分类为支持/不支持，并记录语法样例。
7. 审计可能优化动作：SQL rewrite、数据库参数、查询执行选项、连接级配置、外部策略。每条都写明入口、限制、正确性测试、生效观测方式。
8. 给出“已验证可实施策略”清单。Native 作为 S0；至少找到一个有效非 Native 策略，否则标记研究阻塞并提出可验证的替代研究范围。

### 交付物

- `docs/ENVIRONMENT_REPORT.md`
- `docs/DB_CAPABILITY_AUDIT.md`
- `docs/STRATEGY_CATALOG.md`
- `tests/fixtures/sql/` 和 `tests/fixtures/plans/`
- `docs/IMPLEMENTATION_STATUS.md`

### 通过条件

- 确认 server/client 兼容和数据库模型。
- 能连接并完整读取查询结果。
- 有可复现的 SQL/EXPLAIN 样本。
- 每条策略状态标为 `VERIFIED`、`NOT_VERIFIED` 或 `UNSUPPORTED`。
- 无策略时明确阻塞，不能虚构。

## 阶段 1：基础工程、数据库适配器、存储和日志

**目标：** 不使用 LLM 和 Agent，也能稳定连库、执行查询、计时和保存记录。

### 任务

1. 按目标目录创建 Python package。
2. 实现配置加载和 `.env` 密钥注入。
3. 实现 `TSDBAdapter` 抽象类和实际 IoTDB adapter。
4. 实现版本/模型能力探测。
5. 实现只读查询执行和完整结果集消费。
6. 区分 execute_call_ms、result_fetch_ms、client_wall_ms。
7. 实现 SQLite schema、repositories、事务和迁移/初始化逻辑。
8. 实现结构化日志和 query_id。
9. 实现 CLI 的 `doctor`、`query` 两个基本命令。

### 交付物

- `src/tsdb_optimizer/adapters/`
- `src/tsdb_optimizer/storage/`
- `src/tsdb_optimizer/schemas/`
- `tests/integration/test_iotdb_connection.py`
- `docs/ENVIRONMENT_REPORT.md` 更新

### 通过条件

- `doctor` 能报告 DB 连接、版本、模型和支持能力。
- 查询完整消费结果并记录耗时。
- 重复执行不会泄漏 session/result set。
- SQLite 中有对应 query/execution 记录。
- 单元测试和集成测试通过。

## 阶段 2：Agent 1 查询解析

### 任务

1. 实现 Parser Adapter。
2. 用阶段 0 fixture 验证 parser；对 IoTDB 特殊语法做适配或标记 unsupported。
3. 实现 query hash/template。
4. 提取时间、设备/序列、过滤、聚合、排序、limit 等特征。
5. 明确每个特征的数据来源和缺失情况。
6. 实现查询类型多标签分类。
7. 将结果以 Pydantic schema 输出。

### 交付物

- Agent 1 代码；
- 30～50 条 SQL fixture 测试；
- `docs/QUERY_FEATURES.md` 特征字典。

### 通过条件

- 支持 SQL 特征测试通过；
- unsupported SQL 不会误入自动改写；
- 缺失估计值为 null，而非模型猜测；
- 输出字段可序列化为 JSON。

## 阶段 3：Agent 3 性能监控

### 任务

1. 实现客户端主机资源采集。
2. 若 DB 远程部署，增加 DB 主机指标代理/Exporter 接入方案；不能用客户端值冒充 DB 值。
3. 实现周期采样、时间戳、来源、有效期。
4. 实现查询级计时和结果拉取计时。
5. 根据实际版本实现 EXPLAIN parser；不同模型分别适配。
6. 实现计划结构特征和稳定 hash。
7. 将监控结果关联至 query_id/decision_id/execution_id。

### 交付物

- Agent 3 代码；
- 监控 fixture；
- `docs/METRICS_DICTIONARY.md` 指标字典。

### 通过条件

- DB 与 client 指标不混淆；
- 结果集消费计时正确；
- 能识别缺失/过期状态；
- EXPLAIN 不支持时标记 unavailable，不伪造数据。

## 阶段 4：策略库、Validator、Executor 与 Agent 2

### 任务

1. 实现 S0 Native strategy。
2. 按策略审计结果实现非 Native 策略，不得自行发明数据库接口。
3. 实现 Strategy Registry、Applicability、Candidate Generator。
4. 实现 SQL 只读安全检查、候选 SQL 语法检查和策略版本兼容检查。
5. 实现计划/执行配置验证。
6. 实现规则决策和 baseline/rule_based/adaptive 模式接口。
7. 实现历史统计接入接口；没有经验时安全回退。
8. 实现 Executor 和 fallback；保证 timeout 后不盲目重复执行。
9. 把每次决策记录入库。

### 交付物

- Agent 2 代码；
- 策略 Registry；
- Validator 和 Executor 测试；
- `docs/STRATEGY_CATALOG.md` 更新。

### 通过条件

- 策略只能从 registry 选择；
- Native fallback 可用；
- 非 Native 策略正确性测试已过；
- 对不同输入能输出可解释策略决策；
- 无 LLM 模式下全流程可运行。

## 阶段 5：Agent 4 经验库与基础学习

### 任务

1. 设计 query/decision/execution/experience/model version 的关系。
2. 建立经验记录与检索。
3. 过滤无效样本和低可信样本。
4. 实现按 query template、特征和负载区间的策略统计。
5. 实现奖励函数（延迟主目标，资源为约束或明确定义的辅助惩罚）。
6. 实现模型版本管理和原子保存。
7. 加入最小样本数门槛、策略变更门槛和回退机制。
8. 先完成 L1 统计策略，不要未验证就默认开启 LinUCB。

### 交付物

- Agent 4 L1 代码；
- 经验库表结构；
- `docs/LEARNING_DESIGN.md`；
- 可重放的模拟学习测试。

### 通过条件

- 有效经验可以被查询并影响后续策略评分；
- 无效/错误样本不污染正常奖励；
- 数据库/策略版本隔离正确；
- 模型更新失败不影响查询。

## 阶段 6：Orchestrator 四 Agent 闭环

### 任务

1. 实现查询工作流状态机。
2. 按 query_id 串起 Agent 1、2、3、4。
3. 在决策前获取新鲜系统快照和历史经验。
4. 验证决策、执行查询、监控结果、异步或批量写入学习记录。
5. 实现异常隔离、日志和可观测状态。
6. CLI 支持 `baseline`、`rule_based`、`adaptive`。
7. 完成端到端集成测试。

### 交付物

- Orchestrator；
- CLI 主入口；
- 端到端 trace；
- `docs/WORKFLOW.md`。

### 通过条件

一条 SQL 可以完整走过四 Agent；所有结果以 ID 关联；任一非关键学习错误不会丢失已经成功的查询结果；Native 路径始终可运行。

## 阶段 7：Bandit、LLM 辅助和系统优化

### 任务

1. 当 L1 可靠且样本足够时实现 LinUCB（或先写算法对照后选择）。
2. 明确上下文编码、标准化、缺失值处理、奖励和探索参数。
3. 在 Benchmark 中做安全、随机、平衡的策略探索。
4. 实现训练/更新/测试时间隔离，避免同一批样本既训练又评估。
5. 可选接入 LLM：Agent 2 仅从候选策略中推荐；Agent 3/4 只做复杂诊断和经验解释。
6. LLM 输出必须 JSON schema 校验，失败时回退非 LLM 路径。
7. 比较 LLM on/off 的决策质量与额外延迟。

### 交付物

- Bandit 实现与算法测试；
- LLM Client（可选）；
- LLM prompt 文件；
- `docs/LEARNING_DESIGN.md` 更新。

### 通过条件

- 有充分样本时模型能够随上下文改变策略偏好；
- 未知/不安全策略不可被选中；
- 模型参数可复现、可版本化、可回退；
- LLM 关闭时系统仍完整运行；
- LLM 开启后必须有收益/开销对照。

## 阶段 8：Benchmark、消融实验与结果分析

### 任务

1. 准备固定 workload 和数据集版本。
2. 对每组策略固定预热/缓存条件、fetch mode、并发负载和重复次数。
3. 随机或 ABBA 排列 Native/优化策略执行顺序。
4. 比较 E0 Native、E1 Fixed Strategy、E2 Rule-based、E3 Multi-agent no learning、E4 Adaptive learning。
5. 做去除系统状态特征、去除历史学习的消融。
6. 统计平均、P50、P95、P99、吞吐、CPU、内存、策略选择分布和端到端延迟。
7. 输出 CSV、JSON 原始结果和图表；不能只保存手工汇总表。

### 通过条件

- 每次实验可通过配置与随机种子重放；
- 指标口径统一；
- 结果包含样本量、离散程度和实验限制；
- 不能把单次最优结果当作结论。

---

# 13. Benchmark 实验定义

## 13.1 对比组

| 编号 | 配置 | 目的 |
|---|---|---|
| E0 | IoTDB Native | 原始数据库优化器基线 |
| E1 | 固定非 Native 策略 | 评估策略本身的效果 |
| E2 | 查询特征 + 规则决策 | 评估规则自适应 |
| E3 | 四 Agent，不启用在线学习 | 评估完整协作但不学习 |
| E4 | 四 Agent + 学习反馈 | 评估持续学习的增益 |
| A1 | E4 去掉系统负载特征 | 负载感知消融 |
| A2 | E4 去掉历史经验/学习 | 学习模块消融 |

E0 的含义是将原始 SQL 交给 IoTDB 自带优化器执行，不是关闭 IoTDB 内部优化器。

## 13.2 指标

- Query/server execution latency（数据库提供时）；
- `client_wall_ms`；
- `decision_overhead_ms`；
- `end_to_end_ms`；
- P50/P95/P99；
- throughput；
- DB host CPU/memory/disk I/O；
- result correctness；
- strategy applicability / activation rate；
- 策略样本数、模型更新次数、策略切换情况。

策略“命中率”要定义清楚。建议分成 `selected_rate`、`applicable_rate`、`execution_success_rate`、`plan_effective_rate`，不要用一个含糊的 hit rate。

## 13.3 实验执行原则

- 同一比较组固定 IoTDB 版本、数据集、查询参数、返回行数和 fetch mode。
- 冷/热缓存分开报告；无法清除某层缓存则明确为“受控预热/未知缓存状态”，不要声称冷缓存。
- 随机化或 ABBA 执行顺序，降低同实例缓存与时间漂移偏差。
- 系统负载应有单独的低/中/高档定义，并保存真实采样。
- 至少多次重复，依据方差决定是否增加重复次数。
- 报告数据库执行耗时和端到端耗时，不能只报告其中一个。
- 在线查询不应为了对照而无条件重复执行其他策略；反事实比较通过专门受控 Benchmark 取得。

---

# 14. 代码 AI 协同开发规范

## 14.1 每次任务的标准流程

要求 AI 每次按以下顺序工作：

1. 阅读 `IMPLEMENTATION_GUIDE.md`、`docs/IMPLEMENTATION_STATUS.md` 和相关代码。
2. 总结当前阶段目标、现有实现和缺失项。
3. 列出准备修改的文件和设计理由。
4. 明确可能影响的接口和兼容风险。
5. 小步实现，不重写无关模块。
6. 运行针对性测试，再运行全量测试。
7. 检查 lint/type check（项目已配置时）。
8. 更新文档和阶段状态。
9. 输出真实测试命令、测试结果、尚未解决的问题和下一步建议。

## 14.2 对代码 AI 的硬性约束

- 不得以伪代码或 TODO 代替阶段要求的真实实现，除非明确声明该项被阶段门槛阻塞。
- 不得伪造测试通过；必须展示实际命令和结果摘要。
- 不得编造 IoTDB API、SQL 语法、EXPLAIN JSON 字段或配置项。
- 不得擅自更改已确定的数据协议；确需修改时先说明迁移影响。
- 不得删除已有测试以使测试通过。
- 不得在异常时吞掉错误并返回“成功”。
- 不得把 LLM 的自由文本直接送入数据库执行。
- 不得将客户端监控数据标记为数据库服务器监控数据。
- 不得把没有对照的单次性能提升写成已验证优化收益。
- 依赖变更必须说明原因，更新 lock 文件，并进行导入/集成测试。
- 每次只完成当前阶段，不要顺手实施后续复杂功能。

## 14.3 如何降低 AI 编码返工

- 先提供环境信息和真实 SQL fixture，再让 AI 开始适配。
- 使用本手册定义的 schema 和模块边界，避免每个 Agent 自建格式。
- 让 AI 输出最小可运行 patch，而不是整仓库重写。
- 对每个模块先写测试再实现。
- 一个阶段一个 Git commit；大任务拆成 2～5 个小任务。
- 如果 AI 连续两次修复失败，要求它先定位最小复现并解释根因，不要继续盲目改代码。
- 不要让多个代码 AI 同时修改同一组核心文件；若并行开发，按模块分支并由主控 AI 集成。

---

# 15. 可直接复制的 AI 提示词

下面的提示词按顺序使用。方括号内容应替换为你的实际路径和环境信息。第一条用于建立全局约束，之后每阶段使用对应提示词。

## Prompt 0：总控提示词（每个新代码 AI 会话先发）

```text
你现在是本项目的资深 Python/数据库系统工程师，负责实现 Multi-Agent TSDB Optimizer。

请先完整阅读仓库根目录的 IMPLEMENTATION_GUIDE.md。它是当前项目的工程规范和阶段验收依据。再阅读 docs/IMPLEMENTATION_STATUS.md（如果存在）以及当前代码。

项目目标：实现四个逻辑 Agent：查询解析 Agent、优化决策 Agent、性能监控 Agent、学习反馈 Agent；由 Orchestrator 调度；通过 IoTDB Adapter 访问数据库；通过策略注册表执行经过能力审计的查询优化策略；通过结构化执行经验和在线学习改善后续策略选择。

必须遵守：
1. 不修改 IoTDB 源码。
2. 不允许虚构 IoTDB API、SQL 语法、EXPLAIN 字段、配置项或实验结果。未知内容必须标记 UNKNOWN，并设计实际探测/测试。
3. 不允许让 LLM 任意生成并直接执行 SQL/物理计划。所有策略必须在 Registry 中，经过 Validator。
4. 不要求四个模块都调用 LLM。无 LLM 时系统也必须可完整运行。
5. 原始 Native 查询始终是 S0 安全基线和 fallback。
6. SQL 解析仅对经过 fixture 验证的语法子集负责；未知语法禁止自动改写。
7. 客户端机器指标和 IoTDB 服务端指标必须分开记录。
8. 正式查询不默认双执行；EXPLAIN ANALYZE 是真实执行，不能当成无副作用 EXPLAIN。
9. 学习模块只能使用真实、质量合格的样本更新；不得把一次成功或 RAG 摘要视为模型学习。
10. 不得删除测试、吞掉异常或伪造测试通过。

协作方式：
- 一次只执行我指定的当前阶段/任务，不要提前实现整个系统。
- 开始修改前先检查仓库和环境，列出准备修改文件、设计方案、风险和测试计划。
- 小步实现，保持模块边界和 Pydantic 数据协议。
- 实现后运行相关测试，并报告实际命令和结果。
- 更新 docs/IMPLEMENTATION_STATUS.md、必要的阶段文档和 README。
- 最后报告：完成内容、改动文件、测试结果、已知限制、未完成事项、下一步。

若手册与真实 IoTDB 版本/环境冲突，先说明冲突并给出证据，不要自行猜测覆盖。请先回复你理解的系统边界和当前阶段，不要马上生成全部代码。
```

## Prompt 1：阶段 0——环境与 IoTDB 能力审计

```text
现在执行 IMPLEMENTATION_GUIDE.md 的“阶段 0：仓库、环境与 IoTDB 能力审计”。本阶段以调查和最小探测为主，不要提前编写四个 Agent。

先检查：
- 当前工作区和已有代码；
- Python 版本、操作系统、虚拟环境、关键依赖；
- IoTDB server 版本、部署方式、树模型/表模型；
- 当前客户端包和版本；
- 连接地址、认证方式（不要把密码写入文档或日志）。

然后在用户允许的测试库/只读环境中验证：
1. 最小连接和版本查询；
2. 选择一条用户提供的 SQL 执行，并完整读取结果集；
3. EXPLAIN 是否支持、实际语法和结果样例；
4. EXPLAIN ANALYZE 是否支持，明确它会执行目标查询；
5. 当前 Parser 对真实 SQL 样例的兼容性；
6. 可能优化策略的真实执行入口、适用条件、语义测试方式和生效验证方法。

请建立或更新：
- docs/ENVIRONMENT_REPORT.md
- docs/DB_CAPABILITY_AUDIT.md
- docs/STRATEGY_CATALOG.md
- docs/IMPLEMENTATION_STATUS.md
- tests/fixtures/sql/ 和 tests/fixtures/plans/（保存去敏后的真实样例）

每条优化策略必须标记 VERIFIED / NOT_VERIFIED / UNSUPPORTED。禁止根据理论推断声称 IoTDB 有某个参数/Hint/接口。若未找到至少一个非 Native 策略，明确写出阻塞原因和下一步可验证方向。

结束时给我：环境事实、能力审计表、可用策略清单、测试证据、风险、阶段是否通过。未通过时不要进入阶段 1。
```

## Prompt 2：阶段 1——基础工程和 IoTDB Adapter

```text
执行 IMPLEMENTATION_GUIDE.md 阶段 1。只实现基础工程、配置、日志、IoTDB Adapter、SQLite 存储和最小 CLI，不实现四个 Agent。

先依据阶段 0 的 ENVIRONMENT_REPORT 和 DB_CAPABILITY_AUDIT 选择正确的 IoTDB client/API。必须严格匹配服务端版本和树/表模型。若环境信息不完整，先报告缺项，不猜测。

实现要求：
1. Python package 结构和 pyproject/lock；
2. 安全配置加载，密码/API Key 从环境变量读取；
3. TSDBAdapter 抽象接口和当前 IoTDB adapter；
4. doctor 命令：检查连接、server version、database model、能力探测；
5. query 命令：只接受单条只读 SELECT，执行并完整消费结果集；
6. 分别记录 execute_call_ms、result_fetch_ms、client_wall_ms；
7. 正确关闭 Session/ResultSet；
8. SQLite 初始化、事务、query/execution 基础表；
9. 结构化日志，每条记录带 query_id；
10. pytest 单元和集成测试。

不要把 EXPLAIN JSON 字段写死，先按阶段 0 样例解析；如暂时未实现，能力返回 unavailable。

完成后运行测试，更新 IMPLEMENTATION_STATUS.md 和 README，输出可复制的安装、配置、doctor、query 命令。不要进入阶段 2。
```

## Prompt 3：阶段 2——Agent 1 查询解析

```text
执行 IMPLEMENTATION_GUIDE.md 阶段 2，只实现 Agent 1 和查询特征 schema。

先阅读阶段 0 保存的 SQL fixtures。选择/配置 SQL parser 时必须说明 IoTDB 语法兼容范围。SQLGlot 只能作为候选解析器；不要宣称它完整支持 IoTDB。对不支持的语法返回 UNSUPPORTED，不允许自动改写。

实现：
1. SQLParserAdapter；
2. ParseResult 和 QueryFeatures Pydantic schema；
3. query_hash 和 normalized_template；
4. 时间条件、过滤条件、设备/序列、聚合类型、GROUP BY、ORDER BY、LIMIT 等特征抽取；
5. 每个特征记录 source 和 missing reason；
6. query type 多标签分类；
7. 对 AST 未知节点 fail-closed；
8. 30-50 条 SQL fixture 测试（若当前样例不足，先列清单并提示补充，不伪造真实 IoTDB SQL）。

不调用 LLM，不使用正则作为安全解析器，不猜 estimated_rows。所有未知值使用 None 并标记缺失。

完成后输出特征字典、支持语法清单、测试通过情况和已知局限；更新 IMPLEMENTATION_STATUS.md。不要进入阶段 3。
```

## Prompt 4：阶段 3——Agent 3 性能监控

```text
执行 IMPLEMENTATION_GUIDE.md 阶段 3，只实现 Agent 3、系统采样和计划解析。

先判断 IoTDB 是本机还是远程。如果远程，明确 DB 主机监控如何获取；不能用客户端 psutil 指标冒充 DB 指标。若当前无法安装 DB 主机代理，先实现 source_type=client_host/unknown 的正确数据结构，并将 DB host metrics 标为 unavailable。

实现：
1. 周期系统采样，时间戳、主机、source_type、有效期；
2. CPU、内存、磁盘 I/O（注意区分磁盘容量使用率和 I/O 速率）；
3. 查询执行耗时、结果完整拉取耗时、rows_returned、错误和 timeout；
4. 按阶段 0 的真实 EXPLAIN 输出实现 parser；树/表模型分别适配；
5. 稳定 plan signature/hash，剔除不稳定字段；
6. 查询与 snapshot 的 query_id/decision_id/execution_id 关联；
7. 指标字典 docs/METRICS_DICTIONARY.md；
8. 单元测试与至少一条 IoTDB 集成测试。

EXPLAIN ANALYZE 会实际执行查询，禁止在普通请求路径自动调用。缺少字段要标记 unavailable，不要填假值。

完成后运行测试并更新状态，不进入阶段 4。
```

## Prompt 5：阶段 4——策略库、验证器、执行器和 Agent 2

```text
执行 IMPLEMENTATION_GUIDE.md 阶段 4。重点是“有限、真实、经过验证的策略集合”，不是让 LLM 自由生成 SQL。

先完整阅读 docs/DB_CAPABILITY_AUDIT.md 和 docs/STRATEGY_CATALOG.md。只实现其中状态为 VERIFIED 且有真实入口的策略。S0_NATIVE 必须实现；未验证策略不允许启用。

实现：
1. StrategySpec、StrategyRegistry；
2. S0_NATIVE；
3. 已审计的非 Native 策略插件；
4. is_applicable、candidate generation；
5. 只读 SQL Validator、策略兼容检查、候选方案验证；
6. Decision Engine，支持 baseline/rule_based/adaptive 接口；
7. 成本估计先采用可靠统计或规则，明确 unknown；
8. Executor：只执行一个已选候选，记录完整耗时；
9. fallback：仅在确认第一次请求结束且重试安全时回退 Native；
10. 决策和执行数据持久化；
11. 对每种策略测试语义、计划/动作生效和失败回退。

若阶段 0 没有有效非 Native 策略，不得自行编造策略。此时只完成 Native、安全验证框架和策略插件接口，并将项目标记 BLOCKED: NO_VERIFIED_NON_NATIVE_STRATEGY。

完成后给出真实策略执行证据、正确性测试、回退测试和测试报告。不要进入阶段 5。
```

## Prompt 6：阶段 5——Agent 4 经验库与 L1 学习

```text
执行 IMPLEMENTATION_GUIDE.md 阶段 5。先实现可靠经验记录和结构化统计，不要一开始就直接实现复杂 Bandit，也不要使用向量数据库替代结构化数据。

实现：
1. experience schema 和 SQLite repository；
2. 查询、决策、执行、策略版本、模型版本关联；
3. 样本质量过滤 VALID/LOW_CONFIDENCE/INVALID；
4. 基于 query template、query features、数据库版本、策略版本和负载上下文的经验检索；
5. 每策略样本量、平均/中位延迟、方差、成功率统计；
6. reward 计算（延迟主目标，单位和公式固定）；
7. min_samples 门槛；
8. model version 和原子保存；
9. 学习写库失败不影响查询结果；
10. 模拟数据测试和真实 execution record 集成测试。

重要：普通在线请求默认不双执行，runtime_result_check=NOT_CHECKED 是正常状态。只有策略版本已通过独立语义回归认证、当前执行成功、样本上下文可比且质量合格时，才允许更新性能奖励；如果差分测试发现 MISMATCHED，则该策略版本必须隔离。单次运行更快不等于策略有效；未执行的策略没有真实 reward，不得构造 counterfactual 数据。

先实现 L1 统计决策支持，输出不同 query/load context 下的策略统计。完成后用人工构造的模拟数据验证统计更新，再用真实 Benchmark 数据验证。更新 LEARNING_DESIGN.md 和状态文档，不进入阶段 6。
```

## Prompt 7：阶段 6——Orchestrator 四 Agent 闭环

```text
执行 IMPLEMENTATION_GUIDE.md 阶段 6。把已验收的 Agent 1-4、Adapter、Executor、Store 通过 Orchestrator 串起来。

实现：
1. 查询生命周期状态机；
2. 统一 query_id、decision_id、execution_id；
3. Agent 1 parse -> snapshot -> Agent 4 retrieve -> Agent 2 decide -> Validator -> Executor -> Agent 3 monitor -> Agent 4 learn；
4. baseline/rule_based/adaptive 模式；
5. parser unsupported 时走安全 Native 路径或按安全策略拒绝；
6. 资源状态过期时降级决策；
7. Agent 4 持久化/模型更新错误不得影响已经成功的查询返回；
8. 超时/异常/fallback 的状态与日志；
9. CLI 单查询和 workload 文件入口；
10. 端到端测试，校验完整 trace。

不得新增与阶段目标无关的 Web UI、微服务或 Agent 框架。确保无 LLM 环境完整运行。

完成后给出一条真实测试查询的完整 trace（隐去密码和敏感数据），确认数据库执行只发生预期次数，并更新状态。不要进入阶段 7。
```

## Prompt 8：阶段 7——LinUCB 与可选 LLM

```text
执行 IMPLEMENTATION_GUIDE.md 阶段 7。先检查 L1 统计模块、真实样本量、策略数量和策略探索记录，判断是否满足启用 Bandit 的条件。若样本不足，先实现接口与离线模拟测试，但不要将模型标记为可用于真实自适应决策。

LinUCB 实现必须明确：
- 上下文向量各维度及版本；
- 缺失值处理和数值缩放；
- 每 arm 的 A/b 参数；
- reward 公式；
- alpha、regularization、min_samples；
- 探索策略及安全约束；
- 参数原子保存、model version、旧版本回滚。

只用被执行策略的真实反馈更新对应 arm；未被执行策略不能产生虚假标签。通过固定随机种子和合成数据单元测试算法，再用独立 Benchmark 样本评估。

LLM 是可选增强：
- 默认关闭；
- Agent 2 只能从 registry 候选中推荐 strategy_id；
- LLM 不输出可直接执行的任意 SQL；
- JSON schema 校验失败/超时/不可用时回退规则决策；
- 对比 LLM 开关的决策质量和额外时延。

完成后给出算法测试、样本门槛判断、LLM 开关对照和已知风险。更新状态。
```

## Prompt 9：阶段 8——Benchmark 与消融实验

```text
执行 IMPLEMENTATION_GUIDE.md 阶段 8。重点是实验可复现和比较公平，不要改动优化算法来“迎合”某次结果。

实现：
1. workload 配置文件，固定 SQL、参数、数据集版本、返回行数和 fetch mode；
2. 可配置 warmup、重复次数、执行顺序随机种子、并发级别和系统负载档位；
3. E0 Native、E1 fixed strategy、E2 rule-based、E3 multi-agent no learning、E4 adaptive learning；
4. A1 去除 system state、A2 去除 history/learning；
5. 记录原始每次运行数据，不只保存聚合结果；
6. 计算平均、标准差/置信区间（方法明确）、P50/P95/P99、throughput、CPU、memory、decision overhead、end-to-end latency、correctness；
7. 随机或 ABBA 执行顺序；冷/热缓存分开报告；
8. 生成 CSV/JSON 和图表；
9. 输出实验报告模板，标明无法控制的因素。

不要在同一查询中无条件双执行所有策略来构造在线反馈。策略效果对照通过专门的受控 Benchmark 获取。

完成后提供一条命令可复现实验，并检查同一配置重跑能产生结构一致的结果文件。更新 EXPERIMENT_PROTOCOL.md。
```

## Prompt 10：通用 Debug / 修复提示词

```text
请只定位并修复当前问题，不要重写无关模块。

现象/报错：
[Process ForkProcess-1:
Traceback (most recent call last):
  File "/opt/anaconda3/envs/MANTO/lib/python3.12/multiprocessing/process.py", line 314, in _bootstrap
    self.run()
  File "/opt/anaconda3/envs/MANTO/lib/python3.12/multiprocessing/process.py", line 108, in run
    self._target(*self._args, **self._kwargs)
  File "/opt/program-code/MANTO/src/tsdb_optimizer/experiments/supervisor.py", line 27, in _child
    target(args, _signal=send)
  File "/opt/program-code/MANTO/benchmarks/run_benchmark.py", line 39, in run_live
    execute(config, args.output, environment, signal=progress)
  File "/opt/program-code/MANTO/src/tsdb_optimizer/benchmark/runner.py", line 266, in execute
    future.result()
  File "/opt/anaconda3/envs/MANTO/lib/python3.12/concurrent/futures/_base.py", line 456, in result
    return self.__get_result()
           ^^^^^^^^^^^^^^^^^^^
  File "/opt/anaconda3/envs/MANTO/lib/python3.12/concurrent/futures/_base.py", line 401, in __get_result
    raise self._exception
  File "/opt/anaconda3/envs/MANTO/lib/python3.12/concurrent/futures/thread.py", line 59, in run
    result = self.fn(*self.args, **self.kwargs)
             ^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^
  File "/opt/program-code/MANTO/src/tsdb_optimizer/benchmark/runner.py", line 239, in worker
    stress.check()
  File "/opt/program-code/MANTO/src/tsdb_optimizer/experiments/cpu_load.py", line 87, in check
    if self.error: raise RuntimeError(self.error)
                   ^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^
RuntimeError: CO_TENANT_SAFETY_STOP]

复现步骤：
[PYTHONPATH=src python benchmarks/run_benchmark.py \
  --config benchmarks/workloads/small_count_multiload_c2.json \
  --output "benchmarks/results/phase8_multiload_c2_$(date +%Y%m%d_%H%M%S_%N)" \
  --immutable-epoch-confirmed --exclusive-window-confirmed \
  --cpu-cotenant-authorized]

相关环境：
[MANTO：Python 3.12.13、IoTDB server/client 版本2.0.6、树模型、操作系统 ubuntu 24.04]

要求：
1. 先解释最可能的根因，并指出哪些判断已由代码/日志证实，哪些仍是假设。
2. 建立最小复现或补充失败测试。
3. 检查是否违反 IMPLEMENTATION_GUIDE.md 的接口和数据协议。
4. 提供最小代码修改，不修改不相关文件。
5. 运行针对性测试和相关回归测试。
6. 不得通过删除测试、吞异常、关闭验证或硬编码假数据解决问题。
7. 最后报告根因、改动、测试命令/结果、遗留风险。
```

## Prompt 11：代码审查/阶段验收提示词

```text
请作为严格的数据库系统代码审查员，依据 IMPLEMENTATION_GUIDE.md 对当前阶段进行验收，不要以“代码看起来完整”作为通过标准。

检查：
- 阶段所有任务是否真实实现；
- 是否存在 TODO/pass/mock 假实现；
- 数据结构字段、单位、时间戳和 source 是否一致；
- 是否有异常被吞掉；
- 是否有未经验证的 IoTDB API 或 SQL；
- 是否将 client metrics 冒充 DB metrics；
- 是否让 LLM 绕过 Registry/Validator；
- 是否对错误样本进行正向学习；
- 是否将 EXPLAIN ANALYZE 用于普通查询路径；
- 是否有重复执行、缓存污染或结果集未消费问题；
- 测试是否真实运行、是否覆盖失败路径。

请逐条输出 PASS / FAIL / BLOCKED，并附文件路径、代码位置和测试证据。对 FAIL 项提出最小修复清单。未达到阶段通过条件时，明确判定 NOT ACCEPTED，不要建议直接进入下一阶段。
```

---

# 16. AI 编码时用户需要提供的项目资料

代码 AI 无法从本手册推断以下环境事实。开始阶段 0 前，请准备并提供：

1. IoTDB 服务端准确版本、部署方式、树模型/表模型。
2. IoTDB 客户端/驱动当前版本和连接方式。
3. 数据库与代码运行机器是否同一台；若不同，说明主机关系和是否能在 DB 主机安装监控代理。
4. 实际可执行的 30～50 条 SQL（去敏），包括查询类型和预期结果。
5. 当前数据集名称、数据规模、设备/序列规模、时间范围。
6. 目前已发现或尝试过的优化动作、是否能控制、执行计划变化情况。
7. 连接参数通过环境变量提供，不要把密码粘贴进 Prompt 或提交仓库。
8. 若已有项目代码，提供仓库目录和当前测试命令。

若暂时没有全部资料，先用 Prompt 1 进行审计；代码 AI 应列出缺失项，而不是停下来空等或自行编造。

---

# 17. 最终交付验收清单

系统只有同时满足以下条件，才算第一版实验系统完成。

## 功能验收

- [ ] 能连接实际 IoTDB 服务，正确识别版本和数据模型。
- [ ] 能解析支持范围内的 SQL，并明确标记 unsupported。
- [ ] 能输出查询特征和来源。
- [ ] 能获取带时间戳的有效系统状态。
- [ ] 有 S0 Native 和至少一个通过验证的非 Native 策略。
- [ ] Agent 2 只能从已注册策略中选择。
- [ ] 所有决策和执行记录可用 ID 追溯。
- [ ] Agent 3 能记录完整查询耗时和运行指标。
- [ ] Agent 4 能保存真实经验、筛选样本和更新策略统计/模型。
- [ ] 后续查询能够使用更新后的经验或模型。
- [ ] 失败、超时和 fallback 有明确状态；学习失败不会影响已完成查询。
- [ ] 无 LLM 时系统完整可运行；LLM 关闭/错误时有安全回退。

## 实验验收

- [ ] 有固定 workload、数据集版本和实验配置。
- [ ] 有 Native、固定策略、规则决策、无学习和自适应学习对比。
- [ ] 有系统状态和学习模块消融。
- [ ] 有冷/热缓存或受控预热说明。
- [ ] 执行顺序随机化或 ABBA。
- [ ] 有多次重复和统计离散程度。
- [ ] 同时报告数据库查询耗时和端到端耗时。
- [ ] 有查询正确性验证。
- [ ] 有原始 CSV/JSON 结果和可重放命令。
- [ ] 不以未经控制的单次结果宣称优化收益。

## 研究结论门槛

只有在至少两个真实有效策略的前提下，系统在动态状态场景中能根据反馈调整选择，并在独立测试 workload 上相比固定策略或 Native 获得可重复收益，才能支持“具备自适应查询优化能力”的结论。

若只完成解析、监控、经验检索，没有发生策略模型/决策行为的可测更新，应准确表述为“经验辅助查询优化原型”，不能写成已实现持续在线学习。

---

# 18. 官方技术参考资料

代码 AI 在实现前应查看与实际 IoTDB 版本匹配的官方文档；若当前版本文档与以下页面不一致，以实际版本官方文档和实测结果为准。

- **[R1] IoTDB 2.0.x 表模型查询性能分析：** EXPLAIN、EXPLAIN ANALYZE、JSON 输出和算子统计。JSON 格式从 V2.0.11 起支持。  
  https://iotdb.apache.org/zh/UserGuide/latest-Table/User-Manual/Query-Performance-Analysis_apache.html
- **[R2] IoTDB Python Native API（表模型）：** TableSession、结果读取和客户端版本兼容提醒。  
  https://iotdb.apache.org/zh/UserGuide/latest-Table/API/Programming-Python-Native-API_apache.html
- **[R3] IoTDB 树模型查询性能分析：** V1.3.2 及以上的 EXPLAIN / EXPLAIN ANALYZE 介绍。  
  https://iotdb.apache.org/UserGuide/latest/User-Manual/Query-Performance-Analysis.html
- **[R4] IoTDB Python Native API（树模型文档入口）：** Session 与查询结果集读取方式。  
  https://iotdb.apache.org/UserGuide/latest/API/Programming-Python-Native-API_apache.html
- **[R5] SQLGlot 官方仓库：** 支持方言列表、自定义方言和解析器限制。IoTDB 不在其官方维护方言列表中，必须实测和适配。  
  https://github.com/tobymao/sqlglot
- **[R6] Pydantic 文档：** 数据模型、字段验证与序列化。  
  https://docs.pydantic.dev/
- **[R7] psutil 文档：** Python 进程所在主机的系统资源采集。远程 DB 主机需要单独部署/接入监控。  
  https://psutil.readthedocs.io/

---

# 19. 现在就开始：推荐执行顺序

1. 创建项目仓库，把本文件放在仓库根目录，命名为 `IMPLEMENTATION_GUIDE.md`。
2. 把真实 IoTDB 版本、模型、连接方式、SQL 样例准备好；密码只通过环境变量提供。
3. 对代码 AI 发送 Prompt 0，再发送 Prompt 1。
4. 审核阶段 0 报告，重点审查“真实可执行的非 Native 策略”是否存在。
5. 阶段 0 通过后，依次发送 Prompt 2～Prompt 9；每阶段验收、Git commit 后再进入下一阶段。
6. 遇到错误使用 Prompt 10；每个阶段结束使用 Prompt 11 做严格验收。

**最高优先级不是先让四个 Agent 运行起来，而是尽快确认策略入口和可观测指标。** 这两项决定了后面的优化决策、学习算法和实验是否有实际意义。只要先把这两个前提查实，后续代码就可以按本手册分模块并行推进，最大限度减少 AI 编码返工。

---

**文档结束**
