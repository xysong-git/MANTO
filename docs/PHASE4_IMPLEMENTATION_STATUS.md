# 实现状态

## 1. 当前阶段

**R1/R2 最小修复（2026-11-02）：** 最近严格复审判定 NOT ACCEPTED。
现已修复客户端失效时丢弃有效 DB 快照的问题：各来源分别隔离，客户端
invalid/stale/future 不会屏蔽有效 DB_HOST/DB_METRICS 的资源阈值。
新鲜度使用 `captured_at <= now < valid_until`，不配置时钟容差；未来快照
置空并标记 `FUTURE_SNAPSHOT`，决策写入 `*_FUTURE_DEGRADED`。
状态为 **PENDING RE-REVIEW（R1/R2 修复已应用）**，本文件不自行判定通过。

本轮实际解释器：`/opt/anaconda3/envs/MANTO/bin/python`（命令中的 python 均指该环境）。

本轮实际测试：默认 598 passed/11 skipped，单元 502 passed，启用真实 IoTDB 的完整套件 609 passed（含 11 个真实集成用例）。

定向回归：`tests/unit/test_decision_engine.py`、`tests/unit/test_monitoring_schema.py`、
`tests/unit/test_resource_policy.py`，90 passed。新增 19 个用例覆盖两种决策模式、
两种 DB 来源、客户端 invalid/stale/future、各来源未来时间隔离及有效期边界。


| 项目 | 值 |
|---|---|
| 阶段 | **阶段 4：Agent 2、策略 Registry/Validator、决策、单候选执行与 fallback** |
| 状态 | **`PENDING RE-REVIEW（前序修复 + R1/R2 修复已应用）`**；两个原始研究阻塞项已解除；F1–F15 的修复和回归测试已完成，本轮又修复了资源阈值、无效 DB 快照和验收文档缺口，等待严格复审 |
| 阻塞标记 | 研究阻塞 **已解除**：`BLOCKED: NO_VERIFIED_NON_NATIVE_STRATEGY` 不再成立；阶段 4 **不能**据此宣布重新验收通过 |
| 日期 | 2026-11-02 |
| 分支 | `main` |
| 已认证策略 | `S0_NATIVE`（基线/回退）和 `S1_REPEAT_CACHE`（非 Native） |
| 默认 Registry | `("S0_NATIVE", "S1_REPEAT_CACHE")`；S1 需要 `cache_epoch` 且 exact SQL 有重复 |
| 外部认证目录 | `configs/strategies.yaml`（schema 1）；默认 Registry 强制加载 |
| 资源策略 | `configs/resource_policy.yaml`（schema 1，`policy_version=2026-11-02.1`）；仅对有效、新鲜且 source 匹配的 CPU/内存/磁盘百分比评估，超阈值强制回退 S0 |
| 执行路径 | `query` 是直接 S0；`optimize` 走 Decision → 单候选 Executor → 保守 fallback |
| 普通路径的 EXPLAIN | 不调用 `EXPLAIN`，从不调用 `EXPLAIN ANALYZE` |
| LLM | 没有 LLM；三种决策模式均可离线运行 |
| 持久化 | SQLite schema **v6**；v1/v2/v3/v4/v5 原位迁移；新增 `execution_reports` |
| Git | 所有新增/修改文件仍未提交；等待用户决定 |

本阶段严格按用户指令只实现阶段 4，**没有进入阶段 5**，没有实现 Agent 4、
学习器或 LLM 生成 SQL。`query/` 仍是唯一 workload 依据；没有为启用策略虚构
查询。

**阻塞解除说明（2026-11-01）：** 用户要求解决“没有已验证非 Native 策略和
非 Native 正确性证据”和“没有生产 Native fallback 证据”两个阻塞项。新增
`S1_REPEAT_CACHE`：进程内、精确 cleaned SQL、固定调用方声明 epoch 的完整结果
缓存。它不是 SQL 改写，不修改 IoTDB 源码，不声称服务端计划变化，也不声称
通用 freshness。五项证明在固定 epoch 契约下完成；证据见
[`STRATEGY_CATALOG.md`](STRATEGY_CATALOG.md) 的 S1 章节。

**严格审查修复说明（2026-11-01）：** 后续严格审查判定阶段 4
`NOT ACCEPTED`，并给出五个阻塞项。现已修复并加入回归测试：

- **F1 缓存元数据污染：** `ColumnMeta` 改为 frozen；`ResultCache.put` 在写入
  边界复制列元数据，调用方持有的原对象不能影响已缓存结果；
- **F2 SQL guard 过宽：** 只有 `query/` workload 已验证的两个 SQLGlot 2.0.6
  parse gap（通配测量路径、interval `GROUP BY`）允许 parse-only 归一化；
  任意其他 `ParseError` fail-closed，归一化后仍必须解析为单条 SELECT-family；
- **F3 cache hit 计时语义：** hit 的 `result_fetch_ms=0.0`，查找耗时单独记录为
  `cache_lookup_ms`（结构化日志和 `CACHE_HIT` warning），不再冒充结果拉取；
- **F4 cache hit 日志 ID：** hit 日志记录本次 hit 的 `execution_id`，并用
  `cache_source_execution_id` 单独指向填充缓存的 miss execution；
- **F5 失败路径测试：** 增加缓存元数据不可变、非验证 ParseError、S1 不可缓存、
  hit 计时/日志、disabled VERIFIED 策略不得认证等回归测试。
- **F6 parser 范围：** non-baseline 候选若执行 exact cleaned SQL（无改写），
  中心的 `candidate_parser_scope` 检查显式返回 `EXACT_SQL_NO_REWRITE` PASSED；
  不再把“Parser 不覆盖”写成误导性 WARNING，也不把 S1 说成 Native passthrough。
  parser-unsupported 和缺少 Agent-1 analysis 两种用例均有回归测试。
- **F7 收益证明：** 证据脚本改为每种 workload 30 次 Native + 30 次 S1 direct，
  q8 service 50 次 + Native-only 50 次；输出 n、mean、median、p50/p95/p99、
  variance、stdev、min/max，以及 `decision_overhead_ms` 和派生的
  `non_client_overhead_ms`。原始 JSON 已落盘；不再引用不可复现的旧中位数。
- **F8 正确性边界：** 新增空结果、NULL、重复时间戳、`-(2**63)`/`2**63-1`
  时间边界、NaN/Inf、行序保持、NaN 感知比较测试，覆盖 `ResultCache` 和
  Adapter hit 路径；S1 不做算术/排序/类型转换，缓存层不引入浮点误差或重排。
- **F9 adaptive occurrence：** `DecisionEngine._select_adaptive()` 对
  `REPEAT_CACHE` 建议强制 `occurrence >= 2`；occurrence 1 的 adaptive 建议被
  拒绝并记录 `REPEAT_CACHE_REQUIRES_REPEAT`，实际选择 S0 Native。新增回归测试
  验证第一次 S0、第二次 S1。
- **F10 环境可复现：** 新增 `scripts/setup_iotdb206_env.sh`，从
  `pyproject.toml` 安装 `.[dev]` 并自检 `apache-iotdb==2.0.6`；默认目标
  `.venv-iotdb206/` 已 gitignore。脚本已从零执行成功，不再依赖临时 job venv
  路径；2026-11-02 起文档默认使用 Conda `MANTO`，该脚本保留为可选隔离环境。

**第二轮严格审查修复（2026-11-01，F11–F14）：**

- **F11 occurrence/reason：** occurrence 只在决策成功返回后提交，选择或决策构造
  失败会回滚；occurrence 锁持有到决策完整构造结束，并发决策不能插入预留，
  因此失败决策在并发下也不消耗 occurrence。rule_based/adaptive 的 repeat-cache
  理由不再无条件声称“第一次是 Native”，改为说明 S1 从 occurrence 2 才适用；
  新增“预留后失败不计数”、并发失败不污染首次成功、以及“自定义 Registry 首次
  非 Native 时 reason 不撒谎”回归测试；相关 README/策略目录/指标字典/策略注释
  同步限定为“默认 Registry 且首次决策成功时”；
- **F12 迁移证据：** 新增真实 v2、v3、v4、v5 旧库 fixture 和迁移测试，验证旧
  query/decision/execution 行、默认值、缓存 provenance 与新增
  `execution_reports` 表；不再只有 v1 测试却声称覆盖全部旧版本；
- **F13 报告持久化：** SQLite schema v6 新增 `execution_reports`，持久化
  `fallback_used`/`fallback_trigger`/`fallback_reason`/
  `fallback_blocked_reason`、final status/execution、total/end-to-end、
  warnings 和完整 report JSON；service 写报告失败时追加
  `REPORT_PERSISTENCE_FAILED`，不改变已完成执行的结果；
- **F14 doctor 加固：** doctor 只对通过严格无引号树路径子集校验的
  `SHOW TIMESERIES` 结果拼接 EXPLAIN；包含空白、分号、反引号等字符的路径
  跳过探测并告警，不再把 server 返回值直接插入 SQL。

**F15 文档/环境一致性（2026-11-02）：** 统一 `end_to_end_ms` 口径；确认当前
Conda `MANTO`（Python 3.12.13）安装 `apache-iotdb==2.0.6`，`doctor` 实测
server 2.0.6（build `c6ecbde`）/ client 2.0.6；README、本文件、
`ENVIRONMENT_REPORT.md`、`STRATEGY_CATALOG.md` 和 `METRICS_DICTIONARY.md`
的测试/证据命令改为当前 Conda 直接运行，并说明 `.venv-iotdb206/` 是可选项目
venv、`.env` 是独立的本地连接配置/凭据文件。F15 不改变代码、策略或执行路径。

F15 时实测：默认套件 **563 passed/11 skipped**，真实 2.0.6 完整套件
**574 passed**，`tests/unit` **467 passed**，真实集成 **11 passed**，
`compileall` exit 0。2026-11-02 已在 Conda `MANTO`（Python 3.12.13，
`apache-iotdb==2.0.6`）重新复核上述测试和 `doctor` 连接，不再依赖
`.venv-iotdb206/`。阶段 4 修复期间的证据脚本以 30 次 direct/50 次 service
重跑，原始 JSON 保存在 `docs/evidence/repeat_cache_evidence_2026-11-01.json`；
q8/q7/q11 命中结果仍与 Native 一致、无 server dispatch。完整统计见 §4.8 和
`STRATEGY_CATALOG.md` 的 S1 收益证明。上述结果仅作为复审证据，**本文件不自行
宣布重新通过**。

**本轮严格复审 F-1/F-2/F-3 最小修复（2026-11-02）：**

- **F-1 资源阈值未触发保守策略：** 新增版本化外部策略
  `configs/resource_policy.yaml` 和 `decision/resource_policy.py`。策略为
  `client_host`、`db_host`、`db_metrics` 分别配置 CPU/内存/磁盘容量百分比
  上限；只有 source 类型匹配、`valid=true` 且未过期的快照才参与评估。任一
  指标严格大于上限时，`rule_based` 和 `adaptive` 都被强制选择 `S0_NATIVE`，
  `baseline` 也记录策略版本、越限指标和保守基线原因；非 Native 候选的
  `rejection_reason` 明确写为被资源策略否决。缺少、无效、过期或 source 不匹配
  的快照不会被当成零负载，也不会伪造越限。
- **F-2 无效 DB 快照携带指标值：** `_effective_system_state()` 现在对任何带
  指标值的 DB 侧快照检查 source/valid/fresh；无效、过期或 source 不匹配时用
  `unavailable_copy()` 保留身份/时间/source 并将所有指标置空，同时记录对应
  `*_INVALID_DEGRADED`、`*_STALE_DEGRADED` 或
  `*_SOURCE_MISMATCH_DEGRADED` warning。正常的“无 DB 主机 agent、所有值已
  unavailable”快照不会产生噪声 warning；客户端快照仍保持独立。
- **F-3 验收文档缺口：** 在 §6 增加“超阈值资源状态触发保守策略”验收行，补充
  策略文件、代码和测试证据，并在已知限制中明确 DB 主机仍无真实指标源，因此
  DB 阈值只有协议/单元测试，不声称真实 DB 负载证据。

本轮实测：定向策略/决策测试 **52 passed in 0.97s**；默认套件
**598 passed, 11 skipped in 6.05s**；`tests/unit` **502 passed in 5.70s**；
真实集成 **11 passed in 2.37s**；启用真实 2.0.6 的完整套件
**609 passed in 8.24s**；`compileall` 退出码 0。以上仍是复审证据，本文件
不自行宣布重新通过。

**历史方案 A：** 用户此前确认 `query/` 中没有 LAST 所需的
`ORDER BY time DESC LIMIT 1` 需求，因此 LAST 等 `CAND_*` 仍不实现、不启用。
该结论没有因为 S1 的认证而改变；方案 A 只保留为 LAST 等候选的历史决策。

## 2. 任务完成情况

| # | 用户要求 | 结果 | 证据 |
|---:|---|---|---|
| 1 | `StrategySpec`、`StrategyRegistry` | 完成 | `strategies/models.py`、`strategies/registry.py`；外部目录认证、唯一 enabled baseline、插件类身份与目录覆盖检查 |
| 2 | `S0_NATIVE` | 完成 | `strategies/native.py`；原 SQL 不变、无执行选项、无计划效果声明 |
| 3 | 已审计非 Native 策略插件 | **完成** | `strategies/repeat_cache.py` + `adapters/iotdb/result_cache.py`；S1 五项证明见策略目录 |
| 4 | `is_applicable`、候选生成 | 完成 | `ApplicabilityResult`、`CandidatePlan`、`CandidateContext`；S1 的 epoch/子集/occurrence 条件显式记录 |
| 5 | 只读 Validator、兼容检查、候选验证 | 完成 | `strategies/validator.py`；只读 SQL、版本/模型、执行选项、plan effect、parser 范围全部 fail-closed；`cache_epoch` 是唯一已实现选项 |
| 6 | Decision Engine，baseline/rule_based/adaptive | 完成 | `decision/engine.py`；三种模式均为确定性、无 LLM；进程内有界 occurrence 选择 repeat 策略；每个候选保留选择/拒绝原因 |
| 7 | 成本估计：统计或规则，明确 unknown | 完成（有限范围） | `decision/cost.py`；默认 `UNKNOWN`；历史中位数只使用真实成功、非 fallback 样本；S1 不依赖成本估计 |
| 8 | Executor 只执行一个已选候选并记录耗时 | 完成 | `execution/executor.py`；执行前重建并复验候选；核对 strategy/query/SQL hash；把 `cache_epoch` 传给 Adapter；每 attempt 只执行一次 |
| 9 | 仅重试安全时回退 Native | **完成（含生产路径证据）** | `execution/fallback.py`；真实 Executor/Adapter/Session + 受控预 dispatch 缓存故障后，真实 Native 成功且只执行一次 |
| 10 | 决策和执行数据持久化 | 完成 | SQLite schema v6：`decisions`、扩展 `executions`、`execution_reports`（fallback trigger/reason/blocked reason、final status、total/end-to-end、warnings）、缓存 provenance；v1–v5 迁移不丢数据 |
| 11 | 每种策略的语义、生效、失败回退测试 | 完成 | S0 真实集成测试；S1 真实 Native/miss/hit 差分、零 dispatch、真实 fallback；单元测试覆盖边界与失败 |
| 12 | 超阈值资源状态触发保守策略（本轮 F-1） | 完成（最小闸门；DB 主机阈值无真实指标源） | `decision/resource_policy.py` + `configs/resource_policy.yaml`；source/valid/fresh 校验；规则/自适应模式强制 S0；定向测试和完整套件通过 |

## 3. 交付物

| 文件 | 内容 |
|---|---|
| `configs/strategies.yaml` | 外部策略认证目录（schema 1）；`S0_NATIVE` + `S1_REPEAT_CACHE` enabled，11 个 disabled `CAND_*` |
| `configs/resource_policy.yaml` | 版本化资源阈值策略（schema 1）；client/DB-host/DB-metrics 分离的 CPU、内存、磁盘百分比上限 |
| `src/tsdb_optimizer/decision/resource_policy.py` | 策略模型、严格 YAML 加载、source/valid/fresh 资源压力评估、显式越限 warning |
| `src/tsdb_optimizer/strategies/catalog.py` | 目录模型、严格加载/校验、五证明要求、spec 字段比对、`selection_policy` 比对 |
| `src/tsdb_optimizer/strategies/models.py` | `StrategySpec`、`StrategyStatus`、`SelectionPolicy`、`ApplicabilityResult`、`CandidatePlan`、`ValidationResult` 等 Pydantic 契约 |
| `src/tsdb_optimizer/strategies/native.py` | `S0_NATIVE` spec 和 pass-through 插件 |
| `src/tsdb_optimizer/strategies/repeat_cache.py` | `S1_REPEAT_CACHE` spec、确定性子集守卫、epoch 适用性、候选和验证 |
| `src/tsdb_optimizer/strategies/registry.py` | 有限 Registry、外部目录认证、实际插件类检查、baseline 强制、目录覆盖检查、中心 Validator、S1 条件注册 |
| `src/tsdb_optimizer/strategies/validator.py` | 不执行数据库的候选 Validator；`cache_epoch` 选项所有权检查 |
| `src/tsdb_optimizer/cache_epoch.py` | epoch 规范化与长度/控制字符校验，strategy/adapter/config 共用 |
| `src/tsdb_optimizer/adapters/iotdb/result_cache.py` | 进程内有界 LRU 完整结果缓存；epoch + SQL SHA-256 + strategy 键；完整结果准入 |
| `src/tsdb_optimizer/adapters/iotdb/tree_adapter.py` | S0/S1 执行路径；命中不连接/不 dispatch；miss 捕获完整结果并缓存；结构化预 dispatch 错误 |
| `src/tsdb_optimizer/decision/models.py` | 决策、成本、候选评估、adaptive advice、`query_occurrence` 契约 |
| `src/tsdb_optimizer/decision/cost.py` | `UnknownCostEstimator`、`HistoricalCostEstimator` |
| `src/tsdb_optimizer/decision/engine.py` | baseline/rule_based/adaptive；exact SQL occurrence 追踪；repeat/cost 候选分流；资源阈值闸门与无效 DB 快照隔离 |
| `src/tsdb_optimizer/execution/models.py` | `ExecutionAttempt`、`ExecutionReport`、`FallbackDecision`、`OptimizationRun`、`end_to_end_ms` |
| `src/tsdb_optimizer/execution/fallback.py` | 保守 retry-safety 策略 |
| `src/tsdb_optimizer/execution/executor.py` | 单候选执行、执行前插件 + 中心复验、Adapter 结果身份校验、安全 fallback、执行选项透传 |
| `src/tsdb_optimizer/execution/service.py` | 决策 → 先持久化 → 执行 → 记录持久化 → 端到端计时 |
| `src/tsdb_optimizer/schemas/execution.py` | 缓存命中/来源/epoch 字段；命中零 dispatch 不变量；矛盾状态拒绝 |
| `src/tsdb_optimizer/storage/sqlite.py` | schema v6、v1–v5 迁移、`execution_reports`、缓存 provenance、cache hit 排除历史延迟 |
| `src/tsdb_optimizer/cli.py` | `optimize` 的 `--cache-epoch`、`--server-version`、`--database-model`；不掩盖持久化失败 |
| `scripts/repeat_cache_evidence.py` | 可复现的只读 S1 收益证据脚本（prints JSON） |
| `tests/unit/test_result_cache.py` | 键隔离、LRU、容量、不可变性、完整结果准入 |
| `tests/unit/test_repeat_cache_strategy.py` | S1 spec、epoch 门控、SQL 子集、候选/验证/篡改拒绝 |
| `tests/unit/test_decision_engine.py` | S0/S1 occurrence 选择、重复优先、重置、资源阈值强制 S0、无效/过期/UNKNOWN DB 快照隔离 |
| `tests/unit/test_resource_policy.py` | 版本化策略加载、严格阈值边界、source 分离、无效/过期/不匹配快照跳过、配置 fail-closed |
| `tests/integration/test_repeat_cache_certification.py` | 真实 2.0.6 correctness/effect/benefit/fallback/no-epoch 证据 |
| `tests/unit/phase4_support.py` | **TEST ONLY** 策略/Adapter 双桩；不进入生产 Registry |

阶段 0/1/2/3 的 fixture、Adapter、SQLite、CLI、Parser 和文档未删除、未削弱。

## 4. 实现语义

### 4.1 S1 的缓存身份与 epoch 契约

S1 只缓存**完整结果**，键为：

```text
SHA-256("repeat-cache-v1" + cache_epoch + strategy_id + exact cleaned SQL SHA-256)
```

- `cache_epoch` 由调用方通过 CLI `--cache-epoch`、环境变量
  `TSDB_OPTIMIZER_CACHE_EPOCH` 或 service 的 registry 构造参数声明；
- epoch 必须非空、≤200 字符、无控制字符；空白会被规范化；
- 没有 epoch 时 S1 的 `is_applicable` 为 false，默认选择 S0；
- occurrence 只对**成功返回的决策**计数，失败决策不消耗 occurrence；
- 默认 Registry 下，同一进程内第一条成功的 exact SQL 选择 S0 Native；第二
  条及以后才可能选择 S1；失败决策不会让后续第一次成功决策被误判为重复；
- 命中直接返回已捕获的列、完整行数和预览，不连接 IoTDB、不发送请求；
- 缓存只接受完整结果；超过行/字节上限时记录 `CACHE_NOT_STORED`，不截断；
- 进程退出即丢失；没有跨进程/跨实例共享，也没有 server 端失效协议。

**正确性边界：** 调用方必须保证同一 epoch 内数据不变。外部写入、TTL、重启或
schema 变化需要更换 epoch；S1 不检测这些事件，也不声称 fresh-result 语义。

### 4.2 SQL 子集与安全边界

S1 不生成、不重写 SQL，执行 exact cleaned SQL。可缓存 SQL 必须：

- 通过只读守卫，且只有一条 SELECT；
- 只包含保守 token 白名单中的 token；
- 函数名必须在 `count/sum/avg/min/max` 白名单内；
- 不含字符串、占位符、JOIN、UNION、CAST、`now()`、`rand()`、`uuid()`、
  `last_value`、DDL/DML 或多语句。

不在子集内的 SQL 只是 S1 不适用，原 SQL 仍可由 S0 Native 执行；不会猜测或
自动改写。测试覆盖 q8（LAST + wildcard）、q7（interval `GROUP BY` + count）、
q9（Native DESC + LIMIT）和上述负例。

**Parser 范围（F6）：** S1 的候选 SQL 必须等于原始 cleaned SQL，且不执行任何
改写；因此中心 Validator 对这类 non-baseline 候选使用显式
`EXACT_SQL_NO_REWRITE` 策略：不要求 Agent-1 parser 覆盖，server 仍是执行语义
权威。它不是“Native passthrough”，也不是绕过 parser 的自动改写；需要改写的
候选仍必须通过 parser 范围检查。`test_strategy_validator.py` 覆盖 parser
`UNSUPPORTED` 和缺少 analysis 两种场景。

### 4.3 决策模式与 occurrence

| 模式 | 行为 |
|---|---|
| `baseline` | 永远选择 S0，不切换到其他候选 |
| `rule_based` | exact SQL occurrence >= 2 且存在已应用的 repeat 候选时选择 S1；否则 S0；非 repeat 候选仍需已知成本低于已知 S0 成本 |
| `adaptive` | 只接受调用方显式提供的 `AdaptiveAdvice`；没有建议或建议无效则 S0；若建议指向 repeat-cache 策略且 occurrence < 2，强制回退 S0（F9） |

occurrence 是 DecisionEngine 进程内的有界 exact cleaned SQL 计数，不持久化；
缓存淘汰/进程重启后重新从 1 开始，安全回到 Native。occurrence 只用于选择
repeat 策略，不会被当作结果新鲜度证据。adaptive 建议也不能绕过该契约：
occurrence 1 即使收到 S1 建议，仍选择 S0 并记录
`REPEAT_CACHE_REQUIRES_REPEAT`。每个候选仍记录 applicability、candidate、
validation、cost、选中/拒绝原因和 warnings。

### 4.4 Executor、结果身份与缓存命中

正常路径：

1. 从 Registry 重新取已选策略；
2. 用原始 SQL 重新构建候选，并比较 SQL、执行选项、策略版本；
3. 运行插件 Validator 和 Registry 中心 Validator，合并 checks；
4. 由 Adapter 执行一次，并把候选的执行选项（S1 为 `cache_epoch`）传入；
5. 核对返回的 `strategy_id`、`query_id`、`sql_sha256` 与候选一致；
6. 成功即结束，不 double execution。

缓存命中的 `ExecutionResult` 必须满足：`SUCCESS`、`served_from_cache=true`、
`request_dispatched=false`、`request_completed=false`、`retry_safe=false`、
`fetch_mode=FULL`、非空 `cache_source_execution_id`（且不等于自身）、非空
`cache_epoch`，并满足 `preview_truncated == (row_count > len(rows_preview))`。
非命中结果不得设置 `cache_source_execution_id`。缓存查找/身份校验失败在
dispatch 前返回结构化 `ERROR`，`request_dispatched=false`，允许保守 Native
fallback。

### 4.5 回退政策与真实证据

回退条件未放宽：

| 情况 | 是否回退 |
|---|---|
| 候选在 dispatch 前验证/重建失败 | 允许（无请求发出） |
| 第一次请求明确 `request_dispatched=false` | 允许 |
| 第一次请求完成且 Adapter 明确 `retry_safe=true` | 允许 |
| 第一次请求成功 | 不需要 |
| 超时 | 禁止；服务端可能仍在执行 |
| 完成状态未知 | 禁止；避免重复工作 |
| S0 自身失败 | 不回退到自身、不重试 |

真实 2.0.6 证据使用真实 `IoTDBTreeAdapter`、真实 `Session`、
`DecisionExecutionService` 和 IoTDB；故障注入点为进程内缓存查找
（`_FailingLookupCache.get` 抛异常），它在任何 server 请求之前失败：

- S1 attempt：`ERROR`、`request_dispatched=false`、`CACHE_LOOKUP_FAILED`；
- 真实 Executor 触发 `FIRST_REQUEST_NOT_DISPATCHED`，执行真实 S0 Native；
- fallback 成功，整个 run 只有 1 次真实 server 请求；
- SQLite 记录 `[S1_REPEAT_CACHE, S0_NATIVE]`，第一行 dispatch=0，第二行
  dispatch=1。

这证明生产代码路径上的真实 Native fallback。它不是自然 IoTDB 故障实验；
自然超时/连接中断仍不会自动回退，因为 Adapter 不会把它们标为 retry-safe。

### 4.6 SQLite schema v6

v2 新增 `decisions`；v3 新增 `candidate_sql_sha256` 和
`record_schema_version`；v4 新增 `fetch_mode`；v5 新增：

- `served_from_cache`（0/1，默认 0）；
- `cache_source_execution_id`（缓存命中时指向真实 miss execution）；
- `cache_epoch`。

v6 新增 `execution_reports`（每个 decision 一行），持久化此前只在返回
`ExecutionReport` 中的报告级字段：

- `fallback_used`、`fallback_trigger`、`fallback_reason`、
  `fallback_blocked_reason`；
- `final_status`、`final_execution_id`；
- `total_wall_ms`、`end_to_end_ms`；
- `warnings_json` 与完整 `report_json`；
- `created_at`、`record_schema_version`。

`initialize()` 从 v1–v5 原位迁移，保留旧行；新增列/表有默认值或允许 NULL。
决策在执行前写入；每个 attempt 执行后独立持久化；报告完成后写入
`execution_reports`。任一步写库失败仍保留真实结果并追加
`EXECUTION_PERSISTENCE_FAILED` 或 `REPORT_PERSISTENCE_FAILED`，不会把成功查询
误报为失败，也不伪造 execution/report 行。历史延迟查询排除
`served_from_cache=1` 的行；缓存 hit 的耗时是本地查找，不是 server 执行延迟。

**持久化边界：** 完整结果行仍不写入 SQLite；`ExecutionReport.end_to_end_ms`
现已持久化到 `execution_reports`。报告持久化本身发生在端到端计时之后，因此
`end_to_end_ms` 覆盖请求入口到 attempt 持久化，不覆盖 report 行写入耗时。

### 4.7 外部认证目录

`configs/strategies.yaml` 仍是 Registry 之外的认证来源：

- schema 1、策略 ID 唯一、恰好一个 enabled baseline；
- enabled 条目必须 `VERIFIED`，有 version 和 implementation_class；
- enabled 非 Native 条目必须有入口/正确性/生效/收益/回退五项证明；
- `selection_policy` 也必须与目录一致；
- 实际插件类必须等于 `type(plugin).__module__ + "." + qualname`；
- 目录中所有 enabled 条目必须由运行代码注册，否则启动失败；
- 目录只声明数据，不导入实现、不连接 IoTDB、不自动启用 disabled 候选。

当前目录：S0（baseline, VERIFIED, 0.1.0）、S1（non-Native, VERIFIED, 1.0.0）、
11 个 disabled `CAND_*`。S1 的 proof anchors 指向本仓库
`docs/STRATEGY_CATALOG.md` 的实际章节。

### 4.8 真实执行与收益证据

**正确性/生效（q8/q7/q11，真实 2.0.6，只读）：**

| workload | 行数 | 列数 | Native/miss/hit |
|---|---:|---:|---|
| q8 | 200 | 4 | 列名/类型、行数、全部预览行一致 |
| q7 | 303 | 5 | 列名/类型、行数、全部预览行一致 |
| q11 | 605 | 5 | 列名/类型、行数、全部预览行一致 |

真实 service 三次 run：occurrence 1 S0（dispatch=1），occurrence 2 S1 miss
（dispatch=1，`CACHE_MISS_STORED`），occurrence 3 S1 hit（dispatch=0，
`cache_source_execution_id` 指向 run 2）。真实 `Session` 计数为 2 次请求。

**直接 Adapter 收益（30 次 Native + 30 次 S1，单位 ms）：** S1 第 1 次 miss，
后 29 次 hit；完整 n/mean/median/p50/p95/p99/variance/stdev 见
`STRATEGY_CATALOG.md` 的 S1 收益证明和原始 JSON。

| workload | Native mean | Native median | Native p95 | Native p99 | Native variance | S1 hit median | S1 hit p95 | S1 hit variance | hit vs Native median |
|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|
| q8 | 20.766 | 20.824 | 24.602 | 24.973 | 7.890 | 0.177 | 0.233 | 0.003 | 117.65× |
| q7 | 16.226 | 15.090 | 16.225 | 39.374 | 38.172 | 0.141 | 0.153 | 0.000 | 107.02× |
| q11 | 26.489 | 25.867 | 28.908 | 31.688 | 2.439 | 0.228 | 0.375 | 205.595 | 113.45× |

q11 的 hit 有一个 77.469 ms 离群值，因此 mean/p99/variance 明显变大；典型命中
延迟看 median。Native 30 次均 dispatch；S1 30 次总计只有 1 次 server dispatch。
miss 样本只有 1 个/workload（26.419/17.038/30.719 ms），不作为稳定估计。

**q8 端到端服务（50 次 run）：**

| 路径 | dispatch | hits | mean e2e | median e2e | p95 | p99 | variance | 中位非客户端开销 | 中位决策开销 |
|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|
| 启用 S1（声明 epoch） | 2 | 48 | 47.832 | 45.367 | 64.941 | 84.535 | 86.569 | 45.166 | 15.876 |
| Native-only（无 epoch） | 50 | 0 | 66.858 | 67.609 | 71.098 | 83.154 | 29.067 | 43.821 | 11.785 |

中位 **1.49×**、均值 1.40×、p95 1.09×；p99 84.535 vs 83.154 ms，缓存路径
略差约 1.7%，因此不声称尾延迟优势。hit end-to-end（n=48）中位 45.230 ms，
而 hit `client_wall_ms` 中位仅 0.148 ms；非客户端开销中位 45.053 ms，占
hit end-to-end 的 99% 以上。决策开销中位 15.876 ms，比 Native-only 的
11.785 ms 高约 4.1 ms。分析、决策和 SQLite 审计写入未缓存，不能把 0.15 ms
的 Adapter 命中时间当作整个服务延迟。cache lookup 时间不计入
`result_fetch_ms`，只出现在 `CACHE_HIT` warning 和 `cache_hit` 结构化日志的
`cache_lookup_ms` 字段。

**环境/限制：** 单机、同主机、静态热数据、单进程、无并发/背景负载控制；
没有冷缓存、多进程、多日或 DB 主机指标。这是受控微基准，不是生产 benchmark。
可复现命令（当前 Conda `MANTO`，已确认 `apache-iotdb==2.0.6`）：

```bash
cd /opt/program-code/MANTO
PYTHONPATH=src python scripts/repeat_cache_evidence.py \
  > docs/evidence/repeat_cache_evidence_2026-11-01.json
```

如需隔离环境，可先运行 `scripts/setup_iotdb206_env.sh` 创建项目本地
`.venv-iotdb206/`，再把命令中的 `python` 换成
`.venv-iotdb206/bin/python`。该 venv 不是 `.env`；`.env` 只保存连接配置和
本地凭据（已 gitignore，权限 600）。

### 4.9 CLI 与使用边界

`optimize` 新增：

- `--cache-epoch`：声明不可变 dataset epoch；也可用
  `TSDB_OPTIMIZER_CACHE_EPOCH`；
- `--server-version` / `--database-model`：把已探测的兼容信息传给决策；
- 单次 CLI 进程只执行一条 SQL，通常只能观察 occurrence 1 的 Native。要观察
  miss/hit，必须在同一进程内重复调用 `DecisionExecutionService`（或使用未来
  的批量 workload 入口）；这是缓存的设计边界，不是测试遗漏。

### 4.10 成本估计

- 默认 `UnknownCostEstimator`：`source=UNKNOWN`、无 latency、无 confidence、
  有明确 `unknown_reason`；
- `HistoricalCostEstimator` 按实际执行候选 SQL 的 SHA-256 + strategy ID 查询
  真实成功、非 fallback 历史；达到 `min_samples` 才返回中位数；
- 历史中位数不伪造置信度；失败/fallback/缓存 hit 不进入 server 延迟历史；
- 尚未按 server 版本、数据库模型、`fetch_mode`、结果规模、缓存/负载上下文
  隔离。S1 选择不依赖该成本估计。

### 4.11 资源阈值与保守回退（本轮 F-1）

`configs/resource_policy.yaml` 是外部、版本化、严格校验的配置，当前
`schema_version=1`、`policy_version=2026-11-02.1`。每个 source 分别配置
`cpu_percent`、`memory_percent`、`disk_usage_percent` 上限，默认均为 90.0。
这些数值是保守的项目策略，不是实测的 IoTDB 安全上限。

评估规则：

- 只评估 `source_type` 与位置一致、`valid=true` 且未过期的快照；
  `client_host` 只使用 `CLIENT_HOST`，`db_host` 只使用 `DB_HOST`，
  `db_metrics` 只使用 `DB_METRICS`；
- 新鲜窗口为 `captured_at <= now < valid_until`；未来时间无容差，隔离原因
  为 `FUTURE_SNAPSHOT`。各来源独立判定，不因客户端失效丢弃有效 DB 状态；
- 只有指标值**严格大于**上限才算越限；等于上限不越限；
- 缺少、无效、过期、source 不匹配的快照不参与评估，也不被当作零负载；
- 任一指标越限时，`rule_based` 和 `adaptive` 均选择 Registry 的 baseline
  `S0_NATIVE`；`adaptive` 的调用方建议不能绕过该闸门；`baseline` 模式本来就
  选择 S0，但同样记录越限原因；
- decision 的 `warnings` 包含 `RESOURCE_THRESHOLD_EXCEEDED`、
  `policy_version`、具体 `source.metric=value>limit` 和“强制保守基线”；
  非 Native 的 `CandidateEvaluation.rejection_reason` 记录被资源策略否决；
- I/O 速率和 `active_queries` 不在本轮最小策略的阈值范围内；没有为它们虚构
  通用安全上限；
- 策略文件缺失、不可读、YAML 非法、字段缺失、schema 版本不支持都
  fail-closed 为 `ConfigError`，不会退化成“无策略、无阈值”。

## 5. 测试与结果

测试环境由 **Conda `MANTO`**（Python 3.12.13，`apache-iotdb==2.0.6`）提供：

```bash
cd /opt/program-code/MANTO
python -c "import importlib.metadata as m; print(m.version('apache-iotdb'))"  # 必须输出 2.0.6
```

2026-11-02 已用该环境重新执行下列全部测试；耗时随机器和负载波动。项目本地
`.venv-iotdb206/` 是可选的隔离 venv，由 `scripts/setup_iotdb206_env.sh`
创建，不是必需的，也不是 `.env`；若使用它，把下述命令中的 `python` 换成
`.venv-iotdb206/bin/python`。

### 5.1 默认完整套件（真实集成跳过）

```bash
cd /opt/program-code/MANTO
PYTHONPATH=src python -m pytest -q
```

实际结果：

```text
598 passed, 11 skipped in 6.05s
```

### 5.2 完整套件（真实 IoTDB 2.0.6，只读）

```bash
IOTDB_INTEGRATION=1 PYTHONPATH=src python -m pytest -q
```

实际结果：

```text
609 passed in 8.24s
```

### 5.3 分组结果

R1/R2 定向测试：

```bash
PYTHONPATH=src python -m pytest -q \
  tests/unit/test_decision_engine.py tests/unit/test_monitoring_schema.py tests/unit/test_resource_policy.py
```

```text
90 passed in 1.27s
```

完整单元套件：

```bash
PYTHONPATH=src python -m pytest -q tests/unit
```

```text
502 passed in 5.70s
```

```bash
IOTDB_INTEGRATION=1 PYTHONPATH=src python -m pytest -q tests/integration
```

```text
11 passed in 2.37s
```

真实集成包括：

- `test_phase4_native_decision_execution_and_persistence`：真实 S0 决策、执行、
  SQLite 持久化，正常路径 EXPLAIN 哨兵；
- `test_phase4_cli_optimize_end_to_end`：CLI `optimize --mode baseline`；
- `test_real_native_and_cache_results_are_identical`：q8/q7/q11 Native/miss/hit
  全量预览差分；
- `test_real_service_selects_native_then_cache_and_persists_provenance`：
  三次 service run、occurrence 1/2/3、真实 Session 计数、SQLite provenance、
  历史延迟排除 hit；
- `test_real_native_fallback_after_pre_dispatch_cache_failure`：真实
  Executor/Adapter/Session 的预 dispatch 缓存故障 fallback；
- `test_real_service_without_epoch_never_selects_the_cache`：无 epoch 两次 S0，
  缓存为空。

### 5.4 编译检查

```bash
PYTHONPATH=src python -m compileall -q src tests scripts
```

退出码 0，无输出。

### 5.5 测试双桩边界

`tests/unit/phase4_support.py` 中的策略/Adapter 是 **TEST ONLY**：

- 不进入 `build_default_registry()`；
- 不出现在“已认证策略”中；
- 只验证 Registry 拒绝、决策选择、执行器回退和持久化不变量；
- 不作为 S1 认证证据。S1 的证据来自真实 2.0.6 集成测试。

## 6. 阶段 4 验收

| 验收条件 | 结果 | 证据 |
|---|---|---|
| 只能从 Registry 选择策略 | **PASS** | Executor 执行前 `assert_selectable`、重建候选、核对身份与 SQL hash；伪造 selected ID、同 ID 伪造 spec、自证插件、伪造实现类均有拒绝测试 |
| Native fallback 可用 | **PASS** | 真实 Executor/Adapter/Session + 预 dispatch 缓存故障后真实 Native 成功；safety 规则未放宽；自然超时仍禁止回退 |
| 非 Native 策略必须有正确性测试 | **PASS** | S1 真实 q8/q7/q11 Native/miss/hit 列、行数、全部预览行一致；空/NULL/重复时间戳/时间边界/NaN/Inf/顺序另有专项测试；固定 epoch 契约 |
| 未验证策略不得启用 | **PASS** | `StrategySpec` + 外部目录 + Registry 多层拒绝；默认 Registry 只含 S0/S1；enabled 目录条目缺少实现会启动失败 |
| 不同输入可解释决策 | **PASS** | baseline/rule_based/adaptive、成本 unknown/历史、候选拒绝、repeat occurrence 均记录原因和 warnings；adaptive 的 repeat 建议受 occurrence >= 2 约束 |
| 超阈值资源状态能触发保守策略（手册 §6.2） | **FIXED，待复审；真实 DB 指标 BLOCKED** | R1 已改为各来源独立隔离；`test_bad_client_cannot_hide_high_db_pressure` 覆盖 rule_based/adaptive × DB_HOST/DB_METRICS × 客户端 invalid/stale/future。R2 的未来时间和窗口边界测试通过。真实 DB 负载仍无采集源，不能以合成样本替代 |
| 无 LLM 可完整运行 | **PASS** | 全部模式无 LLM；真实集成 CLI 不调用 LLM |
| 只执行一个已选候选 | **PASS** | 成功路径 Adapter 调用次数 = 1；fallback 仅在明确安全时增加一次 |
| 正式查询不默认双执行 | **PASS** | 执行成功即返回；无并行/对照执行；`EXPLAIN ANALYZE` 无入口 |
| 决策和执行持久化 | **PASS** | schema v6、真实集成 SQLite provenance、v1–v5 迁移测试、候选 SQL hash 历史、`fetch_mode`、缓存字段、`execution_reports` 报告级 fallback/warnings/end-to-end |
| 矛盾状态与 UTC | **PASS** | 成功必须 dispatch/完成且不能 timeout；缓存命中必须零 dispatch；时间戳必须 UTC-aware；`record_schema_version` 持久化 |
| 客户端/DB 指标隔离与无效 DB 快照（本轮 F-2） | **PASS** | `SystemSample` 禁止 client source 冒充 DB；`_effective_system_state()` 对带值的无效/过期/source 不匹配 DB 快照执行 `unavailable_copy()`，保留身份并置空所有指标；正常无 DB agent 的全 unavailable 快照不产生噪声 warning。证据：`test_invalid_db_host_values_are_withheld_and_do_not_trigger_threshold`、`test_invalid_db_metrics_values_are_withheld_and_do_not_trigger_threshold`、`test_unknown_db_source_with_values_is_withheld`、`test_already_unavailable_db_snapshot_does_not_emit_degradation_noise` |
| 项目原阻塞标记 | **PASS（已解除）** | `S1_REPEAT_CACHE` 五项证明完成；但阶段 4 当前仍为 `PENDING RE-REVIEW` |
| 严格审查 F1–F5 | **FIXED，待复审** | 缓存元数据不可变、SQL guard fail-closed、hit 计时语义、hit 日志 ID、失败路径回归测试均见 §1 修复说明 |
| 严格审查 F6–F10 | **FIXED，待复审** | `EXACT_SQL_NO_REWRITE`、完整收益统计、边界类别测试、adaptive occurrence 门槛、可复现 2.0.6 环境脚本均见 §1 修复说明 |
| 严格审查 F11–F14 | **FIXED，待复审** | F11 失败决策不消耗 occurrence（含并发预留回滚）且 reason 不再声称首次为 Native；F12 真实 v2/v3/v4/v5 迁移测试；F13 schema v6 `execution_reports` 持久化报告级 fallback/warnings/end-to-end；F14 doctor 只对安全未加引号 tree path 做 EXPLAIN 探测 |
| 严格审查 F15 | **FIXED，待复审** | 统一 `end_to_end_ms` 口径；同步测试环境说明为 Conda `MANTO` + `apache-iotdb==2.0.6`，澄清 `.venv-iotdb206/` 是可选项目 venv、`.env` 是凭据文件；刷新 2026-11-02 实测耗时 |
| 本轮严格复审 F-1/F-2/F-3 | **FIXED，待复审** | F-1 版本化资源阈值闸门；F-2 无效 DB 快照带值隔离；F-3 验收表与限制文档补齐。证据见 §1、§4.11 及对应测试 |

**阶段 4 结论：** 在**固定、调用方声明的不可变 dataset epoch**契约下，
`S1_REPEAT_CACHE` 的五项证明完成，两个原始研究阻塞项解除。但严格审查曾判定
`NOT ACCEPTED`；F1–F15 以及本轮 F-1/F-2/F-3 的修复和回归测试已经提交为复审
证据。因此当前状态是
**`PENDING RE-REVIEW（前序修复 + R1/R2 修复已应用）`**，不能
自行宣布重新通过。该结论也不外推到通用 freshness、跨进程缓存、自然 server
故障自动回退或生产 benchmark 收益；服务端收益只声称中位/均值改善，不声称尾
延迟优势。资源阈值的 DB 主机部分没有真实 sampler，不能外推为真实 DB 负载
证据。未进入阶段 5。

## 7. 策略状态

| 策略 | 状态 | 阶段 4 是否启用 |
|---|---|---|
| `S0_NATIVE` | `VERIFIED` | 是（基线/回退） |
| `S1_REPEAT_CACHE` | `VERIFIED`（固定 epoch 契约） | 是；仅 epoch + exact SQL 重复时适用 |
| `CAND_LAST_SINGLE_SERIES` | `NOT_VERIFIED` | 否 |
| `CAND_MAXTIME_LASTVALUE` | `NOT_VERIFIED` | 否 |
| `CAND_ALIGN_BY_DEVICE` | `NOT_VERIFIED` | 否 |
| `CAND_FETCH_SIZE` | `NOT_VERIFIED` | 否 |
| `CAND_PREDICATE_REWRITE` | `NOT_VERIFIED` | 否 |
| `CAND_SERIES_PATH` | `UNSUPPORTED` | 否 |
| `CAND_UNION_MERGE` | `UNSUPPORTED` | 否 |
| `CAND_PARALLEL_SPLIT` | `NOT_VERIFIED` | 否 |
| `CAND_DB_PARAMETER` | `NOT_VERIFIED` | 否 |
| `CAND_TABLE_MODEL` | `UNSUPPORTED`（无用户表） | 否 |
| `CAND_CLIENT_CACHE` | `NOT_VERIFIED`（被 S1 取代，保留审计） | 否 |

## 8. 已知限制与未完成事项

### 8.1 S1

- 进程内、单进程；多进程/多实例不共享，进程退出即丢失；
- 正确性依赖调用方维护不可变 epoch；没有 server 端失效/数据版本 API；
- 默认 Registry 下第一条成功的 exact SQL 仍为 Native；第二条才可能填充缓存；
  失败决策不计数，因此不会造成“未执行就命中”的假重复；
- 只缓存完整结果；超大结果不缓存；
- CLI 单进程单条 SQL 通常无法观察 hit；
- 没有冷缓存、并发、多日、背景负载或 DB 主机指标；
- 收益是受控热缓存微基准，不是生产 benchmark；direct hit 中位改善明显，
  但 service 端到端只声称中位/均值改善（q8 中位 1.49×），p99 不优于
  Native-only（84.535 vs 83.154 ms），不能外推为尾延迟保证；
- 不是服务端计划优化；EXPLAIN 不会出现 S1 节点；
- **exact-SQL parser 策略：** S1 可对 Agent-1 标记为 `UNSUPPORTED` 的 exact
  SQL 适用，只要该 SQL 通过 S1 自己的确定性 token 子集守卫；它不解析/不改写
  SQL，只按 exact cleaned SQL 缓存。这是显式的 `EXACT_SQL_NO_REWRITE` 策略，
  不是绕过 parser 的自动改写；中心 Validator 对该候选返回 PASSED，其他需要
  改写的候选仍必须通过 parser 范围检查；
- 缓存键未包含策略实现版本或 server 身份；当前只在单进程默认 Registry 下
  使用，Phase-5 隔离设计前不能外推到多版本/多 server 共享；
- 并发相同的 miss 没有 single-flight 保护，可能重复 dispatch；这是 Phase-5
  优化项，不影响当前串行证据。

### 8.2 决策与执行

- `adaptive` 没有学习器；只接受调用方提供的 `AdaptiveAdvice`；repeat-cache
  建议在 occurrence < 2 时仍被拒绝并回退 Native（F9）；
- 只有 `cache_epoch` 一个执行选项；其他非空选项被 Validator 拒绝；
- 没有已验证的 plan-effect verifier；声明计划效果的策略无法启用；
- 历史成本未按 server 版本、数据库模型、`fetch_mode`、结果规模、缓存/负载
  上下文隔离；
- `allow_uncertified_for_testing` 仍是代码级测试旁路；生产入口不使用；
- `applicability_rules` 只保证顶层只读；嵌套值仍是文档元数据；
- 真实 Adapter 不会把未知完成状态的失败标为 retry-safe；自然超时/连接中断
  不会自动回退；已证明的是预 dispatch 缓存故障后的受控 fallback；
- 没有服务端取消、查询 ID 终止或连接池；
- `timed_out` 仍是客户端观测，不是服务端取消证明；
- 成功/失败路径都不做结果正确性双执行；
- 正常路径不调用 `EXPLAIN`/`EXPLAIN ANALYZE`；
- `end_to_end_ms` 已写入 schema v6 `execution_reports`；其测量截至 attempt
  持久化，不包含 report 行自身写入或序列化耗时。

### 8.3 持久化与监控

- SQLite schema v6 保存决策/执行元数据与报告级 fallback/warnings，不保存完整结果行；
- 持久化的 SQL 是 guard 清理后的文本（去掉注释）；不是原始输入字节，审计时
  不能据此证明用户原始注释/空白；原始文本不落库是当前隐私/体积取舍；
- Agent 3 的 `ExecutionRecord` 仍保存在进程内有界 deque，尚未与 schema v6 的
  decision/execution/report 行合并；
- `optimize` 当前不启动 Agent 3 监控线程，也不把 `SystemSample` 持久化；
- 没有 DB 主机指标代理，DB 主机指标仍全部 unavailable。

### 8.4 安全与凭据

- `.env` 保持 gitignored、权限 600；`.env.example` 只含非敏感模板项
  （host/port/user/timeout/SQLite/日志/预览行），密码为
  `IOTDB_PASSWORD=replace-me` 占位值；
- 密码未写入文档、日志、SQLite 或仓库；
- 真实集成测试只执行只读 SELECT；没有 IoTDB 写入；
- 未提交 Git，等待用户决定。

### 8.5 资源阈值策略（本轮 F-1/F-2）

- 阈值是**项目保守策略**，不是从 IoTDB 2.0.6 实测得到的 server 安全上限；
  默认 90% 可被运维修改，但修改后应视为新的 `policy_version`；
- 仅覆盖 `cpu_percent`、`memory_percent`、`disk_usage_percent`；I/O 速率和
  `active_queries` 没有通用安全阈值，本轮不纳入，不虚构；
- 只有调用方提供 `SystemSample` 时才会评估；`optimize` CLI 当前不采样/不持久化
  系统状态，因此 CLI 端到端路径尚未触发资源闸门。`DecisionExecutionService.run`
  和 `DecisionEngine.decide` 已接受 `system_state`，可供后续调度器/Agent 3
  接入；
- DB 主机没有真实 metrics agent；`UnavailableDbHostSampler` 只产生全
  unavailable 快照，所以 `db_host`/`db_metrics` 阈值只有合成协议/单元测试，
  **没有真实 DB 负载证据**，也不能用 client psutil 值冒充；
- 阈值只做全局非 Native veto，不按策略 ID、查询模板或历史成本做差异化惩罚；
  这是当前最小修复的范围，不是完整学习型资源代价模型；
- 缺少、无效、过期、source 不匹配的快照不会被当成零负载；但它们也不会凭空
  触发越限。若要求“无实时状态时一律回退 S0”，需要单独的产品/调度决策，当前
  未改变无状态调用的既有行为；
- 越限信息写入 decision 的 reason/warnings；没有在无越限时单独持久化
  `policy_version` 字段，完整策略版本审计仍依赖配置文件和代码版本。

## 9. 前序阶段回归基线

| 阶段 | 结果 |
|---|---|
| 阶段 0 | 47 workload、24 负例、12 PLAN fixture；原 `BLOCKED` 现由 S1 认证解除 |
| 阶段 1 | S0-only 基础工程、Adapter、SQLite v1、CLI、doctor/query、计时 |
| 阶段 2 | Agent 1 Parser/QueryFeatures；47 workload 中 34 SUPPORTED / 13 UNSUPPORTED |
| 阶段 3 | Agent 3、客户端采样、DB 主机 unavailable、计划解析/hash、进程内 ExecutionRecord |

阶段 4 没有删除或削弱这些测试；SQLite v1/v2/v3/v4/v5→v6 迁移测试验证旧记录
保留、默认值和新表可用。

## 10. 下一步

1. **不进入阶段 5：** 阶段 4 当前为 `PENDING RE-REVIEW`，必须先由严格审查
   确认前序 F1–F15 以及本轮 F-1/F-2/F-3 修复；用户也未授权阶段 5，不得自行
   开始 Agent 4/学习器/LLM。
2. **资源阈值边界：** 当前只有调用方提供 `SystemSample` 时才评估；若要声称
   CLI/调度端到端资源闸门，需接入真实采样和持久化。DB 主机阈值在拿到真实
   DB metrics source 前只能标记为协议级证据，禁止用 client psutil 值冒充。
3. **维持 S1 边界：** 只允许调用方声明的不可变 epoch；不得把 S1 当成通用
   freshness 缓存，也不得在真实写入 workload 中默认启用。
4. **若要扩展 S1：** 优先增加批量 workload 入口、跨进程/失效方案、并发测试、
   冷缓存/多日/DB 主机指标；这些都需要新的设计和授权。`end_to_end_ms` 已在
   schema v6 的 `execution_reports` 中持久化。
5. **其他候选：** LAST 等仍需 TTL/重启/并发/NaN、需求证明、结果归一化、专用
   插件和生产回退；通过前保持禁用。
6. **Git：** 用户确认后再按阶段提交；不要自动提交。

## 11. 变更记录

| 日期 | 变更 |
|---|---|
| 2026-11-01 | 完成阶段 0 环境/能力审计；原标记 `BLOCKED: NO_VERIFIED_NON_NATIVE_STRATEGY` |
| 2026-11-01 | 完成 LAST 只读与授权可写边缘认证；LAST 仍 `NOT_VERIFIED` |
| 2026-11-01 | 完成 LAST 只读复核：全量 24,000 条 workload 需求扫描 0 条符合、变体边界/结果契约、两次 120 块扩展计时；仍 `NOT_VERIFIED`，未启用 |
| 2026-11-01 | 完成阶段 1 S0-only 基础工程；完整测试 60 passed/2 skipped；真实集成 2 passed |
| 2026-11-01 | 完成阶段 2 Agent 1；47 workload 34 SUPPORTED/13 UNSUPPORTED；完整测试 206 passed/2 skipped |
| 2026-11-01 | 完成阶段 3 Agent 3/采样/计划解析；完整测试 271 passed（真实集成 3 passed） |
| 2026-11-01 | 完成阶段 4 Registry/Validator/Decision/Executor/fallback/SQLite v2；默认仅 S0；完整测试 371 passed/5 skipped（启用真实集成 376 passed）；未进入阶段 5 |
| 2026-11-01 | 修复阶段 4 审查 F1/F2：Registry 拒绝同 ID 伪造 spec，`StrategySpec` frozen；Decision Engine/Executor/fallback 强制执行 Registry 中心 Validator 并合并插件结果；新增 6 个回归测试。完整测试 377 passed/5 skipped（启用真实集成 382 passed），真实集成 5 passed |
| 2026-11-01 | 修复阶段 4 审查 F3–F16：SQLite v3 候选 SQL hash 与 `record_schema_version`；结构化 SQL 守卫；列类型失败可见；执行持久化隔离；stale 客户端状态降级；Registry 认证推导；UTC/矛盾状态不变量；非 AdapterError 包装；日志字段补齐；默认 Registry 接入 Parser。完整测试 413 passed/5 skipped（启用真实集成 418 passed） |
| 2026-11-01 | 阶段 4 框架修复：外部认证目录、五证明强制、实际插件类身份检查、目录覆盖检查、只读 `applicability_rules`、Adapter SQL hash 校验、SQLite v4 `fetch_mode`、doctor 2.0.6 门控、端到端计时、stale DB 快照隔离。完整测试 446 passed/5 skipped（启用真实集成 451 passed），真实集成 5 passed，compileall 0 |
| 2026-11-01 | 用户确认方案 A：`query/` 为当前完整 workload，LAST 等无非 Native 需求，不虚构/不启用候选；当时保持 `BLOCKED` |
| 2026-11-01 | 解决两个阶段 4 阻塞项：新增 `S1_REPEAT_CACHE`（进程内固定 epoch 精确 SQL 缓存）、SQLite v5 缓存 provenance、occurrence 决策、CLI `--cache-epoch`、真实 q8/q7/q11 correctness/effect/benefit/fallback 测试。默认完整套件 522 passed/11 skipped；真实 2.0.6 完整套件 533 passed；阶段 4 `ACCEPTED（S1 固定 epoch 契约）`；未进入阶段 5。**该验收结论随后被严格审查推翻，见下一行。** |
| 2026-11-01 | 严格审查判定阶段 4 `NOT ACCEPTED`，提出 F1–F5：缓存元数据别名污染、SQL guard 无条件接受任意 `ParseError`、cache hit `result_fetch_ms` 混入查找时间、hit 日志使用 source execution ID、缺少失败路径测试。已修复并补充回归测试；真实证据脚本重跑，q8/q7/q11 命中结果仍一致且零 dispatch。默认完整套件 546 passed/11 skipped，真实 2.0.6 完整套件 557 passed，单元 450 passed，真实集成 11 passed，compileall 0；阶段 4 改为 `PENDING RE-REVIEW`；未进入阶段 5 |
| 2026-11-01 | 修复第二轮严格审查 F6–F10：显式 `EXACT_SQL_NO_REWRITE` parser 策略；收益证据改为 30 次 direct + 50 次 service 并输出 mean/median/p50/p95/p99/variance/stdev/non-client overhead；新增空结果/NULL/重复时间戳/时间边界/NaN/Inf/顺序测试；adaptive repeat-cache 建议强制 occurrence >= 2；新增 `scripts/setup_iotdb206_env.sh` 从零复现 2.0.6 venv。默认完整套件 553 passed/11 skipped，真实 2.0.6 完整套件 564 passed，单元 457 passed，真实集成 11 passed，compileall 0；证据 JSON 落盘 `docs/evidence/repeat_cache_evidence_2026-11-01.json`；阶段 4 保持 `PENDING RE-REVIEW（F1–F10 fixes applied）`；未进入阶段 5 |
| 2026-11-01 | 修复第三轮严格审查 F11–F14：失败决策回滚 occurrence（锁持有到决策构造结束，并发失败也不污染后续首次成功）且 repeat-cache reason 不再声称首次为 Native；新增真实 v2/v3/v4/v5 旧库迁移测试；SQLite schema v6 新增 `execution_reports` 持久化报告级 fallback/warnings/final status/total/end-to-end；doctor 只对安全无引号树路径子集构造 EXPLAIN。默认完整套件 563 passed/11 skipped，真实 2.0.6 完整套件 574 passed，单元 467 passed，真实集成 11 passed，compileall 0；阶段 4 保持 `PENDING RE-REVIEW（F1–F14 fixes applied）`；未进入阶段 5 |
| 2026-11-02 | 修复严格复核 F15（文档一致性）：统一 `end_to_end_ms` 为“入口到 attempt 持久化，已写入 schema v6 `execution_reports`，不含 report 行写入/序列化”；同步 METRICS_DICTIONARY、README、STRATEGY_CATALOG；精确化模型注释；统一最近一次测试证据耗时并注明需按需运行 `scripts/setup_iotdb206_env.sh`。默认完整套件 563 passed/11 skipped，真实 2.0.6 完整套件 574 passed，单元 467 passed，真实集成 11 passed，compileall 0；阶段 4 仍为 `PENDING RE-REVIEW`，未进入阶段 5 |
| 2026-11-02 | 环境复核与文档收口：确认 Conda `MANTO`（Python 3.12.13）安装 `apache-iotdb 2.0.6`，`doctor` 连接 server 2.0.6（build `c6ecbde`）；重新执行默认 563 passed/11 skipped、单元 467 passed、真实集成 11 passed、完整 574 passed、compileall 0；README/STATUS/ENVIRONMENT_REPORT/STRATEGY_CATALOG 统一为当前 Conda 命令，澄清 `.venv-iotdb206/` 是可选项目 venv、`.env` 是本地凭据文件。阶段 4 保持 `PENDING RE-REVIEW（F1–F15 fixes applied）`，未进入阶段 5 |
| 2026-11-02 | 本轮严格复审 F-1/F-2/F-3 最小修复：新增版本化 `configs/resource_policy.yaml` 与 `decision/resource_policy.py`，对 source/valid/fresh 的 client_host/db_host/db_metrics CPU/内存/磁盘百分比执行阈值闸门，rule_based/adaptive 越限强制 S0；无效 DB 快照带值改为 `unavailable_copy()` 隔离；验收表补资源阈值行。定向 52 passed，默认完整套件 579 passed/11 skipped，单元 483 passed，真实集成 11 passed，启用真实 2.0.6 完整套件 590 passed，compileall 0；阶段 4 保持 `PENDING RE-REVIEW（前序修复 + R1/R2 修复已应用）`，未进入阶段 5 |

| 2026-11-02 | R1/R2 最小修复：各来源独立隔离，不因客户端 invalid/stale/future 丢弃有效 DB 越限状态；新鲜度增加时间下界，未来快照标记 FUTURE_SNAPSHOT。新增 19 个回归用例；定向 90 passed，单元 502 passed，默认 598 passed/11 skipped，真实完整 609 passed；状态 PENDING RE-REVIEW，未进入阶段 5 |
