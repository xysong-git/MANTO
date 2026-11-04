# 阶段 6 修复后复验：ACCEPTED

日期：2026-11-02。用户授权修复 F-1/F-2、补回归并重新验收。本轮结论：
**ACCEPTED（阶段 6 四 Agent 串行闭环，限定既有策略/数据源契约）**。没有进入阶段 7。
下方保留原初审失败证据，不能将历史 NOT ACCEPTED 当成当前结论。

## F-1 / F-2 关闭证据

| 项目 | 复验 | 最终实现与证据 |
|---|---|---|
| F-1 执行前快照失效 | PASS / CLOSED | execution/executor.py:101 在所有候选验证之后检查资源，:473 逐来源检查；Orchestrator 强制 require_fresh_state=True；失效走既有 predispatch fallback，仅一个真实 Native attempt，保留 decision/occurrence。6 种来源×延迟阶段组合与原反例通过 |
| F-2 状态日志丢失 | PASS / CLOSED | logging.py:15 白名单加入 state/event_index/execution_ids/fallback_trigger；orchestration/orchestrator.py:85 输出阶段与 ID、耗时和 fallback/final_status；5 种路径经实际 JsonFormatter 校验，原反例通过 |

正式新增 `tests/unit/test_phase6_review_regressions.py` 共 12 个用例；未删除原测试，
原独立反例未放宽断言。Executor 闸门在调用 Adapter 前执行；不声称服务器执行期间资源恒定。
Native 本身不因缺失/过期状态被阻断；正常全 unavailable DB 不被伪造为低负载，也不错误阻断。

## 最终代码实际运行结果

```bash
IOTDB_INTEGRATION=1 PYTHONPATH=src /opt/anaconda3/envs/MANTO/bin/python -m pytest -q
# 719 passed in 28.09s，13 个真实 IoTDB 集成用例，无 skipped

PYTHONPATH=src:tests/unit /opt/anaconda3/envs/MANTO/bin/python -m pytest -q docs/evidence/phase6_review_probes.py docs/evidence/phase5_review_probes.py docs/evidence/phase5_rereview_probes.py
# 8 passed in 2.22s

PYTHONPATH=src /opt/anaconda3/envs/MANTO/bin/python -m compileall -q src tests scripts
# exit 0
```

## 逐项复验

| 检查项 | 判定 | 证据/边界 |
|---|---|---|
| 阶段 6 全部任务 | PASS | 十阶段完整 trace、三模式、CLI、失败隔离；F-1/F-2 关闭 |
| TODO/pass/mock 假实现 | PASS | 新闸门为实际检查；合成时钟/指标仅在测试；既有明确不支持功能不虚报 |
| 字段/单位/时间戳/source | PASS | UTC、ms、来源、统一 IDs 保持；决策选择与最终 fallback attempt 分开记录 |
| 异常可见、成功结果隔离 | PASS | 既有六类失败测试和 5 种日志路径；下发状态未知不自动重试 |
| 未验证 IoTDB API/SQL | PASS | 未新增 SDK/SQL；真实 2.0.6 tree 套件通过 |
| client/DB 指标分离 | PASS | 独立来源闸门；DB 未提供保持 UNKNOWN |
| Registry/Validator 不能绕过 | PASS | 先重建并验证，失效仍通过既有 Native fallback 验证，不直接执行 SQL |
| 错误样本无正向学习 | PASS | 新降级只记录实际 Native attempt，fallback reward=null；阶段 5 原反例全部通过 |
| 普通路径不执行 EXPLAIN ANALYZE | PASS | 没有新增查询入口；真实 SDK 调用计数用例通过 |
| 重复执行/缓存/完整消费 | PASS | 失效原候选零下发，Native 一次；fresh 候选不误否决；既有缓存/消费回归通过 |
| 测试实际执行与失败覆盖 | PASS | 全量 719、独立反例 8，日志检查格式化后的输出而非仅 LogRecord extra |
| 真实 DB-host 负载/收益 | BLOCKED / UNKNOWN | 没有代理或生产收益证据；不是阶段 6 闭环验收所要求的新功能，未伪造 |

本轮仅修复当前阶段缺陷。复验无遗留 FAIL；旧阶段 4 显式无快照库调用保持原契约，
阶段 6 始终启用执行前检查。无新增 Web UI/框架/微服务、LLM、Bandit 或阶段 7 工作，未提交 Git。

---

## 初审记录：NOT ACCEPTED（修复前历史，原样保留证据）

日期：2026-11-02。范围：IMPLEMENTATION_GUIDE.md:924–945 的阶段 6，以及 Prompt 7 十项要求。
本轮不修复生产代码；测试通过数量不能代替验收条件。两个独立反例均实际失败。

## 实际测试

解释器固定为 `/opt/anaconda3/envs/MANTO/bin/python`（Conda MANTO）。

```bash
IOTDB_INTEGRATION=1 PYTHONPATH=src /opt/anaconda3/envs/MANTO/bin/python -m pytest -q
# 707 passed in 25.42s，包含 13 个真实 IoTDB 集成用例，无 skipped

PYTHONPATH=src:tests/unit /opt/anaconda3/envs/MANTO/bin/python -m pytest -q /tmp/manto_phase6_review_probes.py
# 2 failed in 1.73s
```

反例已原样保存至 `docs/evidence/phase6_review_probes.py`，可用相同 PYTHONPATH 显式运行。
保存后再次执行 `PYTHONPATH=src:tests/unit /opt/anaconda3/envs/MANTO/bin/python -m pytest -q docs/evidence/phase6_review_probes.py`，结果仍为 **2 failed in 0.85s**。
它们使用测试专用 Adapter/快照，不把模拟数据当作真实数据库证据，不修改源代码。
历史真实 trace `docs/evidence/phase6_workflow_2026-11-02.json` 仍然有效，但只能证明那一次
正常运行（一次 SDK 调用），不能证明以下失败路径。

## F-1（P2）：决策持久化期间快照过期，仍下发非 Native 策略

- 指南依据：IMPLEMENTATION_GUIDE.md:415 未知/过期状态须降级；阶段 6 第 6 项。
- 位置：`src/tsdb_optimizer/orchestration/orchestrator.py:122` 检查新鲜度，
  `:142` 持久化决策后，`:156` 直接进入 Executor。执行前没有再次验证资源新鲜度。
  `src/tsdb_optimizer/execution/executor.py:226` 的 preflight 重建/验证 SQL、版本与选项，
  没有资源快照时效闸门；S1 本身不依赖快照。
- 实证：反例先产生一次 Native occurrence；第二次使用 100ms 有效期的真实协议合成快照。
  进入 record_decision 时仍 fresh，模拟 120ms 审计写入延迟后已 stale（未篡改 captured_at）。
  最后 Adapter 收到 `S1_REPEAT_CACHE`，trace.effective_mode=`rule_based`，warnings=[]。
- 影响：SQLite 锁等待/fsync 或其他决策后延迟可使已失效负载证据继续授权非 Native。
  本轮未声称发生结果错误或错误奖励；实际奖励仍受执行时质量闸门约束。
- 最小修复：在真正下发前的执行 preflight 复核所用资源证据。过期时走已验证 Native
  或明确拒绝，不依赖一次早期检查。保留 decision/attempt/trace 的真实关系与降级原因，
  不额外执行原候选，不重复计入 occurrence。补决策保存/Validator 延迟后失效测试，
  保留现有“检索期间过期”测试。

## F-2（P2）：结构化状态转换日志丢失 state

- 指南依据：阶段 6 可观测状态、超时/异常/fallback 的状态与日志。
- 位置：`src/tsdb_optimizer/orchestration/orchestrator.py:85` 通过 extra 写入 `state`；
  `src/tsdb_optimizer/logging.py:15` 的 `_EXTRA_FIELDS` 不含 state，`:54` 仅输出白名单字段。
- 实证：正常十阶段均产生 LogRecord；经实际 JsonFormatter 编码后，十条 state 均为 None。
  持久化 JSON trace 的状态正确，但不能替代结构化日志；trace 写库失败时该缺陷更明显。
- 最小修复：将 state 纳入日志协议，并测试真实 formatter 输出，覆盖成功、拒绝、超时和
  fallback 路径的阶段名及关联 IDs。不要仅检查 caplog 中尚未格式化的 extra 属性。

## 逐项检查

PASS 仅表示本轮审查范围内有实现与测试支持，不代表穷尽所有可能缺陷。

| 用户检查项 | 判定 | 文件位置与证据 |
|---|---|---|
| 阶段所有任务真实实现 | FAIL | 主链/CLI 已实现，资源过期降级存在 F-1，结构化日志存在 F-2；orchestrator.py:122、:142、:156、logging.py:15 |
| TODO/pass/mock 假实现 | PASS | 新 orchestration 无 TODO/pass/mock；strategies/models.py:141 和 schemas/execution.py:137 是明确无操作分支；sql_parser.py:1371 拒绝未实现 metadata 扩展，不是阶段 6 工作流假成功；测试 doubles 在 tests 内 |
| 字段、单位、时间戳、source 一致 | PASS | orchestration/models.py:45 UTC 校验、:94 IDs/终态/dispatch 校验；schemas/monitoring.py:190 来源隔离；test_orchestrator.py:40、:156。新鲜度使用缺口另见 F-1 |
| 异常被吞掉 | PASS（有界） | orchestrator.py:162、:188、:205 写库/监控/学习异常显式 warnings；未知执行结果次数 null；test_orchestrator.py:100 六类故障、:146 未知执行异常。日志 state 缺失另见 F-2 |
| 未验证 IoTDB API/SQL | PASS（2.0.6 tree 范围） | tree_adapter.py:580 使用实际 SDK；test_orchestrator_live.py:23、:27、:41 核对版本、真实调用、原 SQL；既有真实集成此次重跑通过，未增加数据库语法 |
| client metrics 冒充 DB metrics | PASS | schemas/monitoring.py:190 拒绝来源混用；orchestrator.py:106 保留来源；test_orchestrator.py:233 多来源资源测试；DB 无代理仍 UNKNOWN |
| LLM 绕过 Registry/Validator | PASS | 无 LLM 路径；orchestrator.py:138、:148、:149 与 executor.py:226 重建验证；test_orchestrator.py:167 历史建议仍通过真实引擎/Executor |
| 错误样本正向学习 | PASS | agents/learning_feedback.py:158–195 失败/超时/认证/结果规模/资源质量过滤，reward 仅 VALID；learning/repository.py:25 绑定持久化真实执行；test_orchestrator.py:120、:132、:258；test_learning_review_regressions.py:9、:23 等本轮通过 |
| 普通路径 EXPLAIN ANALYZE | PASS | orchestrator.py:156 唯一执行入口；monitor 阶段只采样和 record_execution；test_orchestrator_live.py:53 断言所有 SDK SQL 均为原 SELECT |
| 重复执行、缓存污染、未消费结果 | PASS（既有缓存契约内） | execution/fallback.py:57 超时不重试；tree_adapter.py:620 全量 fetch；result_cache.py:34 epoch+SQL+策略键；test_tree_adapter.py:185、:583、:871、:955；真实 SDK 次数核对通过。仅固定 epoch/串行实例，不扩张到跨服务器共享缓存保证 |
| 测试真实运行且覆盖失败路径 | FAIL（覆盖不足，非伪造） | 本轮全套 707 passed/13 真实集成；既有失败注入有效，但新增两项验收反例均失败，尚未纳入正式回归并修复 |
| 真实 DB 主机负载/收益证据 | BLOCKED | 没有 DB-host 代理，UNKNOWN；此项不允许以 client psutil 或模拟快照替代，也不是本轮 F-1/F-2 的借口 |

## 阶段 6 十项任务

1. 状态机 PASS（正常/失败路径及协议）。
2. 统一 IDs PASS。
3. 四 Agent 主顺序 PASS。
4. 三种模式 PASS（L1 只在严格已知上下文下建议）。
5. parser unsupported 安全 Native/拒绝 PASS。
6. 资源过期降级 FAIL（F-1）。
7. 学习持久化/模型更新失败不影响已成功结果 PASS（已测故障路径）。
8. 超时/异常/fallback 状态与日志 FAIL（F-2；执行/trace 本身已覆盖）。
9. 单查询/JSON workload CLI PASS。
10. 端到端测试与完整 trace PASS（正常实际链路）；失败路径完整性尚受 F-1/F-2 阻塞。

## 结论与下一步

**NOT ACCEPTED**。最小修复仅 F-1、F-2 并纳入正式回归，复跑真实全套与独立反例后重新验收。
本轮仅更新审查文档/状态和保存反例；生产代码未改，未删除或放宽测试。不要进入阶段 7。
