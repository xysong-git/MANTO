# 指标字典（阶段 3–5）

本文件定义 Agent 3 使用的系统快照、查询计时、超时和计划签名指标，以及阶段 4
持久化的决策、单候选执行和 fallback 指标。所有指标都带来源、时间戳和缺失
原因；没有真实来源的值必须为 `null`，不得填 0、均值或客户端替代值。

## 1. 部署判断与数据来源

阶段 0 的环境报告记录：IoTDB ConfigNode/DataNode 与 Python 客户端运行在
**同一台主机**上，但当前环境**没有安装 DB 主机指标代理/Exporter**。

因此阶段 3 的真实采集范围是：

| 来源 | `source_type` | 状态 |
|---|---|---|
| 客户端主机 `psutil` | `client_host` | 已实现，真实采样 |
| IoTDB DB 主机 CPU/内存/磁盘 | `db_host` | 未接入，全部 unavailable |
| IoTDB 服务端查询/算子指标 | `db_metrics` | 未接入；`null` |
| 未知来源 | `unknown` | 只用于表达“没有 DB 主机来源”，不能设 `valid=true` |

**客户端与 DB 主机不混淆的规则：**

1. 客户端采样器的固定 `source_type` 是 `client_host`；
2. DB 主机快照的 `source_type` 只能是 `db_host` 或 `unknown`；
3. DB 主机未配置代理时使用 `UnavailableDbHostSampler`：`source_type=unknown`、
   `source_host=unknown`、所有指标为 `null`，每个字段附
   `NO_DB_HOST_AGENT` / `NO_DB_METRICS_SOURCE`；
4. 不会把同一主机上的 `client_host` 数值复制到 `db_host`，即使物理机器相同；
5. `SystemSample` 同时保存两者，并计算 `db_host_metrics_available=false`。

未来若部署 DB 主机代理，应先验证代理提供的指标单位、采样窗口和主机身份，再
新增 `source_type=db_host` 的真实 sampler；本阶段不虚构该接口。

## 2. `SystemSnapshot`

实现：`src/tsdb_optimizer/schemas/monitoring.py`。

| 字段 | 类型/单位 | 语义 |
|---|---|---|
| `snapshot_id` | str | 一次快照的稳定标识，用于执行记录关联 |
| `captured_at` | UTC datetime | 采集时刻；必须带时区 |
| `source_host` | str | 数据来源主机名。DB 无来源时为 `unknown` |
| `source_type` | enum | `client_host` / `db_host` / `db_metrics` / `unknown` |
| `cpu_percent` | %，0–100 | 主机 CPU 使用率 |
| `memory_percent` | %，0–100 | 主机内存使用率 |
| `disk_usage_percent` | %，0–100 | **容量使用率**，不是 I/O 速率 |
| `disk_read_bytes_per_sec` | bytes/s，≥0 | 聚合磁盘读速率 |
| `disk_write_bytes_per_sec` | bytes/s，≥0 | 聚合磁盘写速率 |
| `active_queries` | int，≥0，可空 | 数据库并发查询数；客户端采样无法观测，固定 unavailable |
| `valid` | bool | 该来源是否具备 CPU、内存、磁盘容量三项核心真实值 |
| `stale_after_ms` | ms | 本快照自身的有效期窗口 |
| `valid_until` | UTC datetime（计算字段） | `captured_at + stale_after_ms` |
| `unavailable_fields` | map[field, reason] | 每个 `null` 指标的明确原因 |
| `warnings` | list[str] | 采样告警，例如基线尚未建立、单指标失败 |
| `error_type` / `error_message` | str/null | 整次采样异常的结构化记录 |

### 2.1 不变量

R1/R2：新鲜窗口是 `captured_at <= now < valid_until`，无时钟容差。
未来快照以 `FUTURE_SNAPSHOT` 标记；决策记录 `*_FUTURE_DEGRADED`。
客户端、DB_HOST、DB_METRICS 分别隔离；一个来源失效不会删除其他来源的
有效负载信息。隔离保留快照身份和采样时间，所有指标置空。


- 指标为 `null` ⇔ 必须在 `unavailable_fields` 中有原因；
- 指标非空 ⇔ 不能被标记 unavailable；
- `valid=true` 要求 `source_type != unknown` 且 CPU/内存/磁盘容量都存在；
- 过期判断使用 `captured_at`、`stale_after_ms` 和调用方提供的 `now`；调用方
  可用 `max_age_ms` **收紧**窗口，但不能延长；
- `is_fresh()` 只判断时间；`is_usable()` 同时要求 `valid=true`。

### 2.2 CPU 与磁盘 I/O 的首次采样

当前 CPU 使用每个采样器独立保存的 `psutil.cpu_times()` 累积计数；磁盘 I/O counters 同样是相对上一次观测的
差分指标。第一次采样只建立 baseline，`cpu_percent` 和两个 I/O 速率字段为
`null`，原因 `BASELINE_NOT_ESTABLISHED`；不会用 0.0 冒充真实值。第二次及
后续采样才产生值。内存使用率和磁盘容量使用率在第一次采样即可用。

磁盘 I/O 通过 `disk_io_counters(perdisk=False)` 的累计字节差除以单调时钟
经过的秒数计算；这是**整机聚合速率**，不是单块盘、不是容量使用率。计数器
减少时两个速率均为 `null`，原因 `COUNTER_RESET`，并以新计数器重建 baseline。

### 2.3 采样错误

采样器按指标隔离异常：

| reason | 含义 |
|---|---|
| `BASELINE_NOT_ESTABLISHED` | 需要前后两次观测，当前只有基线 |
| `PSUTIL_ERROR` | `psutil` 调用或采样器本身异常 |
| `COUNTER_RESET` | I/O 累计计数器回退 |
| `INVALID_VALUE` | 数值越界、NaN、类型错误或经过时间非正 |
| `FUTURE_SNAPSHOT` | captured_at 晚于当前时间；不作为实时负载证据 |
| `NO_DB_HOST_AGENT` | 没有配置 DB 主机指标代理 |
| `NO_DB_METRICS_SOURCE` | 没有已验证的 DB 查询/并发指标来源 |
| `NOT_APPLICABLE_TO_CLIENT_HOST` | 该指标对客户端主机采样器不适用 |

单个指标失败只影响该字段；整体采样器抛异常时，`SystemMonitor` 生成
`valid=false` 的结构化错误快照，并保留 `error_type`/`error_message`，不会
静默复用旧值。

## 3. 周期采样

实现：`src/tsdb_optimizer/monitoring/system.py`。

- `PsutilSystemSampler`：真实客户端主机采样；
- `UnavailableDbHostSampler`：DB 主机 unavailable 快照，不调用 `psutil`；
- `SystemMonitor`：
  - `start()` 立即采样一次，然后按 `interval_s` 周期采样；
  - `sample_once()` 同时生成客户端和 DB 主机快照，组成 `SystemSample`；
  - `latest_sample()` 返回最近快照；
  - `fresh_sample()` 只在客户端快照未过期时返回；
  - `history()` 返回有界历史（默认 120 条）；
  - `start()`/`stop()` 幂等；线程异常会记录日志并继续运行。

`SystemSample` 的字段是 `client_host`、`db_host`、可空的 `db_metrics`，以及
计算字段 `db_host_metrics_available`。JSON 路径本身已带命名空间，例如
`client_host.cpu_percent`，因此客户端字段不会被误读为 DB 值。

## 4. 查询级指标与超时

来源：`IoTDBTreeAdapter.execute_readonly()` 和 `ExecutionResult`。

| 字段 | 单位 | 语义 |
|---|---|---|
| `execute_call_ms` | ms | 从调用 `execute_query_statement` 到 SDK 返回结果集句柄/首个响应；cache hit 为 0 |
| `result_fetch_ms` | ms | 完整遍历 `has_next()`/`next()` 拉取所有结果的时间；cache hit 没有 server 结果集，固定为 0 |
| `client_wall_ms` | ms | 客户端墙钟总耗时，覆盖执行调用、完整拉取和收尾；cache hit 时覆盖 Adapter 内部的缓存查找/构造，不包含分析、决策或 SQLite |
| `rows_returned` | int | 完整消费的行数；不是预览行数 |
| `status` | enum | `SUCCESS` / `ERROR` |
| `error_type` / `error_message` | str/null | 异常类型和截断后的消息 |
| `timeout_ms` | ms/null | 请求传给 IoTDB client 的超时；`null` 表示未设置 |
| `timed_out` | bool | 客户端观察到超时类异常；不是服务端取消状态证明 |

计时都是**客户端观测值**，不是 IoTDB 服务端 CPU 或算子耗时。适配器只执行
一次，不做隐藏双执行。`execute_call_ms`、`result_fetch_ms`、`client_wall_ms`
分别记录；结果集无论预览是否截断都会被完整消费并关闭 operation handle。

`S1_REPEAT_CACHE` 命中时没有 server dispatch，也没有 server 结果集，因此
`execute_call_ms=0.0`、`result_fetch_ms=0.0`。缓存查找耗时不是结果拉取，不能
写入 `result_fetch_ms`；它单独记录在 `cache_hit` 结构化日志的
`cache_lookup_ms`，并出现在 `CACHE_HIT` warning 文本中。`client_wall_ms` 是
Adapter 本地墙钟，仍覆盖缓存查找与返回构造，但**不覆盖**查询分析、决策、
持久化或完整 `end_to_end_ms`。cache lookup 失败时同样没有 server 结果集，
`execute_call_ms`/`result_fetch_ms` 均为 0，错误结果带 `CACHE_LOOKUP_FAILED`。

`Session.execute_query_statement(sql, timeout=...)` 的 timeout 单位是毫秒，
使用当前锁定的 `apache-iotdb==2.0.6` 已验证 API。SDK 会把它转发到
`TSExecuteStatementReq` 和 `TSFetchResultsReq`。`timeout_ms=None` 时客户端按
SDK 默认值 0 发送；**0 在服务端的语义（无超时或服务端默认）是 UNKNOWN**，
本字典不据此断言，也不会把未知超时报告成 `0`。超时识别基于异常类型
和错误消息中的 timeout 标记，且仅在错误路径使用。

## 5. `ExecutionRecord` 关联

实现：`src/tsdb_optimizer/schemas/monitoring.py`，由
`PerformanceMonitoringAgent.record_execution()` 生成。

| 字段 | 语义 |
|---|---|
| `query_id` | 原始查询 ID，来自 `ExecutionResult` |
| `decision_id` | Agent 2 决策 ID；阶段 3 无 Agent 2 时为 `null` |
| `execution_id` | 执行 ID，来自 `ExecutionResult` |
| `snapshot_ids` | 本次记录附加的客户端/DB/DB 指标快照 ID |
| `strategy_id` | 本次执行的 Registry 策略；阶段 3 直接路径为 `S0_NATIVE`，阶段 4 可为 `S1_REPEAT_CACHE` |
| `started_at` / `finished_at` | 执行时间边界 |
| `execute_call_ms` / `result_fetch_ms` / `client_wall_ms` | 从执行结果复制 |
| `rows_returned` / `success` / `timed_out` / `timeout_ms` | 执行结果 |
| `plan_status` | EXPLAIN 计划状态；没有计划时为 `UNAVAILABLE` |
| `actual_plan_hash` | 稳定计划 hash；只在计划 `AVAILABLE` 时存在 |
| `strategy_certification_status` | 从 Registry 推导：已注册且 `VERIFIED` 为 `PASSED`，否则 `NOT_CERTIFIED`；执行成功不授予认证 |
| `runtime_result_check` | 固定 `NOT_CHECKED`，不做默认双执行 |
| `unavailable_fields` | 缺失字段的显式原因 |
| `warnings` | 结果、快照过期、计划解析等告警 |

缺失值规则：

- `decision_id=null`：`NO_DECISION_IN_PHASE_3`；
- `timeout_ms=null`：`TIMEOUT_NOT_CONFIGURED`；
- `actual_plan_hash=null`：`PLAN_UNAVAILABLE` 或 `PLAN_EMPTY`；
- `snapshot_ids=[]`：`NO_SNAPSHOT_ATTACHED`。

`record_execution()` 默认读取监控器最新快照；没有快照时立即采样一次；如果
快照已过期，记录仍会关联它，但增加 stale 警告。调用方也可以显式传入
`SystemSample` 或 `SystemSnapshot` 列表。记录仍保存在 Agent 3 的进程内有界
历史中，**不是**阶段 4 的持久化决策链路。阶段 4 的
`DecisionExecutionService` 会把 query/decision/execution/execution_report 写入
SQLite schema v6（见第 8–10 节），但尚未把本 `ExecutionRecord` 合并进
schema v6。

## 6. 计划解析与稳定签名

实现：`src/tsdb_optimizer/monitoring/plans.py`，只解析阶段 0 实际观测到的
树模型文本格式：

| 格式 | 识别 | 状态 |
|---|---|---|
| 树模型 `EXPLAIN` | ASCII box 算子节点 | `AVAILABLE` / `EMPTY` / `UNAVAILABLE` |
| 树模型 `EXPLAIN ANALYZE` | `[PlanNodeId N]` + 缩进节点 | `AVAILABLE`（若显式提供文本） |
| 表模型 `EXPLAIN` | 无用户表和真实 fixture | 非空文本一律 `UNAVAILABLE` |

状态：

- `EMPTY`：EXPLAIN 返回成功但 0 行；不是合法计划；
- `UNAVAILABLE`：没有可识别节点、预览被截断、表模型未验证等；
- `ERROR`：解析器或 EXPLAIN 执行错误；保留 `error_type`/`error_message`；
- `AVAILABLE`：有至少一个节点和稳定 hash。

### 6.1 稳定签名

树模型 box 节点保留：

- 去掉末尾 `-<数字>` 的算子类型；
- 父子关系（`parent_index`）和深度（`depth`）；
- 语义属性白名单：`Series`、`QueryAllSensors`、`ScanOrder`、`Limit`、
  `Order`、`DeviceCount`、`Aligned`、`Offset`、`Aggregator-*`。

剔除：

- box 绘制字符、空格、布局、兄弟节点排版顺序；
- `PlanNodeId`、算子名后缀数字；
- `Partition`；
- fragment/instance、IP、端口、DataRegion；
- cost、CPU、wall time、内存、文件计数等运行时值；
- `EXPLAIN ANALYZE` 的输出行数和所有运行时统计。

节点先按稳定子树内容做确定性排序，再用
`json.dumps(sort_keys=True, separators=(",", ":"), ensure_ascii=False)` 序列化，
最后计算 SHA-256。相同结构但节点 ID、分区、排版不同的计划应得到相同 hash。

`EXPLAIN ANALYZE` 的解析只对已经提供的文本做结构提取；Agent 3 **没有**任何
调用 ANALYZE 的方法，普通执行路径也不会调用它。

### 6.2 计划比较

`compare_plan_results()` / `compare_plan_hashes()`：

- 两边都 `AVAILABLE` 且 hash 相同 → `MATCHED`；
- 两边都 `AVAILABLE` 但 hash 不同 → `MISMATCHED`；
- 任一边不是 `AVAILABLE` 或没有 hash → `UNAVAILABLE`。

不比较原始文本；`EMPTY` 不能当作“计划符合预期”。

## 7. `EXPLAIN ANALYZE` 政策

`EXPLAIN ANALYZE` 会真实执行查询，因此：

- `IoTDBTreeAdapter.execute_explain()` 只拼接 `EXPLAIN `；
- Agent 3 的 `explain()` 只调用该适配器方法；
- 没有隐式 ANALYZE 开关；
- 单元测试断言发出 SQL 不包含 `ANALYZE`；
- 只允许在隔离的只读测试数据上手工采集 ANALYZE 文本，不能用它作为普通请求
  的预执行步骤，也不能据此进行默认双执行。

## 8. 阶段 4 决策指标

实现：`src/tsdb_optimizer/decision/models.py`，持久化写入 SQLite `decisions` 表。
决策由 `DecisionExecutionService` 在**执行之前**持久化，因此执行器失败时仍可
审计选择原因；不会为失败的执行伪造 execution 行。

| 字段 | 类型/单位 | 语义 |
|---|---|---|
| `decision_id` | str | 本次决策的稳定 ID，execution 通过它关联 |
| `query_id` | str | 对应 `queries.query_id` |
| `selected_strategy_id` / `strategy_version` | str | 最终选中的 Registry 策略及其版本 |
| `original_sql` / `candidate_sql` | str | 原始只读 SQL 与选中候选 SQL；S0 两者相同 |
| `execution_options` | JSON map | 候选执行选项；`S0_NATIVE` 为空，`S1_REPEAT_CACHE` 只允许 `{"cache_epoch": "..."}`；未注册选项即验证失败 |
| `expected_latency_ms` | ms/null | 选中的成本证据；`UNKNOWN` 时必须为 null |
| `confidence` | 0–1/null | 仅当成本证据真实提供时存在；历史中位数不伪造置信度 |
| `decision_mode` | enum | `baseline` / `rule_based` / `adaptive` |
| `model_version` | str/null | 仅 adaptive 且采纳调用方建议时存在；无学习器 |
| `fallback_strategy_id` | str | 固定基线 `S0_NATIVE` |
| `reason` | str | 选择该策略的可解释原因 |
| `considered[]` | list | 每个 enabled 策略的 applicability/candidate/validation/cost/拒绝原因 |
| `cost_estimate` | object | 选中策略的成本证据，字段见 8.1 |
| `decision_overhead_ms` | ms | 决策引擎墙钟，从进入 `decide()` 到构造决策完成 |
| `warnings` | list[str] | 例如 `NO_VERIFIED_NON_NATIVE_STRATEGY`（自定义 Registry）、`COST_UNKNOWN`、`REPEAT_CACHE_REQUIRES_REPEAT` |
| `created_at` | UTC datetime | 决策创建时刻 |

### 8.1 `cost_estimate`

| 字段 | 语义 |
|---|---|
| `source` | `HISTORICAL_STATISTIC` / `RULE` / `UNKNOWN` |
| `expected_latency_ms` | 已知来源的估计值；`UNKNOWN` 时为 null |
| `sample_count` | 历史样本数；历史来源至少为 1 |
| `confidence` | 可选，0–1；没有可靠来源时为 null |
| `unknown_reason` | `UNKNOWN` 必填；已知来源必须为空 |

不变量：`UNKNOWN` 不能带 latency 或 confidence；已知来源必须有
`expected_latency_ms`；`HISTORICAL_STATISTIC` 至少有一个样本。CLI/Service 默认
使用 `UnknownCostEstimator`，所以真实默认输出是
`COST_UNKNOWN: NO_HISTORICAL_STATISTICS`。`HistoricalCostEstimator` 只统计与
候选 SQL SHA-256 + strategy ID 精确匹配、`SUCCESS` 且非 fallback 的
`client_wall_ms`；达到最低样本数前仍为 unknown。当前尚未按 server 版本、
数据库模型、`fetch_mode`、结果规模、缓存/负载上下文隔离；启用非 Native 策略前
必须补齐。

## 9. 阶段 4 执行、attempt 与 fallback 指标

实现：`src/tsdb_optimizer/schemas/execution.py` 和
`src/tsdb_optimizer/execution/models.py`；写入 SQLite `executions` 表。

| 字段 | 类型/单位 | 语义 |
|---|---|---|
| `execution_id` | str | 一次 Adapter 调用的稳定 ID |
| `query_id` | str | 与决策相同的 query |
| `decision_id` | str/null | 所属决策；直接 `query` 路径为 null |
| `attempt_index` | int ≥1 | 从 1 开始连续编号 |
| `is_fallback` | bool | 是否为 Native fallback attempt |
| `requested_strategy_id` | str/null | 本次 attempt 请求的 Registry 策略；必须与 Adapter 标签一致 |
| `strategy_id` | str | Adapter 实际执行标签；S0 为 `S0_NATIVE`，S1 为 `S1_REPEAT_CACHE` |
| `candidate_sql_sha256` | str/null | 实际执行的候选 SQL SHA-256；历史成本只按它与 strategy ID 匹配；旧记录可为 null |
| `adapter` | str | 例如 `iotdb-tree` |
| `status` | enum | `SUCCESS` / `ERROR` |
| `execute_call_ms` | ms | SDK 执行调用到返回结果集句柄；cache hit 为 0 |
| `result_fetch_ms` | ms | 完整遍历结果集；cache hit 没有 server 结果集，为 0 |
| `client_wall_ms` | ms | 客户端总墙钟；cache hit 时仅覆盖 Adapter 内部缓存查找/构造 |
| `row_count` / `column_count` | int | 完整消费的行数和列数；不持久化结果行 |
| `fetch_mode` | enum | `FULL` / `STREAMING` / `LIMITED`；阶段 4 Adapter 只实现 `FULL`，旧记录迁移默认 `FULL`（协议默认值，不代表历史缓存状态） |
| `timed_out` | bool | 客户端观测到超时类异常；不是服务端取消证明 |
| `timeout_ms` | ms/null | 传给 IoTDB client 的超时 |
| `request_dispatched` | bool | 客户端是否已把请求交给执行路径 |
| `request_completed` | bool | 客户端是否确认请求完成 |
| `retry_safe` | bool | Adapter 是否明确证明重试不会重复工作 |
| `retry_unsafe_reason` | enum/null | `TIMEOUT_OR_UNKNOWN_SERVER_STATE` 或 `REQUEST_COMPLETION_UNCONFIRMED` |
| `plan_status` / `actual_plan_hash` | str/null | 由显式计划诊断提供；普通 `optimize` 路径不执行 EXPLAIN，通常为 null |
| `served_from_cache` | bool | 是否由 `S1_REPEAT_CACHE` 直接返回进程内完整结果；只有 `request_dispatched=false` 的 S1 hit 才能为 true |
| `cache_source_execution_id` | str/null | 填充该缓存条目的真实 execution ID；仅 hit 非空，必须指向同一 epoch 的真实 miss execution |
| `cache_epoch` | str/null | 调用方声明的不可变 dataset epoch；仅 S1 非空，最大 200 字符且不含控制字符 |
| `error_type` / `error_message` | str/null | 截断后的结构化错误 |

retry-safety 不变量：

- `request_completed=true` ⇒ `request_dispatched=true`；
- `timed_out=true` ⇒ 不能 `retry_safe=true`，且必须有 unsafe reason；
- `retry_safe=true` ⇒ 不能超时、不能完成未知，且不能有 unsafe reason；
- 真实 Adapter 不会仅凭一次成功或异常猜测 retry-safe。

缓存 provenance 不变量：

- `served_from_cache=true` ⇒ `request_dispatched=false`、
  `request_completed=false`、`execute_call_ms=0.0`、`result_fetch_ms=0.0`、
  `cache_source_execution_id` 与 `cache_epoch` 均非空；缓存查找时间只在
  `cache_lookup_ms` 日志字段和 warning 中，不进入 `result_fetch_ms`；
- `served_from_cache=false` ⇒ 不得携带 `cache_source_execution_id`；
- `cache_epoch` 非空只表示 S1 请求带上了 epoch，不表示发生 hit；
- hit 不产生 server 延迟样本，不能用于 Native 或 S1 miss 的历史成本；
- 缓存查找在 dispatch 前失败时返回结构化 `ERROR`，`request_dispatched=false`，
  可触发保守 Native fallback；缓存存储在成功查询后失败只产生
  `CACHE_STORE_FAILED` 警告，不改变已成功的结果；
- 只缓存完整结果；错误、部分结果和超过行/字节上限的结果不进入缓存。

`ExecutionReport` 还包含：

| 字段 | 语义 |
|---|---|
| `selected_strategy_id` | 决策选中的策略 |
| `fallback_strategy_id` | 固定 `S0_NATIVE` |
| `fallback_used` | 是否实际发生且成功发起 Native fallback |
| `fallback_trigger` | `NONE` / `CANDIDATE_VALIDATION_FAILED` / `FIRST_REQUEST_NOT_DISPATCHED` / `FIRST_REQUEST_RETRY_SAFE` |
| `fallback_reason` / `fallback_blocked_reason` | 回退原因或被拒绝原因 |
| `final_status` / `final_execution_id` | 最后一个 attempt 的状态和 ID |
| `total_wall_ms` | 整个 Executor 流程墙钟，含预检、attempt 和收尾 |
| `end_to_end_ms` | 从 `DecisionExecutionService.run()` 入口到 attempt 持久化完成的服务级墙钟；服务层填充，已写入 schema v6 `execution_reports`；不包含 report 行自身写入或序列化耗时 |
| `attempts[]` | 连续编号的 attempt 及完整执行证据 |

回退只在预检失败（未发送）、明确未发送或明确完成且 retry-safe 时发生；超时或
完成未知会被阻止并记录原因。S0 自身失败不会重试。

### 9.1 持久化关联

SQLite schema v6 的四张表通过 `query_id`/`decision_id` 关联：

```text
queries 1 ── N decisions 1 ── N executions
                    └──────── 1 execution_reports
```

- 决策先写入，再执行；执行器在返回 report 前抛错时只有 query/decision 行；
- 每个 attempt 一行 execution，`attempt_index` 唯一且连续；
- 缓存 hit 也是一行可审计 execution，但 `request_dispatched=0`；
- history 查询只取 `candidate_sql_sha256` 非空、`SUCCESS`、`is_fallback=0` 且
  `served_from_cache=0` 的行；旧记录没有候选 hash 时不会被误用，缓存 hit 不会
  污染 server 延迟历史。

## 10. 阶段 3 `ExecutionRecord` 与阶段 4 持久化的关系

两者目前是**不同数据路径**，不能混为一谈：

| 路径 | 载体 | 是否持久化 | 说明 |
|---|---|---|---|
| 阶段 3 `PerformanceMonitoringAgent.record_execution()` | `ExecutionRecord` | 否，进程内 deque | 含 snapshot/plan 关联；`decision_id` 固定 null |
| 阶段 4 `DecisionExecutionService.run()` | `OptimizationRun` + SQLite v6 | 是 | 含决策、attempt、fallback trigger/reason/blocked reason、retry-safety、fetch_mode、缓存 provenance、报告级 end-to-end 和 warnings；不启动监控线程 |

阶段 4 的 `optimize` 不会自动启动 Agent 3，也不会把 `SystemSample` 写入 SQLite。
因此不能用阶段 4 execution 行反推客户端/DB 主机快照；也不能把阶段 3 的
`decision_id=null` 当作阶段 4 决策失败。

## 11. 限制

- 当前没有 DB 主机代理，因此 DB CPU/内存/I/O 全部 unavailable；
- 当前没有服务端查询并发、算子耗时或 planning 指标来源；
- `active_queries` 在客户端采样中不适用，DB 侧也没有可靠来源；
- 磁盘 I/O 是整机聚合，未拆分设备或进程；
- 计划 parser 只覆盖 fixture 验证过的 2.0.6 树模型文本；格式变化会返回
  `UNAVAILABLE`/`ERROR`，不会猜测；
- 表模型没有用户表/真实 EXPLAIN fixture，故 fail-closed；
- 计划签名只覆盖结构特征，不能证明执行结果语义等价；
- `ExecutionRecord` 仍保存在进程内；阶段 4 的 SQLite schema v6 持久化决策、
  attempt、fallback、retry-safety、`fetch_mode`、缓存 provenance 和报告级
  fallback/warnings/end-to-end，但只保存该报告的 `report_json`，不保存结果行，
  也尚未合并 `ExecutionRecord`；
- `S1_REPEAT_CACHE` 是进程内、固定 epoch 的完整结果缓存；跨进程/实例不共享，
  进程退出即丢失，且不提供 server 端失效或通用 freshness；调用方必须在同一
  epoch 内保证数据不变；
- `S1_REPEAT_CACHE` 只在 exact SQL occurrence >= 2 时适用；occurrence 只对成功
  决策计数，失败决策不消耗；默认 Registry 下第 1 次成功决策仍是 Native，
  单次 CLI 进程通常观察不到 hit；
- 默认决策在无 epoch 时保持 S0；有 epoch 且出现重复 exact SQL 时才可能选择 S1；
- 没有学习器；`adaptive` 不会自行产生建议，且 repeat-cache 建议在
  occurrence < 2 时仍会回退 Native；
- 资源阈值策略只覆盖 `client_host`/`db_host`/`db_metrics` 的 CPU、内存、磁盘
  容量百分比；I/O 速率和 `active_queries` 没有通用安全上限。阈值只在调用方提供
  `SystemSample` 时评估，且只使用 source 匹配、valid、fresh 的快照；DB 主机仍
  没有真实指标源，因此 DB 阈值只有协议/单元测试，不能当作真实 DB 负载证据；
- 真实超时/连接异常不会被 Adapter 标为 retry-safe，因此自然环境仍未观察到自动
  Native fallback；真实 fallback 证据来自受控预 dispatch 缓存查找故障；
- 集成测试需要 `IOTDB_INTEGRATION=1` 和真实本地凭据；默认跳过。2026-11-02
  使用 Conda `MANTO`（`apache-iotdb 2.0.6`）连接真实 2.0.6 server 的结果：
  默认完整套件 `598 passed, 11 skipped`，单元 `502 passed`，真实集成
  `11 passed`，启用真实集成的完整套件 `609 passed`。

## 阶段 5：经验与 L1 派生统计

当前 SQLite 已升级 v7。阶段 3/4 原有计时字段与来源不变；新增经验协议见
[LEARNING_DESIGN.md](LEARNING_DESIGN.md)。经验内嵌真实 ExecutionRecord 与
before/during/after 快照，保留 source_host/source_type、UTC 时间与缺失原因。
DB 指标仍 unavailable，不因保存经验而获得服务端来源。

- client_wall_ms 作为延迟主目标（ms）；不等于 end_to_end_ms 或服务端执行时间。
- reward = -log1p(client_wall_ms/1000.0)，无量纲，版本 negative-log1p-ms-v1。
- mean/median_latency_ms、variance_ms2（总体方差，分母 n）、mean_reward 只用 VALID。
- observed_count 为全部经验，valid_count 为合格经验；success_rate 为全部实际执行
  成功的比例，不能视为语义正确率。LOW_CONFIDENCE/INVALID 不产生 reward。
- SERVER/CACHE_HIT、实际返回行数、FULL fetch、数据库/策略版本和负载 source/host/bucket
  分开统计。客户端缓存命中延迟不得冒称 DB 延迟。
- 普通执行仍是 NOT_CHECKED；策略认证通过与本次运行差分 MATCHED 不等价。
  MISMATCHED 隔离策略版本并撤销其历史奖励，不抹除实际执行事实。

本阶段新增经验 JSON 不包含结果行或密码；客户端快照即使与 DB 位于同机，
也只标 client_host。真实 DB 负载证据仍 BLOCKED/UNKNOWN。

阶段 5 审查修复：当前训练统计除 VALID 外，还要求 quality_version=2 且资源策略指纹
一致。observed_count 仍保留全部审计样本；valid_count、延迟/reward 统计仅计算当前
准入规则合格样本。负载分桶只决定分组，实际安全准入对所有有效来源按执行时刻的
ResourcePolicy 评估；任一已知越限都不更新奖励。旧规则经验不被补造新质量证明。


## 显式本机 DB 主机采样（2026-11-02）

`VerifiedLocalDbHostSampler` 仅在显式指定本机 DataNode PID 时启用。检查字面量 loopback endpoint、PID 的 DataNode 主类、监听端口、进程创建时间以及 net/mnt/pid 命名空间；采样前后重新检查身份，重启或身份变化拒绝继续。独立采集主机资源并直接构造 `source_type=db_host` 快照，不读取/复制 client_host 快照。默认采样器仍返回 DB UNKNOWN，远程主机不能使用此功能。

CPU、内存、磁盘容量百分比与磁盘 I/O 速率沿用既有字段和单位；这些是数据库所在整台主机的指标，包含同机客户端和其他进程，不是 IoTDB 进程独占用量。active_queries 仍为 null / NO_DB_METRICS_SOURCE。IoTDB 内部执行时间、算子指标和隔离资源配额仍 UNKNOWN。本轮仅显式阶段7实验入口接入，不改变生产默认监控配置。

## 2026-11-02：阶段7实验 CPU 活跃协议（最新补充）

以上早期章节的“DB全部UNKNOWN”描述适用于默认采样器；显式本机实验现可通过
`VerifiedLocalDbHostSampler`核对DataNode/PID/端口/命名空间后，独立采集`db_host`整机指标。
这不是IoTDB独占资源，也不是`db_metrics`查询级指标。

`LearningContext.load_protocol`新增可选协议标识：

- `resource-max-v1`：历史默认，取CPU/内存占用/磁盘容量占用最大值；历史JSON、context key和模型分区保持原样。
- `cpu-activity-v2`：仅以CPU百分比分档，LOW<50、MEDIUM=50..90、HIGH>90；标识显式写入上下文、检索键和模型分区。实验前置CPU采样先在同线程建立基线，等待0.2秒后采样；原始时间戳保留。

v2标签只表示CPU活跃程度，不表示综合压力。内存占用率、磁盘容量占用率、磁盘读/写bytes/s继续分别记录；内存占用不是内存压力，吞吐不是磁盘饱和度。新增筛选协议将`memory_pressure`、`disk_saturation`显式标UNKNOWN，不根据占用率推断。原资源安全策略完全不变：CPU低也不能绕过内存/磁盘容量阈值。缺失/过期/来源错误仍UNKNOWN。

筛选的during CPU采样不能直接成为模型执行前上下文。后台连接数仅是实验控制量，不是DB负载标签。新旧协议不得混合，分析器检查声明与每个Experience协议一致。

### CPU筛选证据接收边界修复

`monitoring/activity.py`现在统一采集和读取校验：ActivityObservation仅允许DB_HOST，sampled_at拒绝naive时间并规范UTC，CPU标签必须与快照值和可用时间一致。过期/未来/缺失CPU只能标UNKNOWN。历史computed valid_until必须与captured_at+TTL一致。筛选汇总核对预声明计划与实际结果身份，终态后事件拒绝；旧结果没有逐执行SQL摘要时显式execution_sql_binding_verified=false，不据此声称完整执行溯源。当前两类实验CLI受Linux独立进程组deadline监护，客户端终止与服务端取消确认分别表述，后者保持UNKNOWN直到有实际证据。


## 2026-11-02：独立 CPU 计数与 v3 实验协议

实测发现旧 `cpu_percent(None)` 按调用线程共享基线：同线程先 client 后 DB 的采样会相互重置窗口。不同采样器实例不能隔离该状态。完整工作流旧 v2 CPU 证据不能用于本轮认证；原日志保留，不追溯改写。

现每个 PsutilSystemSampler 独立保存公开 `cpu_times()` 的累积计数，Linux total 排除重复计入的 guest/guest_nice，busy 排除 idle/iowait。第一次、零增量、计数回退和异常分别标 unavailable，不以 0 代替未知。SystemSnapshot 新增可选 `cpu_measurement=cpu-times-instance-v1`；旧记录缺字段时序列化仍省略，保留原摘要。

`cpu-activity-v3` 仅接受上述测量方法，LOW<50、MEDIUM=50..90、HIGH>90 和资源安全阈值不变；无测量标识返回 UNKNOWN。v1/v2/v3 上下文分区隔离。显式实验执行前使用 0.2 秒窗口。DB_HOST 是经 PID/监听端口/命名空间核对后的数据库所在整机，不是 IoTDB 进程独占指标。

受控实验的 50%–70% 目标来自用户授权的 CPU 共租进程，不能声称由查询自身产生；控制器 80% CPU / 90% 内存保护、每轮 30 秒自行到期，查询进程组 60 秒监护。`e2e_ms` 为围绕完整 Orchestrator.run 的客户端单调计时，包含解析、监控、决策、结果消费、学习与 trace 落盘；显式缓存准备和外部审计日志另记，不在该请求计时内。


## 2026-11-03：显式后台快照与请求内等待分离

PerformanceMonitoringAgent 的 `snapshot_mode=latest` 读取后台采样的深拷贝；默认仍为 sample。原 captured_at/source_type/snapshot_id 不变，线程停止或无样本显式报错。过期样本仍由 Orchestrator/Executor/学习准入处理，不能通过读取动作延长有效期。最终实验 controlled-e2e-v5 采用 interval_s=0.2、TTL=500ms，独立 CPU 计数协议仍 cpu-activity-v3。

before/after 是请求中读取快照的时点，不等于各自新采硬件；后台模式下 after 可能与 before 为同一 ID，甚至 captured_at 早于查询结束，不能用于声称请求导致的资源变化。无查询级 IoTDB CPU 数据。阶段准备时 monitor_ready 独立记录最多2秒就绪等待；正式请求端到端计时不包含这项显式初始化，也不再包含每次0.2秒同步等待，但后台采样的整机消耗仍真实存在。

端到端仍围绕完整 Orchestrator.run，包括学习/持久化；局部 L1 历史重放或不同查询的新旧轮均值不是同期策略收益证据。详情见 PHASE7_OVERHEAD_FIX.md。

最终v5为fixed-rate-db-first-v1：先DB身份核验与采样，再client采样，原子发布；显式WAL_FULL_REQUEST_SESSION逐操作提交，请求连接关闭也计入E2E。

## 2026-11-03：完整请求反馈（阶段7显式实验）

`CompletedRequest` 的source固定 `client_orchestrator_perf_counter_ns`；`elapsed_ns`为客户端单调计时纳秒，`end_to_end_ms=elapsed_ns/1e6`。UTC起止必须包围工作流与执行记录，且与单调时间差≤5ms；不一致拒绝学习，不修正原时间。指标包含Orchestrator内同步学习和存储session关闭，测量完成后才保存反馈。它不是DB execution latency。

新奖励 `negative-e2e-seconds-v1`：`-end_to_end_ms/1000`，保留平均延迟目标的线性尺度。旧`negative-log1p-ms-v1`仍表示Adapter延迟，禁止互换或混训；旧L1字段含义不变。

显式`latest_max_age_ms=250/latest_wait_timeout_s=0.5`：读取原始后台快照，年龄超预算时最多等500ms；等待计入请求E2E，超时报告失败并保守处理。原snapshot TTL仍500ms，不能通过读取刷新。默认latest不等待；两种配置在实验协议中明确区分。外部缓存准备、参考查询、审计序列化、离线Bandit拟合不计入单次Orchestrator请求，不能宣称这些成本为零或是在线更新。
