# 阶段 6：四 Agent Orchestrator

日期：2026-11-02。状态：ACCEPTED（F-1/F-2 修复后复验通过，见 PHASE6_REVIEW.md）。仅阶段 6，无阶段 7 工作。

## 调用链与状态

`Orchestrator` 使用同一个 Store、Registry、资源策略连接四 Agent。每个实例串行使用，
Adapter/Store 由调用方负责关闭。CLI `run` 和 `workload` 使用此路径，旧 `query` /
`optimize` 兼容入口仍分别保留原始 Native / 阶段 4 库服务行为，不隐式启用学习。

```text
RECEIVED → PARSED → SNAPSHOT → RETRIEVED → DECIDED → VALIDATED
         → EXECUTED → MONITORED → LEARNED → SUCCEEDED / FAILED / TIMED_OUT
```

- RECEIVED：生成唯一 query_id。只读 guard 失败直接 REJECTED，零数据库执行。
- PARSED：Agent 1 原 SQL 解析，不改写；保存 QueryRequest 和 hash。
- SNAPSHOT：Agent 3 采集客户端和独立 DB 来源快照。没有 DB 代理时保持 UNKNOWN。
- RETRIEVED：Agent 4 严格 context 检索，返回受约束的 strategy_id/model_version 或无建议。
- DECIDED：Agent 2 选择，保存 decision_id；只在 adaptive 消费学习建议。
- VALIDATED：Registry 的中央 Validator 再校验；Executor 随后独立重建候选、校验并决定安全 fallback。
- EXECUTED：仅 Executor 调用 Adapter；保存每个真实 attempt、execution_id 和 ExecutionReport。
- MONITORED：Agent 3 为每个 attempt 生成 ExecutionRecord，关联 decision_id、snapshot IDs。
- LEARNED：对本查询实际 attempts 作有界串行批次写入，每个 execution_id 至多一份经验；
  Agent 4 保持 min_samples、质量版本/资源策略指纹、原子模型发布和隔离规则。
- 终态由实际最终执行决定；学习阶段完成不等于训练成功，须看 sample_status/model_updated。

trace 使用 UTC 时间戳，elapsed_ms 来自单调时钟，包含解析/监控/学习耗时，
终态时间不包含最后 trace 写库和 JSON 输出。ExecutionReport.end_to_end_ms 仍截止执行审计保存，
不冒充完整闭环时间。SQLite v8 新增 workflow_traces；v1–v7 增量迁移保留既有数据。
拒绝输入也保存 trace，因此 trace 的 query_id 不强制要求已存在 queries 行。
持久化 trace 只包含 hash、阶段、IDs、状态和审计元数据，不含原 SQL、结果值或凭据。
结构化日志显式输出 state、event_index、elapsed_ms、query_id、decision_id 和 execution_ids；
EXECUTED 日志还输出 fallback_used/fallback_trigger/final_status。日志保持 stderr，不混入 CLI 结果。

## 降级与异常

- parser UNSUPPORTED/ERROR 但只读 guard 通过：强制 baseline 原始 Native，不改写。
  guard 无法确认只读则拒绝，不把 parser 失败当成绕过安全校验的理由。
- 客户端快照不可用、无效、未来或过期：强制 Native。已带数值的 DB 快照来源不匹配、
  无效或过期也强制 Native；正常全 unavailable DB 不冒充低负载，也不阻断客户端路径。
  获取历史后再次检查新鲜度；Agent 2 保留逐来源清理和超阈值闸门。
  Orchestrator 还强制启用 Executor 的 require_fresh_state 闸门：候选验证全部完成后、
  调用 Adapter 前复核 client_host 和所有带值 DB 来源。审计写库/验证期间失效会进入
  已有 predispatch Native fallback，只生成一个真实 Native attempt，未执行的原候选不造记录。
  effective_mode 保留实际决策模式；fallback 后的实际策略由 attempts 记录，不覆盖原决策。
  此闸门是客户端下发准入，不承诺服务器执行期间资源恒定，也不声称已取消服务端查询。
  旧阶段 4 显式无快照库接口保持原契约，阶段 6 总是启用此检查。
- 已提交或完成状态不明、超时：不自动重试。只有 Executor 已认证的安全条件允许 fallback。
  trace 保存各 attempt 的 dispatched/completed/timed_out/fallback 标记；执行异常无法确认次数时
  database_dispatches=null（UNKNOWN），禁止把“无返回 report”解释成零执行。
- query/decision 保存失败发生在下发前：终止，无虚构 execution 行。
- 执行审计、监控、经验/模型、最终 trace 保存失败：记录警告和日志，保留已成功查询结果。
  不触发重执行；trace 写库失败时返回的 trace 仍可供调用方保存。
- 学习是执行后的有界同步批次，不是后台线程：学习故障不会改变结果，但会增加返回延迟。
  不提供进程崩溃后的自动补偿、跨进程排程或强制服务器取消承诺。

## 上下文与学习边界

库入口使用 LearningContext；CLI 接受已观测 server/version/model、调用方 dataset_epoch /
cache_condition。未知值明确 UNKNOWN，普通请求不因此被拒绝，但不允许假造质量合格奖励。
负载 identity/bucket 取自真实快照；默认 client_host，绝不作为 DB 指标。

检索保留阶段 5 的严格上下文相等，包括结果规模和结果来源。`--expected-result-rows`
只能是独立已知规模；省略时不预测行数，因此默认可能无可用 L1 建议。执行后用实际
row_count 建立经验；如果调用方提供的已知规模与实际不符，Agent 4 判 INVALID、不更新奖励。
CACHE_HIT 与 SERVER 严格分组，不能用 hit 延迟冒充数据库执行改善。
CLI 若启用 cache-epoch，必须与声明的不可变 dataset-epoch 相同。

NOT_CHECKED 是普通运行的正常语义检查状态；策略版本独立认证保持有效时可以经质量门控
产生 VALID 样本。不执行 Native 对照、EXPLAIN 或 EXPLAIN ANALYZE，不构造未执行策略的 reward。
CLI 初始化额外做一次客户端 psutil 基线采样，不产生数据库请求。DB 主机指标仍 UNKNOWN。

## CLI

```bash
conda activate MANTO
PYTHONPATH=src python -m tsdb_optimizer run \
  --sql "SELECT s_1666, s_1766 FROM root.db800.g_0.d_0, root.db800.g_0.d_1 WHERE time = 1640966450000" \
  --mode baseline
PYTHONPATH=src python -m tsdb_optimizer workload --file configs/workload.example.json --mode adaptive
```

workload 文件是 UTF-8 JSON 字符串数组，每个元素为独立 SQL。不是 SQL 脚本，不按分号
盲目拆分；元素内仍须通过单语句只读 guard。串行复用实例，逐条输出 JSONL，单条拒绝/失败
后继续后续请求；任一查询未成功则退出码 1。文件格式/参数错误退出码 2。
正式可用单查询示例见 README。大 workload 输出逐条写出，避免保留所有结果。

## 验证与真实 trace

`tests/unit/test_orchestrator.py` 覆盖三模式、历史 L1 建议、ID 链、只读拒绝、未知语法、
过期/无效/未来快照、源隔离、资源阈值、异常、超时、安全 fallback、学习/模型/审计故障。
`tests/unit/test_workflow_cli.py` 覆盖 JSON workload、逐条拒绝后继续及单查询 trace。
`tests/integration/test_orchestrator_live.py` 使用真实 SDK 调用计数核对 trace，不假设机器负载
固定低值，不为通过测试篡改快照。缓存可能因资源策略被否决，调用数必须与实际证据一致。

真实单查询证据：[phase6_workflow_2026-11-02.json](evidence/phase6_workflow_2026-11-02.json)。
生成命令（只读查询，doctor 探测在计数范围之外）：

```bash
PYTHONPATH=src python scripts/phase6_workflow_evidence.py \
  --output docs/evidence/phase6_workflow_2026-11-02.json \
  --sqlite /tmp/manto-phase6-evidence.sqlite
```

示例使用实际 fixture 的单条 SELECT，导出完整十阶段 trace，隐去 SQL 文本、返回值、
凭据和主机地址。工作流 SDK 查询调用数为 1、无 fallback、NOT_CHECKED；epoch/cache
条件未知，学习保存 LOW_CONFIDENCE，不更新模型。这是正确的质量门控结果，不是学习收益证据。


## F-1/F-2 回归覆盖

`tests/unit/test_phase6_review_regressions.py` 纳入正式套件：client_host/db_host/db_metrics
分别在决策保存、Executor 验证后到达过期边界（6 种组合），检查只下发 Native、真实
attempt/ID/occurrence 不被改写、无错误 reward；另验证 fresh 状态不被误否决，以及
5 种终态/执行路径经真实 JsonFormatter 编码后的状态与关联 IDs。原审查反例保留于
`docs/evidence/phase6_review_probes.py`，不删除或放宽断言。
