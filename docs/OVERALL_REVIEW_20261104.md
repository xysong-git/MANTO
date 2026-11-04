# 阶段 0–8 整体审查（2026-11-04）

结论：**NOT ACCEPTED**。工程能力、实验可复现性与研究收益分别验收。1021 项当前离线回归通过不能替代真实数据库验收；负收益实验不是 Benchmark 实现失败。未修改运行逻辑、测试、配置或原始实验数据，未启动 IoTDB 测试。

依据：IMPLEMENTATION_GUIDE.md 阶段 0–8（约760–992行）、§13、§17；当前源码、历史认证/验收文件、两次公平性修复后真实实验的离线证据。PASS 仅限 IoTDB 2.0.6 树模型、已验证 SQL 子集和不可变 epoch；不代表表模型、动态写入或任意 SQL 已支持。历史实测不是本轮重跑。

## 分阶段结论

| 阶段 | 结论 | 实现、位置与证据 |
|---|---|---|
| 0 能力审计 | PASS（限定范围） | docs/DB_CAPABILITY_AUDIT.md、STRATEGY_CATALOG.md；47 workload、24负例、12计划fixture；S0与固定epoch精确结果缓存S1有认证，LAST等候选不启用。tests/test_phase0_fixtures.py本轮通过。 |
| 1 基础工程 | PASS（限定范围） | adapters/tree_adapter.py:550/614/648 查询、消费、关闭；storage/sqlite.py:284/306/352 事务与初始化。test_tree_adapter.py:142/185/199/244/294/442覆盖完整消费、失败、关闭和禁止SDK重发。真实连接证据沿用历史集成及两次Benchmark，不宣称本轮运行doctor。 |
| 2 解析Agent | PASS | parsing/sql_parser.py:469 AST白名单、:1189 Adapter；47 workload的34支持/13不支持分类。tests/test_phase2_sql_features.py本轮通过；metadata enrichment未实现但显式拒绝，不作为估计值来源。 |
| 3 监控Agent | PASS（来源/缺失契约） | schemas/monitoring.py、monitoring/local_db.py:25/51/64；客户端和经身份核验的本机DB主机分开采样。计划解析只按fixture。纯服务端执行耗时和远程DB指标仍UNKNOWN。 |
| 4 Registry/Validator/Executor | PASS（S0/S1范围） | strategies/registry.py、validator.py:32/126/198/292，execution/fallback.py；固定epoch S1认证，不实现未经验证的改写。当前完整回归覆盖版本、SQL、资源、超时和fallback；两轮真实结果全部MATCHED。旧阶段4文档中的待复审状态不能作为现状单独引用。 |
| 5 学习反馈/L1 | PASS（库接口） | agents/learning_feedback.py:51/62/77/86/90、learning/repository.py；合格样本、幂等、隔离、原子发布、最低样本门槛确有实现。test_learning.py:114/140/192/211/286/408；本轮原反例和复审探针通过。 |
| 6 四Agent调度 | PASS（闭环功能） | orchestration/orchestrator.py:103/169/176/204/240；状态、ID、执行前过期检查、学习失败隔离。docs/PHASE6_REVIEW.md最新复验及本轮phase6探针通过；不等同生产部署。 |
| 7 Bandit/可选LLM | FAIL（研究收益）；部分功能PASS，扩展BLOCKED | bandit/model.py、delayed_store.py:129/139、decision/llm_advisor.py:37；参数、真实更新、冻结推断版本恢复已实现。阶段7 v7额外收益区间跨0；阶段8当前实现两轮E4稳定慢于E1/E3。实际LLM provider未接入、真实on/off对照UNKNOWN；可选LLM缺失不单独否决无LLM路线。恢复仅限冻结/排空后的推断指针，不是训练回滚续跑。 |
| 8 Benchmark | PASS（当前配置工具/复现）；扩展验收BLOCKED | benchmark/protocol.py:91/126/174，runtime.py:126/172，policy.py:17/40/84，analysis.py:94/135/365/395。E0–E4/A1/A2、随机/ABBA、原始记录、统计与导出均实现；两个LOW/并发1会话配置/源码/计划/结构相同。多负载×并发真实验证未完成，故不签发完整阶段验收。不能仅因E4负收益把实验工具判成假实现。 |

表内源码路径均相对于 src/tsdb_optimizer；测试路径相对于 tests/unit（明确写出其他路径的除外）。

## 用户要求逐项检查

| 检查 | 判定 | 代码位置与测试证据 |
|---|---|---|
| 所有阶段任务均达到验收条件 | FAIL | 阶段7收益和§17动态反馈研究结论未达到；扩展覆盖BLOCKED。不是“所有模块存在”即可通过。 |
| TODO/pass/mock假实现 | PASS（未发现冒充实现） | src中无TODO/FIXME实现占位；schemas/execution.py:137是缓存成功分支，strategies/models.py:141允许已认证但禁用，experiments/supervisor.py:16异常类型。parsing/sql_parser.py:1394明确抛未支持错误。单元测试替身不计真实样本；benchmark分析区分模拟/真实。 |
| 字段、单位、时间戳、source | PASS（限定已定义口径） | schemas/monitoring.py、benchmark/results.py:59/72/81/103；UTC、时效、非负耗时、ms/ns转换。benchmark/analysis.py:83/390把server_execution_ms保留null/UNKNOWN。内部ExecutionReport.end_to_end_ms不是完整请求延迟；正式比较使用Lane外层计时，test_service_request_scope.py:82验证包含关闭成本。 |
| 异常是否被吞掉 | PASS（已审路径） | learning_feedback.py:86/90记录日志+warnings，不覆盖成功结果；request_memo.py:36关闭失败显式警告，主体失败抛出；runner.py:298清理失败留痕后重抛。test_learning.py:211、test_service_request_scope.py:49/66、test_tree_adapter.py:199/244。不是保证所有未知故障都已覆盖。 |
| 未验证IoTDB API/SQL | PASS（启用路径） | tree_adapter.py:522只用已审EXPLAIN；validator.py:198/292检查选项/解析范围；catalog约束版本。未认证候选不启用，未声称SQLGlot完整支持IoTDB。fixture回归通过；新版本/表模型BLOCKED。 |
| client冒充DB metrics | PASS | monitoring/local_db.py:25核验PID/监听/namespace，:64采样前后核验；独立source。DB_HOST仍是整机指标，不是DataNode进程或单查询CPU；同机两源数值接近合理。test_phase8_cpu_evidence.py通过。 |
| LLM绕过Registry/Validator | PASS（安全接口） | llm_advisor.py:45/58/60只接受已验证候选strategy_id，JSON extra=forbid，超时/错误返回原因；没有执行SQL能力。真实provider收益BLOCKED。相关单元测试在1021项内。 |
| 错误样本正向学习 | PASS（质量门禁） | learning_feedback.py:62先隔离MISMATCHED，:77仅VALID更新；benchmark/policy.py:40检查实际完成请求并去重，:84冻结。test_learning.py:114/140/230/286/408，以及phase7重复ID/隔离回归。未执行arm没有虚构reward。 |
| 普通路径EXPLAIN ANALYZE | PASS | tree_adapter.py:522 EXPLAIN；普通_execute独立。专门能力审计/fixture允许ANALYZE，不纳入普通请求反馈。adapter调用计数及Benchmark无隐藏执行测试通过。 |
| 重复执行/缓存污染/未消费 | PASS（不可变epoch范围）；动态写入BLOCKED | tree_adapter.py:614/648完整消费后关闭；fallback只在安全完成条件下；runtime.py:172显式warmup/cache prepare、每lane隔离。test_phase8_benchmark.py、test_tree_adapter.py:185/294/442和test_service_request_scope.py:29。应用EMPTY/WARM不冒充IoTDB或OS冷/热；不可变epoch依赖用户真实保证，未实现动态失效。 |
| 测试真实运行/失败覆盖 | PASS（本轮非DB）；扩展真实测试BLOCKED | 本轮1021通过+独立8通过，含超时、结果读取/关闭失败、落盘失败、篡改日志、资源拒绝、Ctrl+C、重复反馈和模型回退。历史真实日志作为独立证据，不混入本轮pytest数量。 |

## 关键实测与公平性

两轮目录：benchmarks/results/phase8_fairness_20261104_120412_469528511 与 phase8_fairness_repeat_20261104_124904_168672646。每轮5940 MATCHED、960训练更新、3360留出请求、540/540 allocations；训练与留出分离，评估模型冻结。原始日志独立重放证据见 docs/evidence/phase8_fairness_repeat_comparison_20261104.json 与 PHASE8_FAIRNESS_REPEAT_RESULTS.md。

第二轮E4−E1：应用空缓存+25.869ms（99%区间[22.497,28.584]），预热+28.380ms（[24.881,31.594]）；E4−E3分别+16.105/+17.870ms，区间均为正。第一轮方向一致。非Native组动作/命中相同，不能将S1缓存收益归因于学习。区间是会话内配对block bootstrap，4000次、99%、探索性、无多重比较校正；两轮不合并伪增样本量。

原基础设施公平性F1已修复：execution/service.py:67复用request_scope/request_session，测试确认guard解析28→2、连接8→1；本轮test_service_request_scope五项继续通过。不能把历史F1再次作为未修复代码缺陷。

## FAIL和最小处理清单

1. **F1／研究结论：自适应额外收益FAIL。** 不存在一个有证据保证奏效的“小补丁”。保留负结果与部署禁用；若继续优化，先独立分析约23ms决策成本，再只改可证明冗余的工程开销，保持策略/奖励/Validator，重新登记新源码和未用于调参的独立评估。不得在当前留出集反复调参后宣称泛化收益。
2. **F2／文档一致性FAIL。** README原第5–13行同时称“两次复跑完成”“待独立重复”“真实复跑尚未执行”；analysis.py:430的通用报告模板仍写跨会话UNKNOWN，它描述单个run、不能替代跨run验收。最小修复：README历史段明确日期/历史身份；状态文档以当前审查为准。单run报告不擅自推断另一次会话是否存在。
3. **B1／覆盖BLOCKED。** 已准备small_count_multiload_c2.json，但未有当前版本LOW/MEDIUM×并发1/2完整真实日志。由用户执行已预声明实验，助手只离线分析；不可用模拟单元测试填补。
4. **B2／适用范围BLOCKED。** 远程DB监控、动态写入失效、真实LLM对照、持续在线训练回滚未认证。无需为本次审查重写这些模块；准确保持UNKNOWN/禁用和范围限制。

## 本轮实际命令

```sh
conda run -n MANTO python -m pytest -q tests/unit tests/test_phase0_fixtures.py tests/test_phase2_sql_features.py
# 1021 passed in 101.51s，exit 0
PYTHONPATH=src:tests/unit conda run -n MANTO python -m pytest -q docs/evidence/phase5_review_probes.py docs/evidence/phase5_rereview_probes.py docs/evidence/phase6_review_probes.py
# 8 passed in 1.76s，exit 0
```

没有执行IoTDB集成、训练或性能实验。工作区原有大量未跟踪文件和已修改手册，本轮未删除或重置它们。整体 **NOT ACCEPTED**，不建议进入下一阶段。
