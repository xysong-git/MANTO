# 策略目录、阶段 0 审计与阶段 4 实现状态

**当前说明：** 以下阶段 4 状态与测试属于历史记录。用户已授权阶段 5，
当前实现及待复审状态见 [IMPLEMENTATION_STATUS.md](IMPLEMENTATION_STATUS.md)，
策略认证范围保持不变，阶段 5 增量说明见文末。

R1/R2 后续修复已应用：资源状态按来源独立隔离，未来时间不作为新鲜状态。
阶段保持 PENDING RE-REVIEW；本次不改变策略认证范围，真实 DB 资源验证仍 BLOCKED。

- **阶段：** 0 审计，2026-11-01 补入阶段 1 的只读配置入口探测；阶段 4 补入
  Registry/Validator/Decision/Executor 实现状态、外部认证目录和 **S1 认证**；
- **审计日期：** 2026-11-01；2026-11-02 环境复核更新
- **结论：** `S1_REPEAT_CACHE` 在**固定、调用方声明的不可变 dataset epoch**
  契约下完成五项证明；**`BLOCKED: NO_VERIFIED_NON_NATIVE_STRATEGY` 已解除**。
  严格审查先后提出 F1–F15；本轮复审又提出 F-1/F-2/F-3。前序和本轮修复及
  回归测试均已完成，当前状态为
  **`PENDING RE-REVIEW（前序修复 + R1/R2 修复已应用）`**，本目录
  不自行宣布重新通过。F15 是文档/环境一致性修复；本轮 F-1/F-2/F-3 是资源阈值、
  无效 DB 快照隔离和验收文档修复，不改变 S1 的技术结论。
- **已认证策略：** `S0_NATIVE`（基线/回退）和 `S1_REPEAT_CACHE`（非 Native，
  进程内精确 SQL 全结果缓存）。默认 Registry 为
  `("S0_NATIVE", "S1_REPEAT_CACHE")`；S1 只有在调用方提供 `cache_epoch` 时才
  适用。
- **阶段 4 实现：** `S0_NATIVE` 和 `S1_REPEAT_CACHE` 已进入真实的
  `StrategyRegistry`、Validator、Decision Engine、单候选 Executor 和 SQLite
  schema v6 持久化路径；所有 `CAND_*` 仍未启用。
- **外部认证目录：** `configs/strategies.yaml`（schema 1）是 Registry 的外部
  认证依据；默认 Registry 必须加载并通过目录覆盖检查，否则启动失败。
- **审查修复（2026-11-01）：** F1 `ColumnMeta` 冻结且缓存写入复制元数据；
  F2 只对 workload 已验证的两个 SQLGlot parse gap 做 parse-only 归一化，其他
  `ParseError` fail-closed；F3 hit 的 `result_fetch_ms=0.0`，查找耗时单独记录；
  F4 hit 日志使用本次 hit 的 `execution_id`；F5 增加对应失败路径测试。
- **第二轮审查修复 F6–F10（2026-11-01）：** F6 把 non-baseline exact-SQL 候选
  的 parser 范围改为显式 `EXACT_SQL_NO_REWRITE`：没有 SQL 改写，就不要求
  Agent-1 parser 覆盖，server 仍是执行语义的权威；F7 证据脚本改为 30 次
  Native/30 次 S1 direct、50 次 service，并报告 n、mean、median、p50/p95/p99、
  variance/stdev 和非客户端端到端开销；F8 增加空结果、NULL、重复时间戳、时间
  边界、NaN/Inf、顺序保持测试；F9 adaptive 在 occurrence < 2 时拒绝
  repeat-cache 建议并回到 Native；F10 新增
  `scripts/setup_iotdb206_env.sh`，从项目依赖从零重建并自检
  `apache-iotdb==2.0.6`，不再依赖临时 job venv 路径。修复后（F11–F14 之前）
  默认套件 553 passed/11 skipped，真实 2.0.6 完整套件 564 passed。
- **第三轮审查修复 F11–F14（2026-11-01）：** F11 occurrence 只在决策成功返回后
  提交，失败决策回滚，且 occurrence 锁持有到决策构造结束，并发失败不会把后续
  首个成功决策标成重复；repeat-cache reason 不再无条件声称首次为 Native；F12
  增加真实 v2/v3/v4/v5 旧库迁移测试；F13 schema v6 新增 `execution_reports`
  持久化 fallback trigger/reason/blocked reason、final status、total/end-to-end
  和 warnings；F14 doctor 只对安全无引号树路径子集拼接 EXPLAIN，非法路径跳过
  探测并告警。修复后默认套件 **563 passed/11 skipped**，真实 2.0.6 完整套件
  **574 passed**，单元 **467 passed**，真实集成 **11 passed**，`compileall` 0。
- **F15 文档/环境一致性（2026-11-02）：** 统一 `end_to_end_ms` 口径；确认
  当前 Conda `MANTO` 已安装 `apache-iotdb==2.0.6`，`doctor` 实测 server
  2.0.6 / client 2.0.6；README、STATUS、ENVIRONMENT_REPORT 和本目录的测试/
  证据命令改为当前 Conda 直接运行，并说明 `.venv-iotdb206/` 是可选项目
  venv、`.env` 是独立的本地连接配置/凭据文件。
- **本轮严格复审 F-1/F-2/F-3（2026-11-02）：** 新增版本化
  `configs/resource_policy.yaml` 和 `decision/resource_policy.py`；越限资源
  状态强制 S0，无效 DB 快照带值被隔离，验收表补资源阈值行。这不改变任何策略
  的认证状态，也不把 S1 变成资源感知策略；S1 仍只在固定 epoch + exact SQL
  occurrence >= 2 时适用。修复后定向 52 passed，默认套件 **579 passed/11
  skipped**，真实 2.0.6 完整套件 **590 passed**，单元 **483 passed**，真实
  集成 **11 passed**，`compileall` 0。
- **重要：** 本文件仍是审计目录。S1 不是 SQL 改写，也不声称通用新鲜度：它把
  调用方声明的 epoch 当作正确性边界；没有 server 端失效/数据版本 API。未认证
  策略不允许通过配置、LLM 或测试双桩进入生产 Registry；测试中的策略双桩有明确
  `TEST_ONLY` 标记，只用于验证框架路径。

## 1. 状态定义

| 主状态 | 判定标准 | 是否可进入 Registry |
|---|---|---|
| `VERIFIED` | 入口证明、正确性证明、生效证明、收益证明、回退证明全部通过，且有可复现测试 | 可以（仍需代码实现和认证标记） |
| `NOT_VERIFIED` | 入口存在且可执行，但五项证明至少缺一项；或入口尚未实际探测，不能假设存在 | 不可以 |
| `UNSUPPORTED` | server/client 拒绝，当前部署没有入口，或重写被证明不等价/静默错误 | 不可以 |

**每条策略的主状态只使用上述三种。** 需要额外说明时使用独立子标记，不把
`UNKNOWN`、`INVALID` 当作第四种主状态：

- 未探测的入口：`NOT_VERIFIED` + 子标记 `UNKNOWN`；
- 静默空结果、错误结果或不等价重写：`UNSUPPORTED` + 子标记 `INVALID` 或
  `NOT_EQUIVALENT`；
- 当前部署没有用户数据入口：`UNSUPPORTED` + 子标记 `NO_USER_TABLE`；
- 只读认证通过干净数据、但部分异常数据未测：`NOT_VERIFIED` + 子标记
  `EDGE_CASES_PARTIAL`；LAST 的授权边缘子项已完成补测，不再使用
  `EDGE_CASES_UNTESTED`；
- 没有代表性 workload 需求：`NOT_VERIFIED` + 子标记 `DEMAND_UNPROVEN`；
- 回退只在注入式契约层验证，没有生产 Executor：`NOT_VERIFIED` + 子标记
  `FALLBACK_CONTRACT_ONLY`。

“有收益”不等于 `VERIFIED`。LAST 候选已完成只读差分、时间边界、计划、收益、
回退契约，以及用户授权专用库中的 NULL/缺失、重复时间戳、乱序、缺口和空序列
测试；真实 workload 需求和生产 Executor 回退仍未验证，因此仍是
`NOT_VERIFIED`。详见 [`LAST_CERTIFICATION.md`](LAST_CERTIFICATION.md)。
S1 则是执行层策略，不是 SQL 改写；它的正确性只在调用方声明的不可变 epoch 内
成立，超出该边界时不得把缓存结果当作 fresh result。

**策略 ID 说明：** 阶段 0 不预设 `S1`/`S2` 的真实含义。所有未认证候选一律
使用 `CAND_*` 临时 ID；只有完成五项证明并进入 Registry 时，才分配正式策略
ID。`CAND_*` 不得用于经验记录中的稳定策略标识。

## 2. 策略总表

| 策略 ID | 动作 | 真实入口 | 正确性 | 生效证据 | 收益 | 回退 | 状态 |
|---|---|---|---|---|---|---|---|
| `S0_NATIVE` | 原 SQL 执行 | `Session.execute_query_statement` | Native 定义；只读 SELECT | 原始计划 | 基线，不声称优化 | 自身 | `VERIFIED`（已实现） |
| `S1_REPEAT_CACHE` | 进程内精确 SQL 全结果缓存 | 真实 Decision→Executor→Adapter；命中不 dispatch | 固定 epoch 下与 Native 列/行/值一致（q8/q7/q11），并覆盖空结果/NULL/重复时间戳/时间边界/NaN/Inf/顺序；epoch 外不保证 fresh | 命中 0 次 server dispatch；miss 1 次 | 命中中位 0.177/0.141/0.228 ms vs Native 20.824/15.090/25.867 ms；q8 服务端到端中位 45.367 vs 67.609 ms（1.49×，p99 略差） | 预 dispatch 缓存故障后真实 Native 成功；无 epoch 不选中 | `VERIFIED`（已实现，固定 epoch 契约；阶段 4 待复审） |
| `CAND_LAST_SINGLE_SERIES` | `SELECT ... ORDER BY time DESC LIMIT 1` → `SELECT LAST ...` | SQL 改写，server 接受 | **通过（授权范围）**：六类型 1,200 采样 + 8 边界 + 79 边缘差分通过；2026-11-01 变体复核覆盖别名/OFFSET/wildcard/无 ORDER BY；TTL/重启/并发/NaN 未测 | `LastQuery` → `LastQueryScan` | 两次 120 块复测六类型均值低约 11.7–42.6%，95% CI 不跨 0（受控热缓存） | 注入式契约通过；无生产 Executor | `NOT_VERIFIED`（子标记 `DEMAND_UNPROVEN`、`FALLBACK_CONTRACT_ONLY`） |
| `CAND_MAXTIME_LASTVALUE` | Native 最新值 → `MAX_TIME + LAST_VALUE` | SQL 改写，server 接受 | 值精确但列名/Time 语义不同 | `AlignedSeriesAggregationScan` | 单次计时有快有慢，无稳定收益 | 可回退；未实现 | `NOT_VERIFIED` |
| `CAND_ALIGN_BY_DEVICE` | 多设备查询加 `ALIGN BY DEVICE` | SQL 改写，server 接受 | **形状不等价** | `FullOuterTimeJoin` → `DeviceView` | 本次探测更慢 | 可回退；未实现 | `NOT_VERIFIED` |
| `CAND_FETCH_SIZE` | 调整 `Session(fetch_size=...)` | 客户端连接选项 | 行数相同（已测场景） | 无 server 计划变化；传输批次变化 | 均值差异约 3.6%，方差重叠 | 可回退；未实现 | `NOT_VERIFIED` |
| `CAND_PREDICATE_REWRITE` | 去重/等价边界改写 | SQL 改写 | 已测 case 结果相同 | 计划文本相同 | 无收益 | 可回退；未实现 | `NOT_VERIFIED` |
| `CAND_SERIES_PATH` | device path → series path | SQL 改写 | **错误/空结果** | 0 计划行 | 表面更快，实际无结果 | 禁止 | `UNSUPPORTED`（子标记 `INVALID`：静默空） |
| `CAND_UNION_MERGE` | UNION → 单区间 | SQL 改写 | **不等价**；UNION 被 server 拒绝 | 无法生成 UNION 计划 | 不适用 | 禁止 | `UNSUPPORTED`（子标记 `NOT_EQUIVALENT`） |
| `CAND_PARALLEL_SPLIT` | 客户端按 device 拆成多查询再合并 | 外部执行策略 | 已测行数一致 | 多查询 + 客户端 merge | 2/10 设备都更慢 | 可回退；未实现 | `NOT_VERIFIED` |
| `CAND_DB_PARAMETER` | server 参数/配置调优 | **部分探测**：`SHOW CONFIG`/`SHOW CONFIGURATION` 被拒绝；`SHOW VARIABLES` 只有 5 个集群变量 | 未知 | 未知 | 未知 | 未知 | `NOT_VERIFIED`（子标记 `UNKNOWN`：无查询级调优入口） |
| `CAND_TABLE_MODEL` | 使用表模型/表会话 | 表模型 adapter | **当前无用户表** | 仅 `information_schema` | 不适用 | 不适用 | `UNSUPPORTED`（子标记 `NO_USER_TABLE`） |
| `CAND_CLIENT_CACHE` | 客户端缓存结果 | 未审计 | 一致性/新鲜度未定义 | 无 | 未知 | 未实现 | `NOT_VERIFIED`（子标记 `UNKNOWN`：未审计） |

**统计：** 2 个 `VERIFIED`（`S0_NATIVE` 基线 + `S1_REPEAT_CACHE` 非 Native），
其中非 Native `VERIFIED` 策略数量为 1。默认生产 Registry 的可选策略集合是
`("S0_NATIVE", "S1_REPEAT_CACHE")`，但 S1 只有在 `cache_epoch` 已声明且 SQL
位于确定性 token 子集时才适用。优化闭环在固定 epoch 契约下成立；没有通用
freshness、跨进程或服务端失效保证。

## 3. S0_NATIVE（已实现的基线/回退）

```yaml
strategy_id: S0_NATIVE
name: Native SQL baseline and fallback
database_model: tree
compatible_server_versions: ["2.0.6"]
enabled: true
implementation_class: tsdb_optimizer.strategies.native.NativeStrategy
applicability_rules:
  - single, read-only SELECT accepted by the read-only guard
  - no rewrite, no extra execution
execution_control: execute original SQL once via tree Session
semantic_test_suite: tests/unit/test_native_strategy.py
plan_effect_expectation: null
known_limitations:
  - no optimization
  - client fetch_mode is not varied by this strategy
  - only tested with apache-iotdb 2.0.6
version: 0.1.0
```

### 3.1 入口证明

`Session.execute_query_statement(sql)` 已实际执行单条只读 SELECT，完整读取
结果集并在 `finally` 中 `close_operation_handle()`；`Session.close()` 成功。
树模型方言已确认；`SELECT 1` 不是合法健康检查，应使用 `SHOW VERSION` 或
带 `FROM` 的查询。

### 3.2 正确性定义

S0 不改变 SQL，因此“正确性”是：执行原始只读 SQL，返回 server 原始列和行；
不比较、不归一化、不重排。它是所有候选策略的语义基准和最终 fallback。

### 3.3 生效与收益

无改写，计划即原始计划；不声称性能收益。S0 是基线，不是优化策略。

### 3.4 回退证明

S0 是所有候选策略的回退目标。阶段 4 已实现保守回退策略：

- 候选在 dispatch 前验证失败 → 允许回退，因为第一次请求未发出；
- 第一次请求失败且 Adapter 明确报告 `request_dispatched=false` → 允许回退；
- 第一次请求明确 `retry_safe=true` 且已完成 → 允许回退；
- 超时或完成状态未知 → **禁止回退**，避免重复执行；
- S0 自身失败 → 不重试自身。

当前 IoTDB Adapter 不把任何异常标记为 retry-safe，因此真实超时/连接中断
路径不会触发自动重试。阶段 4 的 S1 认证已用真实 Executor/Adapter/Session 和
受控的预 dispatch 缓存查找故障证明：S1 未发送请求时，真实 Native 可以成功
回退且只执行一次。该证据不表示自然 server 故障会自动回退；自然超时/完成未知
仍禁止回退。单元测试中的可编程 Adapter 双桩继续覆盖其余 fallback 条件。

### 3.5 认证状态

**`VERIFIED`（能力层 + 代码实现）**：入口、语义定义、server 行为、原 SQL
不变、单次执行、只读守卫和保守回退框架均已测试。真实 IoTDB 集成测试已验证
决策、执行和持久化链路。

**但这不表示其他策略自动成立**：S0 只做基线和 fallback，不声称性能收益。
默认 Registry 现在包含 `S1_REPEAT_CACHE`，但 S1 的适用性、正确性和收益都受
固定 epoch 契约限制；LAST 等 `CAND_*` 仍未认证。

## S1_REPEAT_CACHE（正式非 Native 策略）

### 概述与适用边界

`S1_REPEAT_CACHE` 是一个**执行层**策略，不是 SQL 改写，也不是数据库服务端
优化。它把“同一条 exact cleaned SQL、同一个调用方声明的 dataset epoch”的完整
结果缓存在进程内；缓存命中时 Adapter 直接返回已捕获的完整结果，不向 IoTDB
发送请求。

- 缓存键：`cache_epoch + exact cleaned SQL SHA-256 + strategy_id`；
- 缓存只在调用方提供 `cache_epoch` 时启用；没有 epoch 时 S1 不适用；
- S1 只在 exact SQL occurrence >= 2 时适用；occurrence 只对成功决策计数，
  失败决策不消耗；默认 Registry 下第一条成功的 exact SQL 走 Native，第二条
  可选择 S1（miss，执行一次并填充缓存），第三条及以后才可能命中；
- 只缓存完整结果；超过行数或字节上限的结果不缓存、不截断；
- SQL 必须落在保守的确定性 token 子集内；未知函数、字符串、占位符、JOIN、
  UNION、CAST 等一律拒绝 S1，但原 SQL 仍可作为 Native 执行；
- **exact-SQL parser 策略（`EXACT_SQL_NO_REWRITE`）：** S1 不解析、不改写
  SQL；只要 exact cleaned SQL 通过上述确定性子集守卫就适用，即使 Agent-1
  将其标为 `UNSUPPORTED`。中心 Validator 对这类候选显式返回 PASSED，并记录
  `EXACT_SQL_NO_REWRITE` 理由：没有改写需要 parser 证明，server 仍是执行语义
  的权威。该策略只适用于“候选 SQL = 原始 cleaned SQL”的 non-baseline 策略；
  它不是绕过 parser 的自动改写，也不改变其他策略的 fail-closed 规则。对应回归
  测试为 `tests/unit/test_strategy_validator.py` 的 parser-unsupported 和
  无 Agent-1 analysis 用例；
- 没有 server 端缓存失效或数据版本 API；调用方必须保证 epoch 内数据不变。

**这不是通用结果缓存。** 如果数据在同一个 epoch 内被外部写入、TTL 删除、
schema 变化或重启改变，缓存可能返回 stale 结果；策略不声称能检测这些情况。
需要 fresh 语义的调用方必须更换 epoch 或禁用 S1。

<a id="s1-repeat-cache-入口证明"></a>
### 入口证明

真实入口链路已经存在并可执行：

```text
CLI/service
  -> DecisionExecutionService
  -> DecisionEngine（occurrence >= 2 时选择 S1）
  -> StrategyExecutor（插件 + 中心 Validator）
  -> IoTDBTreeAdapter.execute_readonly(... strategy_id="S1_REPEAT_CACHE",
                                          execution_options={"cache_epoch": ...})
  -> ResultCache.get / ResultCache.put
```

- 插件：`src/tsdb_optimizer/strategies/repeat_cache.py:RepeatCacheStrategy`；
- 缓存：`src/tsdb_optimizer/adapters/iotdb/result_cache.py:ResultCache`；
- Adapter 接入：`src/tsdb_optimizer/adapters/iotdb/tree_adapter.py:execute_readonly`；
- 外部目录：`configs/strategies.yaml` 的 `S1_REPEAT_CACHE` 条目；
- 真实集成测试：`tests/integration/test_repeat_cache_certification.py`。

真实 2.0.6 服务路径（q8，三次 `DecisionExecutionService.run()`）的实际选择：

```text
run 1: selected=S0_NATIVE       occurrence=1  dispatch=true
run 2: selected=S1_REPEAT_CACHE occurrence=2  dispatch=true   (miss, stored)
run 3: selected=S1_REPEAT_CACHE occurrence=3  dispatch=false  (hit)
```

同一测试用真实 `Session` 包装器计数：三次服务调用只产生 **2 次**真实 server
请求；第三次命中没有新增请求。正常路径没有调用 `EXPLAIN`。CLI `optimize` 也有
`--cache-epoch` 参数，但**单次 CLI 进程只执行一条 SQL**，通常只能看到
occurrence 1 的 Native；要观察 miss/hit，必须在同一进程内重复调用 service，
或使用未来的批量 workload 入口。这是当前使用边界，不是伪造证据。

<a id="s1-repeat-cache-正确性"></a>
### 正确性

S1 的正确性定义是：**在同一个调用方声明的不可变 dataset epoch 内，缓存命中
返回的完整结果必须与真实 Native 执行完全一致。** 它不是“任意时刻都等价于一次
新的数据库查询”。

真实 2.0.6、只读、静态数据集上的差分（`preview_rows=1000`，覆盖全部结果）：

| workload | 真实行数 | 列数 | Native vs miss vs hit |
|---|---:|---:|---|
| `query/queries_dbs/q8.sql` 第 1 条 | 200 | 4 | 列名/类型、行数、全部行一致 |
| `query/queries_dbs/q7.sql` 第 1 条 | 303 | 5 | 列名/类型、行数、全部行一致 |
| `query/queries_dbs/q11.sql` 第 1 条 | 605 | 5 | 列名/类型、行数、全部行一致 |

测试 `test_real_native_and_cache_results_are_identical` 对三种 workload 参数化
断言 `rows_preview`、`row_count`、列名和列类型完全相同；`preview_truncated`
为 `false`。`test_real_service_selects_native_then_cache_and_persists_provenance`
进一步断言 SQLite 中的 hit 结果与 miss 结果的列和行一致。

SQL 子集测试覆盖 q8（LAST + wildcard）、q7（interval `GROUP BY` + count）、
q9（Native DESC + LIMIT）以及负例：`now()`、`rand()`、`uuid()`、字符串、
占位符、JOIN、UNION、CAST、`last_value`、DDL、多语句。负例被拒绝为
non-cacheable，不会被猜测或改写。

**正确性边界类别（F8）：** S1 只存储并返回 Adapter 已捕获的完整结果，不做
算术、排序、类型转换或 NULL 替换；因此缓存层不能引入浮点误差、改变 NULL、
重排时间戳或丢掉空结果。对应测试同时覆盖 `ResultCache` 存储层和 Adapter
hit 路径：

| 边界类别 | 测试 | 断言 |
|---|---|---|
| 空结果 | `test_cache_preserves_empty_result`、`test_cache_hit_preserves_empty_result` | 0 行、空 preview、列元数据不变，hit 仍为 0 dispatch |
| NULL | `test_cache_preserves_boundary_values_duplicate_timestamps_and_order`、`test_cache_hit_preserves_boundary_values_and_duplicate_timestamps` | NULL 原样保留，不被 0/空串替代 |
| 重复时间戳 | 同上 | 重复 timestamp 逐行保留，行序不变 |
| 时间边界 | 同上 | `-(2**63)`、`2**63-1` 原样返回 |
| 浮点特殊值 | 同上 | NaN 保持 NaN，+Inf 保持 +Inf，0.0 不丢失 |
| 顺序 | 同上 | hit 行序与首次真实结果一致；无 `ORDER BY` 时只承诺同一结果的重放，不额外声明语义排序 |
| 数值容差 | 缓存不做数值运算 | 命中值与源结果逐值相等（NaN 用 NaN 感知比较） |

真实 2.0.6 差分证明 q8/q7/q11 的全量预览与 Native 一致；上述测试补齐真实
workload 未自然覆盖的空/NULL/重复时间戳/边界/NaN/Inf/顺序类别。

**正确性边界：** epoch 必须由调用方维护为不可变；S1 没有 server 端失效协议，
不能证明外部写入、TTL、重启或 schema 变化后的新鲜度。该限制是策略契约的一部分，
不是可以忽略的实现细节。

<a id="s1-repeat-cache-生效证明"></a>
### 生效证明

真实 2.0.6 上的可观测差异：

- miss：`served_from_cache=false`、`request_dispatched=true`、
  `request_completed=true`，并带 `CACHE_MISS_STORED`；
- hit：`served_from_cache=true`、`request_dispatched=false`、
  `request_completed=false`、`retry_safe=false`、`execute_call_ms=0.0`，
  `cache_source_execution_id` 指向填充缓存的真实 miss execution；
- 命中返回完整行数和预览，`preview_truncated` 按真实行数/预览长度计算；
- 真实 `Session` 计数证明 hit 没有 server 请求；
- SQLite schema v6 持久化 `served_from_cache`、`cache_source_execution_id`、
  `cache_epoch`，并通过 `execution_reports` 持久化报告级 fallback/warnings 和
  end-to-end；缓存命中行被排除在历史延迟样本之外。

失败和边界路径同样有测试：缓存查找异常在 dispatch 前返回结构化 `ERROR`
（`request_dispatched=false`）；缓存写入失败只追加 `CACHE_STORE_FAILED`，
不把已成功的数据库结果改成失败；超过缓存上限的结果只记录
`CACHE_NOT_STORED`，不缓存部分结果。

<a id="s1-repeat-cache-收益证明"></a>
### 收益证明

**环境：** Apache IoTDB 2.0.6，树模型，客户端 `apache-iotdb==2.0.6`，同主机
`127.0.0.1`，静态热数据，单进程，无并发/背景负载控制。**日期：** 2026-11-01。
**可复现环境与命令：** 当前 Conda `MANTO` 已确认 `apache-iotdb==2.0.6`；
`.venv-iotdb206/` 是可选的隔离 venv，不是 `.env`。

```bash
cd /opt/program-code/MANTO
PYTHONPATH=src python scripts/repeat_cache_evidence.py \
  > docs/evidence/repeat_cache_evidence_2026-11-01.json
```

若使用可选的 `.venv-iotdb206/`，先运行 `scripts/setup_iotdb206_env.sh`，再把
命令中的 `python` 换成 `.venv-iotdb206/bin/python`。

原始 JSON 已保存为
[`docs/evidence/repeat_cache_evidence_2026-11-01.json`](evidence/repeat_cache_evidence_2026-11-01.json)，
包含每次运行的原始值；脚本拒绝非 2.0.6 或非 tree server，且只执行只读 SELECT。

**直接 Adapter 证据：** 每种 workload 30 次 Native + 30 次 S1（第 1 次 miss，
后 29 次 hit）；完整消费结果，`preview_rows=1000`。统计使用线性插值分位数，
单位为 ms：

| workload | 行数 | Native n | Native mean | Native median | Native p95 | Native p99 | Native variance | Native stdev |
|---|---:|---:|---:|---:|---:|---:|---:|---:|
| q8 | 200 | 30 | 20.766 | 20.824 | 24.602 | 24.973 | 7.890 | 2.809 |
| q7 | 303 | 30 | 16.226 | 15.090 | 16.225 | 39.374 | 38.172 | 6.178 |
| q11 | 605 | 30 | 26.489 | 25.867 | 28.908 | 31.688 | 2.439 | 1.562 |

| workload | S1 hit n | hit mean | hit median | hit p95 | hit p99 | hit variance | hit stdev | miss（n=1） | median 加速 |
|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|
| q8 | 29 | 0.183 | 0.177 | 0.233 | 0.379 | 0.003 | 0.057 | 26.419 | 117.65× |
| q7 | 29 | 0.143 | 0.141 | 0.153 | 0.158 | 0.000 | 0.005 | 17.038 | 107.02× |
| q11 | 29 | 2.917 | 0.228 | 0.375 | 55.883 | 205.595 | 14.339 | 30.719 | 113.45× |

- Native 30 次均 `request_dispatched=true`；S1 30 次总计只有 1 次 server
  dispatch（第一次 miss），后 29 次 hit 的 `execute_call_ms=0.0`、
  `result_fetch_ms=0.0`；
- q8/q7 的 hit 分布很窄；q11 的 hit 中位为 0.228 ms，但有一个 77.469 ms
  离群值，使 mean/p99/variance 明显变大。这里同时报告 median 和尾部统计，
  不把 q11 的均值当作典型命中延迟；
- miss 比 Native 略慢（本样本 +12%～+27%，每个 workload 只有一个 miss 样本，
  不是稳定估计），因为要捕获完整结果并写入缓存；收益从第二次 exact SQL
  重复开始；
- hit 结果与 Native 完全一致（见正确性）。hit 的 `client_wall_ms` 仍覆盖
  Adapter 内部的缓存查找，cache lookup 时间另见 `CACHE_HIT` warning /
  `cache_hit` 日志的 `cache_lookup_ms`。

**端到端服务证据（q8，50 次 `DecisionExecutionService.run()`）：**

| 路径 | runs | 真实 server dispatch | hit 数 | mean e2e | median e2e | p95 | p99 | variance | 中位非客户端开销 | 中位决策开销 | 中位 hit client_wall |
|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|
| 启用 S1（声明 epoch） | 50 | 2 | 48 | 47.832 | 45.367 | 64.941 | 84.535 | 86.569 | 45.166 | 15.876 | 0.148 |
| Native-only（无 epoch） | 50 | 50 | 0 | 66.858 | 67.609 | 71.098 | 83.154 | 29.067 | 43.821 | 11.785 | — |

- 端到端中位 **45.367 vs 67.609 ms（1.49×）**，均值 47.832 vs 66.858 ms
  （1.40×），p95 64.941 vs 71.098 ms（1.09×）；p99 84.535 vs 83.154 ms，
  缓存路径反而略差约 1.7%。因此这里只声称中位/均值改善，不声称尾延迟优势；
- 缓存路径的 hit end-to-end（n=48）中位 45.230 ms、mean 46.361 ms、
  p95 53.876 ms、p99 65.900 ms、variance 29.680；其 `client_wall_ms` 中位
  仅 0.148 ms。非客户端开销（`end_to_end_ms - client_wall_ms`，包含决策、
  验证、SQLite 审计/持久化、日志和 harness）中位 45.053 ms，占 hit 端到端
  的 99% 以上；决策开销中位 15.876 ms，比 Native-only 的 11.785 ms 高约
  4.1 ms。不能把约 0.15 ms 的 Adapter 命中时间宣称为整个服务的端到端延迟；
- 该表同时报告 mean、median、p95/p99 和 variance 的原始输入；q8 的
  workload 是 2,000 条完全相同语句；q7/q11 各有 99 条 unique SQL、每条约
  重复 20 次，均满足“先 Native、再 miss、之后 hit”的重复模式。

**限制：** 这是受控热缓存微基准，不是生产 benchmark；没有冷缓存、多进程、
并发、多日、背景负载或 DB 主机指标。进程退出后缓存丢失。服务端到端仍受
决策和 SQLite 持久化开销支配；p99 不优于 Native-only，不能外推为尾延迟保证。

<a id="s1-repeat-cache-回退证明"></a>
### 回退证明

回退政策没有放宽：只有**第一次请求尚未 dispatch**、明确未发送或明确完成且
`retry_safe=true` 时才回退；超时/完成未知禁止回退。

真实生产路径的受控故障注入证据
（`test_real_native_fallback_after_pre_dispatch_cache_failure`）：

1. 使用真实 `IoTDBTreeAdapter`、真实 `Session`、真实 `DecisionExecutionService`
   和真实 IoTDB 2.0.6；
2. 注入 `_FailingLookupCache.get()`，使 S1 在任何 server 请求之前抛出缓存查找
   异常；
3. 第一次 run 选择 S0，真实执行 1 次；
4. 第二次 run 选择 S1；S1 attempt 返回结构化 `ERROR`，
   `request_dispatched=false`，warning 为 `CACHE_LOOKUP_FAILED`；
5. 真实 Executor 判断可以安全回退，执行真实 S0 Native，成功且只向 server
   发送 1 次请求；`fallback_used=true`，
   `fallback_trigger=FIRST_REQUEST_NOT_DISPATCHED`；
6. 两次 run 的真实 server 请求总数恰好为 2，SQL 相同；SQLite 记录为
   `[S1_REPEAT_CACHE, S0_NATIVE]`，第一行 dispatch=0，第二行 dispatch=1。

这证明了**生产 Executor/Adapter 路径上的真实 Native fallback**，但故障点是
受控注入的缓存查找失败，不是自然发生的 IoTDB 故障；不能据此声称真实 server
超时/连接中断已经在本项目中被观察到自动回退。无 epoch 的服务路径也有测试：
两次 run 都选择 S0，产生两次真实 dispatch，缓存保持为空。

### 已知限制与认证边界

- 进程内、单进程；多进程/多实例不共享；
- 正确性依赖调用方声明的不可变 epoch；没有 server 端失效/数据版本 API；
- 默认 Registry 下第一条成功的 exact SQL 仍为 Native；第二条才可能填充缓存；
  失败决策不消耗 occurrence；
- 缓存只保存完整结果；超大结果不缓存；
- 没有冷缓存、并发、多日、背景负载或 DB 主机指标；
- CLI `optimize` 单进程单条 SQL 通常无法看到 hit；长驻 service 或未来批量入口
  才能体现收益；
- 这不是服务端查询计划优化；EXPLAIN 不会有 S1 节点；
- 阶段 4 之后仍不进入阶段 5。

## 4. CAND_LAST_SINGLE_SERIES

### 4.1 入口与适用条件

- 原始模式：`SELECT <single_series> FROM <single_device> [WHERE time ...]
  ORDER BY time DESC LIMIT 1`
- 候选改写：`SELECT LAST <single_series> FROM <single_device> [WHERE time ...]`
- server 2.0.6 接受；EXPLAIN 为 `LastQuery-1` → `LastQueryScan-3`。
- 仅允许单序列、单设备、仅时间过滤、无值过滤；必须正好是
  `ORDER BY time DESC LIMIT 1`；FLOAT 需要容差；多序列/多设备/其他
  ORDER BY/LIMIT/OFFSET/GROUP BY/别名/wildcard 一律不得自动改写。
- 这些适用条件目前是**认证规则**。通用 Registry/Validator/Executor 已在阶段 4
  实现，但 LAST 的专用插件、结果归一化和认证规则尚未实现，因此 LAST 没有
  可启用的生产实现。

### 4.2 正确性证明（干净数据 + 授权边缘数据）

| 检查 | 结果 |
|---|---|
| 单序列、仅时间过滤的时间戳 | 与 Native 一致 |
| INT32 / INT64 / BOOLEAN / TEXT | 实测值一致 |
| FLOAT | LAST `34579.86` vs Native `34579.859375`，在 1e-6 容差内通过 |
| DOUBLE | 200 个时间采样通过；12 位相对/绝对容差 |
| 时间/值扫描 | 六类型各 200 个 `WHERE time <= t`，合计 1,200 对，0 失败 |
| 时间边界 | 8 个 case（min/max/非时间点/单点区间/空区间）全部通过 |
| 结果形状 | LAST 4 列 `Time, Timeseries, Value, DataType`；Native 2 列，必须归一化 |
| 多序列 | LAST 每序列一行，Native 一行多列；不等价，拒绝 |
| 多设备 | LAST 每设备一行，Native 一行多列；不等价，拒绝 |
| 值过滤 | `701: Only time filters are supported in LAST query`；拒绝 |
| 错误 device path | 两侧都空，但空结果不能证明合法；必须由 schema 校验拒绝 |
| `LIMIT 2` / `ORDER BY ASC` | 返回形状不同；拒绝 |
| 缺失测量值/空序列 | 32 + 3 个 case 通过；每个序列按自己的最后有效点返回 |
| 重复时间戳 | 3 个 case 通过；两次写入接受，`COUNT=1`，本次观测为 last-write-wins |
| 乱序写入 | 4 个 case 通过；写入被接受，查询按时间升序返回 |
| 时间缺口 | 9 个 case 通过；缺口、空区间和范围过滤与 Native 一致 |
| 显式 SQL NULL | 4 个 case 通过；NULL 不产生该序列的数据点，两侧都为空 |
| all-None API | 2.0.6 客户端抛 `ValueError`；未写入，adapter 必须显式处理 |
| TTL / 重启 / 并发 / NaN | **未测试**，不能假设等价 |
| 结果归一化 | 认证脚本已证明；项目代码尚未实现 |

因此，可以在**干净单序列数据和本轮授权边缘数据**上证明归一化后的值一致；
不能把它当作所有数据、所有生命周期状态上的 Native 等价重写。完整数据见
[`LAST_CERTIFICATION.md`](LAST_CERTIFICATION.md)。

### 4.3 生效证明（通过）

同一 FLOAT 查询的 EXPLAIN 从：

```text
AlignedSeriesScan-1
Limit: 1
ScanOrder: DESC
```

变为：

```text
LastQuery-1
  └─ LastQueryScan-3
     Aligned: true
```

说明不是只改 SQL 文本，而是进入独立的 LAST 查询计划。该证明只说明计划改变，
不说明结果在所有数据上等价。

### 4.4 收益证明（受控热缓存，已通过）

客户端 2.0.6，单机、热缓存、同主机、静态数据；初始运行 30 个配对块；
2026-11-01 扩展复测为每个类型每个 session 10 次 warmup、120 个配对块，并用
不同随机种子独立重复一次；两个 `Session` 交替使用；Native/LAST 正反交替并
在部分块随机打乱；完整读取结果集；20,000 次 bootstrap 计算均值差 95% CI。
单位 ms，扩展运行 `n=120`，两种写法错误数均为 0。

| 类型 | Native mean | LAST mean | 配对差 mean | 95% CI | 相对降低 |
|---|---:|---:|---:|---:|---:|
| FLOAT | 3.841 | 2.475 | 1.365 | [1.332, 1.402] | 35.456% |
| DOUBLE | 4.363 | 2.533 | 1.830 | [1.734, 1.897] | 41.922% |
| INT64 | 3.704 | 2.478 | 1.226 | [1.200, 1.253] | 33.049% |
| INT32 | 2.865 | 2.477 | 0.388 | [0.353, 0.416] | 13.463% |
| TEXT | 3.032 | 2.512 | 0.520 | [0.427, 0.576] | 17.105% |
| BOOLEAN | 2.825 | 2.491 | 0.334 | [0.305, 0.362] | 11.720% |

第二次独立 120 块复测六类型 CI 同样不跨 0（TEXT `[0.244, 0.691]`）；但本轮
30 块复核中 TEXT CI 为 `[-0.351, 0.733]`，说明单次小样本可能波动，不能只引用
一次 30 块结果。

- 降低主要来自 `execute_call_ms`；LAST 的 `result_fetch_ms` 反而略高（四列、
  需要归一化），净收益仍为正；
- 没有冷缓存、背景负载、并发、多进程/多日、DB 主机指标；
- 全量 `query/queries_dbs` 24,000 条语句中 **没有** 符合条件的 Native
  `ORDER BY time DESC LIMIT 1`；2,000 条 `SELECT LAST` 全部是 q8 的多序列/
  全设备查询，已经使用 LAST；
- 因此这些数字是受控实验收益，不是生产收益，也不是需求证明。

### 4.5 回退证明（认证脚本契约；LAST 生产插件未实现）

认证脚本注入 LAST 结果，Native 侧使用真实只读连接并计数：

| case | 选中路径 | Native 执行次数 |
|---|---|---:|
| 正常 LAST | LAST | 0 |
| 时间戳不符、值不符 | NATIVE_FALLBACK | 1 |
| 错误 `Timeseries`/`DataType` | NATIVE_FALLBACK | 1 |
| 空结果 | NATIVE_FALLBACK | 1 |
| 注入异常 | NATIVE_FALLBACK | 1 |

成功路径不双执行；失败路径执行 Native 一次。这些是 LAST 认证阶段的契约测试，
不是 LAST 生产插件。阶段 4 已实现通用 Registry、Validator、单候选 Executor
和保守 fallback，但 LAST 专用插件、结果归一化和认证规则尚未实现，因此 LAST
仍未接入生产 Registry，也没有 LAST 的真实回退执行证据。

### 4.6 结论与认证缺口

**`NOT_VERIFIED`**，剩余子标记：

- `DEMAND_UNPROVEN`：没有代表性 Native 最新值 workload；
- `FALLBACK_CONTRACT_ONLY`：回退未进入生产 Executor。

用户授权专用库中的 NULL/缺失、重复时间戳、乱序、缺口、空序列和显式 SQL NULL
子项已完成（79/79 通过）；2026-11-01 变体复核已完成别名、`LIMIT`/`ORDER BY`、
`OFFSET`、无 ORDER BY、wildcard、限定路径和空区间的边界测试。残余未测类别是
TTL/重启/并发/NaN、多进程/多日/冷缓存/背景负载和 DB 主机指标。

要升级为 `VERIFIED`，至少还需要：

- 实现并证明结果归一化后的列名、类型、行数、顺序、数值全部等价；LAST 返回四列，
  Native 两列，FLOAT 的 `Value` 还是十进制字符串；
- 证明真实 workload 中存在该 Native 模式，而不是为了启用策略虚构需求；
- 补 TTL、重启/崩溃恢复、并发、多进程、多日、冷缓存、背景负载和 DB 主机指标；
- 将 LAST 安全接入已实现的阶段 4 Registry/Validator/Executor，并测试
  timeout/连接中断不产生重复副作用。

在这些条件满足前，LAST 是研究候选，不是可执行策略。

## 5. CAND_MAXTIME_LASTVALUE

- **入口：** `SELECT MAX_TIME(s), LAST_VALUE(s) FROM <device> [time filter]`；
  server 接受，EXPLAIN 为 `AlignedSeriesAggregationScan`，`ScanOrder: DESC`。
- **正确性：** INT 和 FLOAT 最新值精确；列名为
  `MAX_TIME(...)`、`LAST_VALUE(...)`，`Time=0`，与 Native 的 `Time, series`
  不同；需要结果归一化。
- **生效：** 计划结构改变，确认不是文本改写。
- **收益（单次，n=1）：**

| case | 聚合 | Native |
|---|---:|---:|
| int | 7.197 | 6.765 |
| float | 5.413 | 6.331 |
| int + time filter | 7.503 | 6.706 |

  两次更慢、一次更快；单次观测不足以证明收益。
- **回退：** 可回退，未实现。
- **状态：** `NOT_VERIFIED`。没有重复测量、没有形状 adapter、没有语义边界测试。

## 6. CAND_ALIGN_BY_DEVICE

- **入口：** 多设备查询加 `ALIGN BY DEVICE`；server 接受。
- **生效：** `FullOuterTimeJoin` → `DeviceView`，`DeviceCount: 2`。
- **正确性：** 形状改变：列变为 `Time, Device, <series...>`；两设备
  Native 50 行 vs by-device 100 行；十设备 Native 50 行 vs by-device 500 行。
  不是同一结果集的 drop-in 替换。
- **收益（单次，n=1）：** 两设备 7.255 vs 8.350 ms；十设备 18.597 vs
  19.953 ms；本次更慢。
- **状态：** `NOT_VERIFIED`。形状不等价、无稳定收益，不应进入候选集。

## 7. CAND_FETCH_SIZE

- **入口：** 新建 `Session(fetch_size=500/1000/5000/10000)`；同一 SQL，
  只改客户端批次大小。
- **正确性：** 已测 50 devices × 10,000 行，结果行数一致；未覆盖所有类型/空值。
- **生效：** 无 server 计划变化；这是客户端/传输选项，不是数据库优化。
- **收益：** 3 次 warmup、10 次采样、随机/反向顺序；完整读取 10,000 行。

| fetch_size | mean | median | p95 | stdev | fetch mean | execute mean |
|---:|---:|---:|---:|---:|---:|---:|
| 500 | 363.839 | 355.923 | 427.100 | 24.130 | 327.184 | 36.655 |
| 1000 | 353.350 | 338.525 | 415.884 | 34.015 | 315.555 | 37.795 |
| 5000 | 356.620 | 345.589 | 450.660 | 37.696 | 297.143 | 59.477 |
| 10000 | 350.750 | 344.326 | 386.657 | 19.093 | 271.028 | 79.722 |

  10,000 的均值最低（较 500 低约 3.6%），但标准差/分位数重叠，且
  `execute_call_ms` 随批次增大而上升；不能证明稳定收益。没有改变 server
  计划，也没有多查询/多会话复验。
- **状态：** `NOT_VERIFIED`。可作为后续 benchmark 的控制变量，不能作为
  优化策略宣传。

## 8. CAND_PREDICATE_REWRITE

- **入口：** 对时间谓词去重、把 `>= x` 改写为 `> x-1` 等。
- **正确性：** 已测 base / duplicate / shifted 三种写法均返回 12 行，结果一致。
- **生效：** EXPLAIN 文本相同；计划没有变化。
- **收益（20 次采样）：** base 7.657、duplicate 7.954、shifted 7.700 ms；
  无收益，重复谓词反而略慢。
- **状态：** `NOT_VERIFIED`；按指南，不应把“只改文本、计划不变”当作策略。
  该候选不应进入 Registry。

## 9. CAND_SERIES_PATH（禁止）

- **入口：** 把 `FROM root.test.g_0.d_0` 改成
  `FROM root.test.g_0.d_0.s_130`。
- **观测：** server 接受，EXPLAIN 0 行；执行只返回 `Time` 列、0 行；
  单次 3.673 ms 的“更快”是因为没有结果。
- **状态：** `UNSUPPORTED`（子标记 `INVALID`：静默空结果）。必须在 Validator
  中拒绝，不能把空计划/空结果当成功。

## 10. CAND_UNION_MERGE（不支持）

- **入口：** `UNION` SQL；server 拒绝：

```text
700: Error occurred while parsing SQL to physical plan:
line 1:108 no viable alternative at input '... UNION'
```

- 单区间变体可执行（62 行），但与两个重叠区间的 UNION 不等价：重复行、
  边界和区间并集语义都不保持。
- **状态：** `UNSUPPORTED`；不能自动重写。负例见
  `tests/fixtures/sql/negative_cases.json:union_to_single_range_not_equivalent`。

## 11. CAND_PARALLEL_SPLIT

- **入口：** 客户端把一个多设备查询拆成每设备一个查询，用线程并发执行，
  再按时间戳合并；不是 server 内并行。
- **正确性：** 2 设备和 10 设备探测中行数/内容与 Native 一致（已测 case）。
- **生效：** 多个查询和客户端 merge，非单一计划。
- **收益（20 次采样）：**

| 设备数 | Native mean | Split mean | Split parallel wall | Merge mean |
|---:|---:|---:|---:|---:|
| 2 | 9.521 | 12.235 | 12.025 | 0.210 |
| 10 | 12.861 | 46.156 | 45.428 | 0.728 |

  两种规模都更慢；不能作为优化策略。没有并发压力/连接池/失败回退测试。
- **状态：** `NOT_VERIFIED`。

## 12. CAND_DB_PARAMETER（未验证）

- **入口：** 阶段 1 只读补充探测结果：
  - `SHOW CONFIG` 被拒绝：`700 ... no viable alternative at input 'SHOW CONFIG'`；
  - `SHOW CONFIGURATION` 同样被拒绝；
  - `SHOW VARIABLES` 可用，但只返回 `ClusterName`、`DataReplicationFactor`、
    `SchemaReplicationFactor` 和两个 Region consensus protocol class，属于
    集群复制/共识配置，不是查询级 tuning 参数；
  - 修改语法、热生效、重启要求和回滚仍未验证。
- **风险：** 不能根据理论猜测参数名、默认值或效果；不能在共享实例上试改。
- **状态：** `NOT_VERIFIED`（子标记 `UNKNOWN`：无查询级调优入口）。如要继续，
  必须在隔离实例、获得授权、记录重启和回滚方案的条件下探测；阶段 0/1 未做
  任何修改。

## 13. CAND_TABLE_MODEL（当前部署不支持用户数据）

- 表模型 `TableSession` 可连接；`SHOW TABLES` 返回 17 张
  `information_schema` 系统表，没有用户表。
- 表方言 `SELECT 1` 也失败（`701: From clause must not be empty`）。
- **状态：** `UNSUPPORTED`（子标记 `NO_USER_TABLE`）。表模型 adapter 只能在
  未来出现用户表并重新审计后启用；当前不能作为策略入口。

## 14. CAND_CLIENT_CACHE（未审计）

- 客户端结果缓存可能降低重复查询延迟，但不是数据库策略；新鲜度、失效、
  多会话一致性、内存和副作用都未定义。
- 阶段 0 未实现、未测量。
- **状态：** `NOT_VERIFIED`（子标记 `UNKNOWN`：未审计）。不得把缓存命中当作
  数据库优化收益。

## 15. 计时与实验协议（阶段 0 实际使用）

本阶段所有数字都只用于“是否有继续研究的价值”，不是认证 benchmark。

### 15.1 条件

- IoTDB 2.0.6，单机 ConfigNode + DataNode；
- 客户端与 server 同主机（`127.0.0.1`），无网络延迟变量；
- 静态数据集，热共享缓存，没有显式清缓存；
- 无并发/背景负载控制；单客户端进程；
- LAST 使用 2.0.6 客户端；部分早期探测使用 2.0.10；
- 结果集完整读取；`close_operation_handle()` 在 `finally` 中执行；
- 所有时间单位 ms，记录 `execute_call_ms`、`result_fetch_ms`、
  `client_wall_ms`。

### 15.2 采样

| 实验 | warmup | 采样 | 顺序 |
|---|---:|---:|---|
| LAST vs Native（六类型） | 5–10/session/type | 30 或 120 配对块/type | 两个 session；正反交替 + 部分块随机 |
| MAX_TIME/LAST_VALUE | 0 | 1 | 单次观测 |
| ALIGN BY DEVICE | 0 | 1 | 单次观测 |
| fetch_size | 3/config | 10 | 随机/反向交错 |
| predicate rewrite | 0 | 20 | 单组顺序执行 |
| parallel split | 3 | 20 | Native/split 交错 |

### 15.3 解释限制

- 不声称冷缓存；没有清 OS/存储缓存，也不能用重启冒充冷缓存；
- LAST 专项有 10,000 次配对 bootstrap 的 95% CI；其他单次/小样本实验没有
  显著性检验；
- p95 为 nearest-rank 近似，样本量很小时不稳定；
- 单次观测（MAX_TIME、ALIGN）不能用于收益结论；
- 不能把不同缓存条件、fetch_mode、结果规模混算；
- 不能把客户端 CPU 当 DB CPU；阶段 0 没有 DB 主机指标；
- 百分比只在同一实验内相对比较，不跨实验外推。

### 15.4 认证实验的最低要求（未来）

- 至少两个已认证策略 + S0；
- 固定数据集版本和 fetch_mode；
- 多会话/多进程、多日重复、随机顺序；
- 报告 n、均值、中位数、p95、方差/置信区间和 effect size；
- 覆盖查询模板、设备数、结果规模、空/边界/NULL/重复时间戳；
- 同时报告 `execute_call_ms`、`result_fetch_ms`、`client_wall_ms`、
  `decision_overhead_ms`、`end_to_end_ms`；
- 区分 `client_host` 与 `db_host`；若远程部署，必须采集 DB 主机指标；
- EXPLAIN ANALYZE 只用于诊断/专用 benchmark，不能默认双执行。

## 16. 阶段 0 策略门槛结论

| 门槛 | 结果 |
|---|---|
| S0 Native 入口与语义 | 通过（能力层 + 阶段 4 代码） |
| 至少一个非 Native 策略通过五项证明 | **通过：`S1_REPEAT_CACHE`（固定 epoch 契约）** |
| 策略状态全部可分类 | 通过 |
| 未验证策略未启用 | 通过（Registry 构造时强制检查） |
| 可复现 SQL/EXPLAIN 证据 | 通过 |
| 项目标记阻塞 | **已解除**：`S1_REPEAT_CACHE` 已认证；阶段 4 为 `PENDING RE-REVIEW`（前序 F1–F15 + 本轮 F-1/F-2/F-3 已修复待复审） |

**不要**为了让其他候选“通过”而把 LAST 或 MAX_TIME/LAST_VALUE 标成
`VERIFIED`。LAST 的干净数据和授权边缘数据证据增强了候选可信度，但需求证明
和生产插件仍缺失；MAX_TIME/LAST_VALUE 仍只有单次计时。S1 的通过不改变这些
候选的状态。S1 的认证边界是固定 epoch、进程内、exact SQL；它不是通用缓存，
也不把 LAST 的受控热缓存计时当作生产收益证明。

## 17. 可验证的后续研究方向

以下方向需要用户授权和新的实验，不属于阶段 0 已完成内容：

1. **LAST 认证子项目：** 只读部分已完成六类型、1,200 个时间采样、8 个边界、
   负例、EXPLAIN、30 块初始计时、两次 120 块扩展计时、变体边界和注入式回退
   契约；用户授权专用库中的 NULL/缺失、重复时间戳、乱序、缺口和空序列已完成
   79 个差分；全量 24,000 条授权 workload 扫描未发现符合条件的 Native 最新值
   语句。**仍需**补 TTL/重启/并发/NaN、多进程/多日/冷缓存/DB 主机指标；获得
   真实需求；并为 LAST 实现结果归一化和专用插件，接入已实现的通用
   Registry/Validator/Executor。详见
   [`LAST_CERTIFICATION.md`](LAST_CERTIFICATION.md)。
2. **服务器参数审计：** 阶段 1 已确认 `SHOW CONFIG`/`SHOW CONFIGURATION`
   被拒绝，`SHOW VARIABLES` 只有 5 个集群复制/共识变量；仍需在隔离实例中
   寻找可修改的查询级参数、验证生效方式/重启要求和回滚，否则不能建立策略。
3. **其他外部执行策略：** 只有在有真实入口和可观测生效证据时才建立候选；
   并行拆分当前证据为负，不应重复包装。
4. **S0-only 基线：** 如果未来 S1 的 epoch 契约无法满足（例如数据持续写入且
   调用方无法声明不可变 epoch），系统仍应只启用 Native、监控、经验记录和
   “无优化动作”的决策；这不是优化闭环，但可作为可靠的数据/实验平台。
   S0-only 仍是禁用 S1 时的安全降级路径。

当前建议：**只启用已认证的 `S0_NATIVE` 和固定 epoch 下的 `S1_REPEAT_CACHE`；
不启用任何 `CAND_*`，不以 LAST 的受控热缓存计时作为生产收益证明，也不把 S1
的进程内缓存收益外推到 fresh-result 场景。**

## 18. 阶段 4 实现状态（S1 认证更新）

阶段 4 实现的是**有限、真实、经过验证的策略集合**，不是让 LLM 自由生成 SQL。
默认生产 Registry 包含 `S0_NATIVE` 和 `S1_REPEAT_CACHE`；所有 `CAND_*` 均未
启用。S1 只有在调用方声明 `cache_epoch`、SQL 位于确定性子集且 occurrence >= 2
时才被选中。

### 18.1 已实现的策略框架

| 组件 | 文件 | 行为 |
|---|---|---|
| 外部认证目录 | `configs/strategies.yaml`、`src/tsdb_optimizer/strategies/catalog.py` | schema 1；唯一 enabled baseline；enabled 非 Native 必须有五项证明；缺失/非法/重复/多 baseline 全部 fail-closed |
| `StrategySpec` / `StrategyRegistry` | `src/tsdb_optimizer/strategies/models.py`、`registry.py` | 只有 `VERIFIED` 且 `enabled=true` 的策略可选；重复 ID、错误 baseline、未认证、目录缺失/不匹配、实际插件类不符、启用目录条目未实现均拒绝；`applicability_rules` 顶层只读；`selection_policy` 必须与目录一致 |
| `S0_NATIVE` 插件 | `src/tsdb_optimizer/strategies/native.py` | 只执行原始 SQL；不改写、不加执行选项、不声称计划效果 |
| `S1_REPEAT_CACHE` 插件 | `src/tsdb_optimizer/strategies/repeat_cache.py` | 精确 SQL + 固定 epoch 的进程内缓存策略；不改写；SQL 子集 fail-closed；只提供 `cache_epoch` 选项 |
| 结果缓存 | `src/tsdb_optimizer/adapters/iotdb/result_cache.py` | 有界 LRU；完整结果准入；不缓存错误/部分/超大结果；按 epoch+SQL hash+strategy 隔离 |
| `is_applicable` / candidate | `src/tsdb_optimizer/strategies/` | Agent-1 分析、只读守卫、候选 SQL 和版本显式记录；Parser `UNSUPPORTED` 只告警，不阻止 Native；S1 的 exact-SQL 候选由 `EXACT_SQL_NO_REWRITE` 策略显式放行 |
| 只读 SQL Validator | `src/tsdb_optimizer/strategies/validator.py` | 原始/候选 SQL 只读检查、版本/模型兼容、执行选项所有权、计划效果 fail-closed、改写候选 parser 范围检查 |
| Decision Engine | `src/tsdb_optimizer/decision/engine.py` | `baseline` / `rule_based` / `adaptive` 三种 LLM-free 接口；按 exact SQL 的进程内 occurrence 选择 repeat 策略；adaptive 的 repeat-cache 建议也必须 occurrence >= 2；每个候选保留适用性、候选、验证、成本、选中/拒绝原因 |
| 成本估计 | `src/tsdb_optimizer/decision/cost.py` | 默认 `UNKNOWN` 并记录原因；历史估计只用真实成功、非 fallback 的样本，达到阈值才给中位数，不给伪造置信度；尚未按版本/模型/fetch_mode/结果规模/负载隔离 |
| 单候选 Executor | `src/tsdb_optimizer/execution/executor.py` | 只执行一个已选候选；执行前重新构建/验证候选，核对 Adapter 返回的 strategy/query/SQL hash；把候选执行选项传给 Adapter；不调用 `EXPLAIN`/`EXPLAIN ANALYZE` |
| 安全 fallback | `src/tsdb_optimizer/execution/fallback.py` | 只在 dispatch 前失败、明确未发送或 Adapter 明确 `retry_safe` 时回退；超时/完成状态未知禁止重试 |
| SQLite 持久化 | `src/tsdb_optimizer/storage/sqlite.py` | schema v6：decisions + executions + execution_reports，含决策 ID、attempt index、fallback、dispatch/completion、retry-safety、timeout、候选 SQL hash、fetch_mode、计划字段、`served_from_cache`/`cache_source_execution_id`/`cache_epoch`、报告级 fallback trigger/reason/blocked reason、final status、total/end-to-end、warnings、记录 schema 版本；v1–v5 原位迁移 |

### 18.2 当前可执行策略

```text
默认 Registry: ("S0_NATIVE", "S1_REPEAT_CACHE")
已认证非 Native 策略: 1 (S1_REPEAT_CACHE)
S1 适用条件: cache_epoch 非空 + exact SQL 在确定性子集 + occurrence >= 2
项目状态: 阶段 4 PENDING RE-REVIEW (前序修复 + R1/R2 fixes applied)
```

`CAND_LAST_SINGLE_SERIES`、`CAND_MAXTIME_LASTVALUE`、`CAND_ALIGN_BY_DEVICE`、
`CAND_FETCH_SIZE`、`CAND_PREDICATE_REWRITE`、`CAND_PARALLEL_SPLIT` 等仍没有
生产插件，不能通过配置直接启用；即使有人构造了 `enabled=true`，`StrategySpec`
和 `StrategyRegistry` 都会拒绝未 `VERIFIED` 的策略。

### 18.3 测试双桩的边界

`tests/unit/phase4_support.py` 中有 `TEST_ONLY` 策略和可编程 Adapter，只用于
验证 Registry 拒绝、Decision 选择/拒绝、单候选执行、fallback 触发条件和持久化
不变量。它们：

- 不进入 `build_default_registry()`；
- 不被文档描述为已认证策略；
- 不连接真实 IoTDB；
- 不作为“非 Native 策略已验证”的证据。

### 18.4 真实 IoTDB 证据

真实集成测试覆盖两类路径：

- **S0**：使用 `query/queries_dbs/q4.sql` 第 2 条，实际经过
  `DecisionExecutionService`：`S0_NATIVE` decision → 一次只读执行 → SQLite
  decision/execution 持久化；断言最终 `SUCCESS`、无 fallback、候选 SQL 等于
  原始 SQL，且正常路径不调用 `EXPLAIN`；
- **S1**：使用 q8/q7/q11 的真实只读 SQL，验证 Native/miss/hit 结果一致、
  hit 零 dispatch、真实 Session 计数、SQLite provenance、无 epoch 不选中，以及
  受控预 dispatch 缓存故障后的真实 Native fallback。详见本文件 S1 章节和
  `tests/integration/test_repeat_cache_certification.py`。

结果见 [`IMPLEMENTATION_STATUS.md`](IMPLEMENTATION_STATUS.md)。

### 18.5 仍未完成

- S1 是进程内缓存，不能跨进程/实例共享；正确性依赖调用方维护不可变 epoch；
- S1 没有 server 端失效或数据版本 API，不声称通用 freshness；
- CLI `optimize` 单进程单条 SQL 通常无法观察 hit；需要长驻 service/批量入口；
- 没有 Agent 4、学习器或 LLM；`adaptive` 只接受调用方显式提供的建议；repeat-cache
  建议在 occurrence < 2 时仍被拒绝并回退 Native（F9）；
- 执行选项目前只实现 `cache_epoch`；其他选项被 Validator 拒绝；
- 没有已验证的计划效果检查器；声明计划效果的策略一律 fail-closed；
- 历史成本尚未按 server 版本、数据库模型、`fetch_mode`、结果规模、缓存/负载
  上下文隔离；
- `end_to_end_ms` 已写入 schema v6 `execution_reports`；测量截至 attempt 持久化，
  不包含 report 行自身写入或序列化耗时；
- 真实 server 超时/连接中断不会产生自动 fallback（这是保守策略）；已证明的是
  预 dispatch 缓存故障后的真实 Native fallback；
- LAST 等 `CAND_*` 仍未认证；
- 未进入阶段 5 或其他后续阶段。

## 19. 外部策略认证目录（阶段 4 框架修复）

### 19.1 目的

原先 Registry 只检查插件自己的 `StrategySpec` 是否声明 `VERIFIED`，因此一个
恶意或错误的插件可以自证。现在策略是否能注册由 Registry 之外的
`configs/strategies.yaml` 决定：

```text
插件 spec ──比对──> 外部目录条目 ──比对──> 实际插件类身份
      │                  │
      └──── Registry 拒绝任何不一致 ────┘
```

### 19.2 目录不变量

- `schema_version` 必须为 1；
- 策略 ID 唯一；
- 恰好一个 `enabled=true` 且 `is_baseline=true` 的条目；
- enabled 条目必须是 `VERIFIED`，并有 version 和 implementation_class；
- enabled 非 Native 条目必须有 `proof_entry`、`proof_correctness`、
  `proof_effect`、`proof_benefit`、`proof_fallback` 五项非空证明；
- 目录中的 enabled 条目必须全部由运行代码注册；否则
  `assert_catalog_coverage()` 在 `build_default_registry()` 中失败；
- `implementation_class` 还必须等于实际
  `type(plugin).__module__ + "." + type(plugin).__qualname__`；
- 目录只声明数据，不导入实现、不连接 IoTDB、不会自动启用 disabled 候选。

### 19.3 当前目录内容

```text
schema_version: 1
enabled strategies: 2
  S0_NATIVE (baseline, VERIFIED, version 0.1.0)
  S1_REPEAT_CACHE (non-Native, VERIFIED, version 1.0.0)
disabled strategies: 11
  CAND_LAST_SINGLE_SERIES
  CAND_MAXTIME_LASTVALUE
  CAND_ALIGN_BY_DEVICE
  CAND_FETCH_SIZE
  CAND_PREDICATE_REWRITE
  CAND_SERIES_PATH
  CAND_UNION_MERGE
  CAND_PARALLEL_SPLIT
  CAND_DB_PARAMETER
  CAND_TABLE_MODEL
  CAND_CLIENT_CACHE
enabled non-Native strategies: 1 (S1_REPEAT_CACHE)
```

目录中的状态复制自本文件；本文件仍是认证证据文档。

### 19.4 测试边界

- `allow_uncertified_for_testing=True` 可绕过目录，但仍不能启用未
  `VERIFIED` 的 spec；生产 CLI/Service 不使用；
- TEST ONLY 双桩不会进入默认 Registry，也不会被当作认证证据；
- 目录缺失、YAML 非法、字段不匹配、伪造实际类、启用条目未实现均有单元测试
  拒绝。

## 20. S1 认证、方案 A 的终止与阶段 4 验收结论

2026-11-01 用户最初确认 **方案 A**：`query/` 是当前完整 workload，其中没有
符合 `ORDER BY time DESC LIMIT 1` 的 Native 最新值需求；因此不为启用 LAST 等
候选而虚构 workload。该结论仍适用于 LAST 等 `CAND_*`，但**不再是当前项目
状态**：用户随后要求解决“没有已认证非 Native 策略”和“没有生产 fallback 证据”
两个阻塞项。实现的是 `S1_REPEAT_CACHE`，不是对 LAST 的重新包装。

### 20.1 需求来自真实 workload

- `query/queries_dbs/q8.sql`：2,000 条语句，unique=1；同一 SQL 重复 2,000 次；
- `query/queries_dbs/q7.sql`：2,000 条语句，unique=99，每条 unique SQL 约重复
  20 次；
- `query/queries_dbs/q11.sql`：2,000 条语句，unique=99，每条 unique SQL 约重复
  20 次；
- 没有新增、编造或改写 workload；S1 只缓存既有 exact SQL 的结果，且 SQL 文本
  不变。

### 20.2 五项证明

| 证明 | 位置 | 结果 |
|---|---|---|
| 入口证明 | [S1 入口证明](#s1-repeat-cache-入口证明) | 真实 service/Registry/Executor/Adapter；计数测试证明 hit 零 dispatch |
| 正确性 | [S1 正确性](#s1-repeat-cache-正确性) | q8/q7/q11 列、行数、全部预览行与 Native 一致；空/NULL/重复时间戳/时间边界/NaN/Inf/顺序有专项测试；固定 epoch 契约 |
| 生效证明 | [S1 生效证明](#s1-repeat-cache-生效证明) | hit `request_dispatched=false`；miss 1 次；SQLite v6 provenance |
| 收益证明 | [S1 收益证明](#s1-repeat-cache-收益证明) | 30+30 direct、50+50 service；hit 中位 0.177/0.141/0.228 ms vs Native 20.824/15.090/25.867 ms；q8 端到端中位 1.49×，p99 略差；完整统计见 JSON |
| 回退证明 | [S1 回退证明](#s1-repeat-cache-回退证明) | 真实 Executor/Adapter/Session；预 dispatch 缓存故障后真实 Native 成功，恰好 1 次重试 |

### 20.3 阶段 4 验收

| 验收项 | 结果 | 证据 |
|---|---|---|
| 非 Native 策略正确性测试 | **PASS** | S1 真实 2.0.6 Native/miss/hit 差分；固定 epoch 契约 |
| Native fallback 可用（生产路径） | **PASS** | 真实 Executor + 真实 Adapter + 真实 IoTDB；缓存查找故障在 dispatch 前注入；Native 成功且只执行一次 |
| 阶段 4 完整验收 | **PENDING RE-REVIEW（前序修复 + R1/R2 fixes applied）** | 严格审查曾判定 `NOT ACCEPTED`；前序 F1–F15 和本轮 F-1/F-2/F-3 修复及回归测试已提交，等待复审 |
| 严格审查 F1–F5 | **FIXED，待复审** | 缓存元数据不可变、SQL guard fail-closed、hit 计时语义、hit 日志 ID、失败路径回归测试 |
| 严格审查 F6–F10 | **FIXED，待复审** | F6 `EXACT_SQL_NO_REWRITE` 显式策略；F7 完整统计与 30/50 次证据；F8 边界类别测试；F9 adaptive occurrence < 2 回退 Native；F10 `setup_iotdb206_env.sh` 可复现 2.0.6 venv |
| 严格审查 F11–F14 | **FIXED，待复审** | F11 失败决策不消耗 occurrence（含并发预留回滚）、reason 不再声称首次为 Native；F12 真实 v2/v3/v4/v5 迁移测试；F13 schema v6 `execution_reports` 持久化报告级 fallback/warnings/end-to-end；F14 doctor 只对安全未加引号 tree path 做 EXPLAIN 探测 |
| 严格审查 F15 | **FIXED，待复审** | 统一 `end_to_end_ms` 口径；同步 Conda `MANTO` + `apache-iotdb==2.0.6` 环境说明和 2026-11-02 实测结果；澄清 `.venv-iotdb206/` 与 `.env` 的区别 |
| 本轮严格复审 F-1/F-2/F-3 | **FIXED，待复审** | F-1 版本化资源阈值闸门；F-2 无效 DB 快照带值隔离；F-3 验收表补资源阈值行。证据见 `docs/IMPLEMENTATION_STATUS.md` §1/§4.11 和 `tests/unit/test_decision_engine.py`、`tests/unit/test_resource_policy.py` |

**阻塞标记已解除：** `BLOCKED: NO_VERIFIED_NON_NATIVE_STRATEGY` 不再成立。
但阶段 4 当前状态是
**`PENDING RE-REVIEW（前序修复 + R1/R2 fixes applied）`**；严格
审查曾判定 `NOT ACCEPTED`，修复结果不能由实现方自行宣布重新通过。这里的“生产
fallback 证据”是**生产代码路径上的受控预 dispatch 故障注入**，不是自然 IoTDB
故障；真实超时/完成未知仍按保守策略禁止回退。不得把该证据外推为“任何 server
故障都会自动回退”。

### 20.4 不可外推的边界

- S1 不是通用 freshness 缓存；调用方必须保证 epoch 内数据不变；
- S1 不是服务端计划优化；EXPLAIN 不会出现 S1 节点；
- 收益是受控热缓存微基准，不是生产 benchmark；没有冷缓存/并发/多日/DB 主机
  指标；服务端端到端只声称中位/均值改善，p99 不优于 Native-only；
- S1 是进程内缓存，进程退出即丢失；CLI 单次调用通常只能观察 Native；
- LAST 等 `CAND_*` 仍为 `NOT_VERIFIED`/`UNSUPPORTED`，不因 S1 认证而改变；
- 阶段 4 认证不等于可以进入阶段 5；本阶段没有实现 Agent 4、学习器或 LLM。


## 阶段 5 增量说明（2026-11-02）

本文前述 PENDING RE-REVIEW、schema v6 与未进入阶段 5 的描述为阶段 4 历史证据。
当前用户已明确授权阶段 5；最新阶段状态以 IMPLEMENTATION_STATUS.md 为准，
学习设计见 LEARNING_DESIGN.md。没有新增或扩大任何策略认证；CAND_* 仍禁用。

SQLite v7 的 Agent 4 按 DB/策略版本和严格 context 记录已执行策略统计，
不能通过一次成功或快速运行给策略授予 VERIFIED。MISMATCHED 导致版本隔离：
绑定 learner 的 Registry 在 get_enabled/assert_selectable/Executor 重新校验时
拒绝该版本，持久化记录保存在 strategy_quarantine。测试旁路仍只限单元测试。
S1 的固定 epoch、进程内缓存、无 server/实现版本缓存键和无 single-flight 等
边界未改变。L1 的经验上下文隔离不是 S1 缓存边界改造。

真实阶段 5 Benchmark：evidence/phase5_learning_2026-11-02.json；全部观测执行的是
现有认证 S0/S1。对未支持 Agent 1 模板的 q5 仅记录 LOW_CONFIDENCE，无 reward；
不以这次 Benchmark 替代或扩大已有 S1 语义认证套件。


## 2026-11-03：完整请求条件收益补充

自然LOW、不可变epoch、COUNT子集、客户端READY状态下，小规模160列和大规模1600列各36个真实S1/S0配对块；S1−S0完整请求均值分别−27.729ms/−103.235ms，点态99%区间[-52.804,-12.236]/[-130.138,-78.696]，三个连续批次方向一致。两种规模EMPTY区间跨0，不据此声称Native或S1稳定更快。

这是已命中条件下的单会话适用性证据，不含外部显式填充成本，不证明任意SQL、一般生产新鲜度或自适应优于固定S1。S1策略版本仍1.0.0，执行机制/适用规则未修改。来源及失败批见PHASE7_E2E_REWARD_SCENARIOS.md与对应evidence JSON。
