# 实现状态

日期：2026-11-02。当前阶段：**阶段 6 四 Agent Orchestrator**。
状态：**ACCEPTED（阶段 6 F-1/F-2 修复后复验通过）**。
最终代码全量 **719 passed（28.09s）**，包含 13 个真实集成用例；
阶段 5/6 独立反例 **8 passed（2.22s）**。F-1/F-2 已关闭。
详见 [PHASE6_REVIEW.md](PHASE6_REVIEW.md)，修复前证据仍保留。
未进入阶段 7；未新增 Web UI、微服务或 Agent 框架，无 LLM 也完整运行。
阶段 5 已验收记录保存在 [PHASE5_IMPLEMENTATION_STATUS.md](PHASE5_IMPLEMENTATION_STATUS.md)，
阶段 4 历史见 [PHASE4_IMPLEMENTATION_STATUS.md](PHASE4_IMPLEMENTATION_STATUS.md)。

## 范围与验收证据

| 阶段 6 要求 | 实现及本轮验证 |
|---|---|
| 1. 查询状态机 | orchestration/models.py，十阶段正常路径，REJECTED/FAILED/TIMED_OUT 终态，非法转换拒绝 |
| 2. 统一 IDs | 每请求唯一 query_id，decision_id 与所有真实 attempts 的 execution_id 贯穿 trace/记录/经验；协议检查与 SQLite 回读测试 |
| 3. 四 Agent 顺序 | Orchestrator 串行 parse → snapshot → retrieve → decide → Registry Validator → Executor → monitor → learn；仅 Executor 下发 SQL |
| 4. 三模式 | baseline/rule_based/adaptive 均覆盖；合格真实协议历史统计能给后续 adaptive 建议并保存 model_version（模拟数据测试，非收益宣称） |
| 5. parser unsupported | guard 可确认只读则原 SQL Native；无法确认只读则零下发拒绝；重复未知语法也不能转缓存策略 |
| 6. 过期资源降级 | 检索后及 Executor 验证后下发前重检快照；客户端缺失/无效/未来/过期及 DB 带值无效/过期来源降级 Native；原多来源资源阈值继续生效 |
| 7. 学习故障隔离 | 经验写入/原子模型发布/observe 异常、监控/执行审计/trace 写入异常不替换成功结果；返回警告和日志，无重执行 |
| 8. 超时/异常/fallback | 超时不重试；结构化日志含 state/IDs/fallback；安全 predispatch fallback 按每 attempt 追踪；执行异常次数 UNKNOWN；query/decision 保存失败在下发前停止 |
| 9. CLI | run 单 SQL/单 SQL 文件；workload JSON SQL 字符串数组，串行输出 JSONL，共享实例；单条拒绝后继续，格式错误拒绝 |
| 10. 完整 trace | 单元协议/失败注入、CLI 和真实 IoTDB SDK 调用计数；脱敏十阶段 JSON 已保存 |

以上要求经修复后复验通过；第 6 项增加 Executor 下发前闸门，第 8 项修复实际格式化日志字段。[WORKFLOW.md](WORKFLOW.md) 定义详细边界。
SQLite 升级 v8，新增 workflow_traces，增量迁移保留 v1–v7 数据。所有策略认证不变：
S0_NATIVE / 固定 epoch 的 S1_REPEAT_CACHE；CAND_* 继续禁用。未修改 IoTDB 源码。

## F-1/F-2 修复与复验文件

- execution/executor.py：候选验证后、Adapter 下发前时效准入；保留原安全 fallback 和审计关系。
- orchestration/orchestrator.py：强制启用闸门，记录 fallback 原因；结构化日志包含阶段、IDs、执行结局。
- logging.py：补齐日志白名单。
- tests/unit/test_phase6_review_regressions.py：12 个正式用例；原独立反例保留。
- WORKFLOW.md / PHASE6_REVIEW.md / README / LEARNING_DESIGN.md / 本状态同步验收结论。

## 阶段 6 初次实现文件

- 新增：src/tsdb_optimizer/orchestration/{models.py,orchestrator.py,__init__.py}。
- 修改：src/tsdb_optimizer/cli.py、storage/sqlite.py、agents/learning_feedback.py（检索失败诊断透传）。
- 新增测试：tests/unit/test_orchestrator.py、test_workflow_cli.py；tests/integration/test_orchestrator_live.py。
- 迁移断言更新：tests/unit/test_storage.py、test_learning.py，未删除已有测试。
- 新增：scripts/phase6_workflow_evidence.py、configs/workload.example.json、docs/WORKFLOW.md、真实 trace JSON。
- 更新：README.md、docs/LEARNING_DESIGN.md、本状态；阶段 5 状态原样归档。

## 最终复验结果

- `IOTDB_INTEGRATION=1 PYTHONPATH=src /opt/anaconda3/envs/MANTO/bin/python -m pytest -q`：719 passed in 28.09s。
- `PYTHONPATH=src:tests/unit /opt/anaconda3/envs/MANTO/bin/python -m pytest -q docs/evidence/phase6_review_probes.py docs/evidence/phase5_review_probes.py docs/evidence/phase5_rereview_probes.py`：8 passed in 2.22s。
- `PYTHONPATH=src /opt/anaconda3/envs/MANTO/bin/python -m compileall -q src tests scripts`：exit 0。

## 初次实现历史命令与结果（非当前复验数字）

解释器：`/opt/anaconda3/envs/MANTO/bin/python`，Python 3.12.13，apache-iotdb 2.0.6。
以下简写 `python` 均指这个 Conda 解释器。

| 命令 | 实际结果 |
|---|---|
| `PYTHONPATH=src python -m pytest -q tests/unit/test_orchestrator.py tests/unit/test_workflow_cli.py` | 31 passed in 5.70s（之后新增 1 个仓库检索故障测试，一并纳入最终全量） |
| `IOTDB_INTEGRATION=1 PYTHONPATH=src python -m pytest -q tests/integration/test_orchestrator_live.py` | 1 passed in 1.03s |
| `IOTDB_INTEGRATION=1 PYTHONPATH=src python -m pytest -q` | **707 passed in 28.65s**，包括 13 个真实集成用例，无跳过 |
| `PYTHONPATH=src:tests/unit python -m pytest -q docs/evidence/phase5_review_probes.py docs/evidence/phase5_rereview_probes.py` | 6 passed in 1.33s |
| `PYTHONPATH=src python -m compileall -q src tests scripts` | exit 0 |
| `PYTHONPATH=src python scripts/phase6_workflow_evidence.py --output docs/evidence/phase6_workflow_2026-11-02.json --sqlite /tmp/manto-phase6-evidence.sqlite` | exit 0，SUCCEEDED，SDK 查询调用 1 次 |

开发中曾发现并修正：ParseResult 字段名使用错误、测试超时样本缺少 retry_unsafe_reason、
同名测试模块的收集冲突、doctor 关闭探测连接后的计数器安装顺序，以及 UNKNOWN 版本应作为
未提供能力信息而非已知不兼容版本传给 Validator。上表最终全量已覆盖修复。

## 真实单查询 trace

完整文件：[phase6_workflow_2026-11-02.json](evidence/phase6_workflow_2026-11-02.json)。

- query_id：`4f016b4a51954474aa6ae9742471839c`
- decision_id：`5708c026dd9d486e994222477d063170`
- execution_id：`bf1bdda7b666409fbfcea285d52b6822`
- 路径：RECEIVED → PARSED → SNAPSHOT → RETRIEVED → DECIDED → VALIDATED → EXECUTED → MONITORED → LEARNED → SUCCEEDED。
- 策略 S0_NATIVE，SDK execute_query_statement **1 次**，trace database_dispatches=1，fallback=false。
- 普通语义检查 NOT_CHECKED，学习 LOW_CONFIDENCE（UNKNOWN_DATASET_EPOCH / UNKNOWN_CACHE_CONDITION），无 reward/model 更新。
- client_wall_ms=3.584；trace 终态 elapsed_ms=43.752，两个口径不同，不混用。
- doctor 能力探测在工作流计数外；未执行在线 Native 对照或 EXPLAIN ANALYZE。
- 导出不含 SQL 文本、结果值、密码或主机地址。

## 已知限制、未完成事项、下一步

- DB 主机/服务端指标代理未提供，UNKNOWN；真实 DB 负载收益无证据，不用 client psutil 冒充。
- 检索严格相同 context，结果规模未知不预测建议；默认请求可以正确退回 Native。
- 学习为本查询实际 attempts 的同步小批次，会增加响应时间；无后台队列、进程崩溃恢复或服务器取消保证。
- 实例串行使用；不宣称多线程共享 Adapter/缓存的安全性。workload 当前为 JSON 数组，不是通用 SQL 脚本解析器。
- query/optimize 保留旧入口语义，四 Agent 入口明确为 run/workload。
- 本阶段复验 ACCEPTED，无遗留 FAIL。不开展阶段 7；后续工作等待用户指定。未提交 Git。
