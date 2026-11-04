# 阶段7重新验收：SQLite初始化修复后

2026-11-03。依据IMPLEMENTATION_GUIDE.md阶段7条件、§10.4以及已预声明的双基线实验门槛复核。**最终NOT ACCEPTED，不进入阶段8，生产NOT READY。**

本轮没有修改运行代码、没有调用IoTDB。执行完整离线分析器，重新读取v7的1080批日志及源CSV审计资料；生成结果与原report.json逐字段完全一致，无不同字段。见[evidence/phase7_reaccept_offline_20261103.json](evidence/phase7_reaccept_offline_20261103.json)。执行方式为Conda MANTO、PYTHONPATH=src:scripts下调用`phase7_analyze_delayed_batch.analyze(Path('data/phase7_manual_v7_20261103/study/journal.jsonl'))`，exit0。没有为相同代码重复运行上一轮已通过的单元测试。

## 验收表

| 项目 | 判定 | 位置与证据 |
|---|---|---|
| F1模型版本恢复 | PASS（限定） | bandit/delayed_store.py:129/139，独立推断指针、冻结/排空要求、版本令牌与事务审计。用户49 passed；随后v7回归及最新全单元通过。只支持冻结后的离线推断恢复，不支持训练倒退续训。 |
| F3同口径L1统计对照 | PASS | experiments/batch_statistics.py；analyzer:268核对冻结统计和相同evidence。真实360训练批、720四策略留出批；本轮完整离线重放再次一致。对照完整不等于LinUCB优于L1。 |
| 重复SQLite初始化修复 | PASS（工程回归） | storage/sqlite.py:341/352，核对文件身份/user_version/schema_version才跳过迁移，成功提交后发布标记。test_storage_initialization.py:21/29/39/60/77覆盖重复、schema变化、替换、失败及版本错误；针对性124 passed in16.68s，全部unit863 passed in43.49s。 |
| 真实训练、隔离、质量与可复现 | PASS（v7范围） | 批次完整成本归因、仅真实已APPLIED反馈更新、冻结后独立SQL/时间留出。5400 VALID/MATCHED、360 APPLIED、2345独占检查、两模型证据一致；本轮报告逐字段重放一致。 |
| Registry/Validator、Native回退 | PASS（受审路径） | decision/engine.py:619、execution/fallback.py:57及相关单元/真实集成证据。未知候选不能执行，超时不盲目重发，普通请求没有EXPLAIN ANALYZE。最新修复未更改这些路径。 |
| 协议、指标来源、结果完整消费 | PASS（受审范围） | schemas/common.py:26、monitoring/local_db.py:51、tree_adapter.py:614。UTC/ms/负秒奖励区分，DB_HOST是核实后的整机指标，不冒充IoTDB进程或纯服务端耗时；Native结果完整消费。 |
| 错样本、异常与测试真实性 | PASS（已有覆盖） | schemas/learning.py:130、episodes.py:141、delayed_store事务拒绝/回滚。单元模拟不作实验样本；用户v7专项179 passed、全量966 passed。最新代码离线unit863 passed；不是重新跑了966项真实回归。未发现以TODO/pass/mock伪造已完成工作的新增证据。 |
| F2额外收益：最近真实实验 | FAIL | v7相对固定S1差-3.083ms、99%区间[-12.100,+6.051]，首个15块差+1.012ms。相对L1差-2.736ms、区间[-10.702,+5.050]。均未证明稳定更优。 |
| 最新初始化修复后的真实收益 | BLOCKED（缺证据） | v7启动摘要与当前代码比较：运行代码只有storage/sqlite.py变更；协议文档有后续说明变更。v7日志未变，但不是当前修复版本的实测收益。不能从少执行25条初始化SQL推断必然通过相对收益。 |
| 可选真实LLM效果 | BLOCKED（可选） | 默认关闭，安全建议接口已有测试，真实provider质量/延迟仍UNKNOWN。不作为无LLM路线独立否决条件。 |
| 持续在线训练/动态写入部署 | BLOCKED | 冻结推断实验不覆盖在线入队/更新总体成本；缓存依赖不可变epoch，没有服务端失效协议。禁止外推为持续写入场景正确性或收益证明。 |

## 致命风险与结论边界

本次未发现足以推翻已有受控实验结果的新增P0缺陷；这不是全输入或并发写入场景无缺陷的保证。独立源CSV COUNT审计覆盖参考结果首列，完整结果一致性以Native参考及策略语义回归核验；不能声称逐单元独立审计了全部源数据值。

v7两种学习策略的动作完全相同：长度2选S0、长度8选S1。这证明存在随声明长度变化的策略选择，但未证明同长度随负载变化，也未证明LinUCB优于简单统计。相对Native收益仍PASS；当前预声明协议同时要求优于固定S1，不能事后退回仅比较Native。

当前没有证据要求重写无关模块。剩余验收动作是：若要验证最新初始化修复的真实收益，先预声明新的运行版本及独立留出，再由用户执行IoTDB验证；保持原安全线、样本量与门槛。若仍无额外收益，继续保留FAIL，而不是反复运行直到显著。本轮仅重新验收，不启动或提前实现新实验。
