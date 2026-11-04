# 阶段7严格审查：v6

后续修复：用户报告F1回归49 passed in6.46s，在冻结后离线推断恢复范围通过；v7已完成，F3统计对照和独立重放通过，F2额外收益仍FAIL。见[恢复说明](PHASE7_BATCH_RESTORE_FIX.md)、[F2/F3协议](PHASE7_F2_F3_V7.md)。以下保留修复前审查结论。

2026-11-03。结论：**NOT ACCEPTED，不进入阶段8，生产关闭**。本轮静态审查现有实现和用户完成的v6证据，没有启动pytest、SQL或压测，没有修改运行代码。PASS只覆盖明确审查的范围，不等于全输入形式化证明。

## 阻塞与严重程度

不只是收益不稳定。当前至少存在一个工程验收缺项和一个实验对照缺项，另有在线适用范围限制。

1. **F1 / FAIL / P1：当前延迟批次模型缺少受支持的旧版本回滚流程。** 手册§10.4、阶段7通过条件要求可回退（IMPLEMENTATION_GUIDE.md:971）。`bandit/delayed_store.py:48`只加载head，`:83`更新版本/队列，`:111`冻结；没有旧revision恢复/切换入口及队列账本一致性协议。`bandit/store.py:72`的rollback只适用于旧Snapshot存储，不适用于带owners/evidence的BatchPolicy。`tests/unit/test_linucb.py:106`验证旧库回滚；`test_delayed_batch.py:71`仅验证发布失败时事务回滚，不能替代已提交模型版本回退。它不推翻v6重放，但不能宣称当前模型生命周期验收完成。
2. **F2 / FAIL / P1：预声明的额外收益门槛未通过。** `experiments/batches.py:46`要求99%区间上界<0且三个时间段同向，`:89`要求两基线都通过。v6对S1差-0.525ms、区间[-7.846,+6.826]，第二段+0.272ms；样本充足不等于收益充分。手册§17通用表述允许“固定策略或Native”，但当前已预声明更严格的双基线协议，不能看到结果后换门槛。
3. **F3 / FAIL / P2：当前完整批次奖励协议缺少L1统计策略对照。** 手册§10.4明确“固定策略、基于统计的策略选择、LinUCB”（IMPLEMENTATION_GUIDE.md:712）。`experiments/batches.py:9`只有S0、固定S1、ADAPTIVE；`:40`也只调度这些策略。历史单请求L1结果不能替代当前批次奖励/新SQL/时间段的可比对照。v6所有选择与“长度2选S0、长度8选S1”的简单规则相同，不能证明复杂模型有额外价值。简单长度规则对照是建议，L1对照是手册要求；二者不混为一项强制要求。
4. **BLOCKED：持续在线学习总体收益尚未证明。** `scripts/phase7_delayed_batch.py:222`入队发生在批次服务计时后，`:238`在下个批次之前同步消费，`:251`冻结后才留出。这是真实持久化延迟反馈，不是后台生产调度。入队47.804ms/批、更新142.774ms/批被单列，未纳入冻结推断收益。无数据支持把当前结果表述为持续在线训练的端到端收益。
5. **BLOCKED：广泛动态负载/跨会话外推。** 当前只覆盖单一COUNT族、有限同SQL长度2/8、不可变epoch、两档整机CPU。`bandit/episodes.py:197`使用caller声明长度选模型；v6没有同一长度因LOW/MEDIUM改变策略的结果。不能称作已验证开放流量的重复次数预测或IoTDB物理计划优化。不是“没有任何自适应”：长度本身确实影响选择。

## 是否有致命因素

在当前单连接、只读、不可变epoch的受控范围内，**未发现已证实的P0级数据错误、越权执行、训练污染或重放造假**；不作“绝对没有致命bug”保证。

若目标是不加限制地用于持续写入TSDB，则有根本适用性限制：`adapters/iotdb/result_cache.py:10`明确没有服务端失效协议。数据改变却继续复用同一epoch，缓存可能返回旧结果。当前实验通过人工不可变epoch规避，并未解决在线失效。这不是v6已经发生缓存污染的证据，却会使动态写入场景的正确性声明不成立。最小安全边界是无法保证不可变时禁用S1；真正支持写入需要另行审计失效机制或新增真实有效策略，不能猜IoTDB接口。

另一个研究限制是收益空间：长度8自适应与固定S1都选同一动作；长度2固定S1与S0均值只差约2–3ms，而自适应准备/选择多约1–3ms。降低噪声不保证存在足够的额外收益。不能承诺补齐工程缺项就必然通过F2。

## 逐项审查

| 检查项 | 结果 | 代码位置、证据与边界 |
|---|---|---|
| 阶段任务全部真实完成 | FAIL | LinUCB、平衡探索、冻结留出和延迟归因真实存在；F1/F3仍缺。`bandit/model.py:175`、`bandit/episodes.py:205`、`experiments/batches.py:33`、runner:246。 |
| TODO/pass/mock假实现 | PASS（限定范围） | 扫描src及阶段脚本未见用TODO或mock冒充真实成功。`model.py:272` online_advice返回None是显式部署锁；`parsing/sql_parser.py:1394`拒绝未实现metadata；`schemas/execution.py:137`与`strategies/models.py:141`的pass是合法分支；supervisor的pass是异常类型。真实LLM provider未实现，明确不可声称已集成。 |
| 字段、单位、时间戳/source | PASS | `schemas/common.py:26`拒绝naive时间并转UTC；`bandit/context.py:10`版本化17维编码、缩放/缺失位；`episodes.py:80`ns→ms、`:83`奖励→负秒；`orchestration/models.py:84`会话计时加和；`experiments/sequences.py:55`外层计时覆盖验证。v6成本对账900批通过。 |
| 异常是否被吞掉 | PASS（受审路径） | `delayed_store.py:42`回滚后重抛；`:96`只有质量AdmissionError记REJECTED，其他异常不ACK。`agents/learning_feedback.py:89`学习失败记warning/log保留查询结果；`tree_adapter.py:631`结构化查询错误、`:649`关闭异常warning。不同于伪造成功。对应发布失败、损坏反馈、读取/关闭失败测试。 |
| 未验证IoTDB API或SQL | PASS（2.0.6树模型范围） | `tree_adapter.py:580`调用已审计Native SDK；`:614`完整遍历；未知timeout=0语义明确UNKNOWN（:577）。`configs/strategies.yaml:32`认证S1仅外部精确SQL缓存，未把其他NOT_VERIFIED动作启用。真实连接与S1认证集成用例存在，v6两条查询CSV审计/MATCHED；不外推其他版本。 |
| client指标冒充DB指标 | PASS | `monitoring/local_db.py:25`检查DataNode/监听/namespace；`:51`独立DB_HOST采样；`:69`明确整机指标和active_queries UNKNOWN。`schemas/monitoring.py:200`约束source；`test_local_db_sampler.py:36`覆盖分源。不存在服务端纯执行时延已测量的结论。 |
| LLM绕过Registry/Validator | PASS（安全边界）；真实LLM效果BLOCKED（可选） | `decision/llm_advisor.py:16`extra=forbid，仅strategy_id；建议从已验证候选筛选、超时/异常无建议；`decision/engine.py:619`再拒绝无效候选；Executor再次校验。`test_llm_advisor.py:34/44/54`覆盖SQL注入式多字段、未知ID、超时、隔离。默认无transport、关闭；本地模拟transport不是实际LLM质量证据。无LLM路线不因此被否决。 |
| 错误样本用于学习 | PASS | `schemas/learning.py:130`非VALID无reward，MISMATCH/timeout不能VALID；`model.py:221`重校验资格与上下文；`episodes.py:141`完整批次准入、HOLDOUT拒绝，`:205`每批只更新真实allocated arm。`test_delayed_batch.py:84/97/157/164`覆盖留出、低质量、错误奖励通道与损坏队列。v6 4500 VALID/MATCHED；未观测错误样本正向更新。 |
| 普通路径EXPLAIN ANALYZE | PASS | `tree_adapter.py:523`诊断接口只EXPLAIN；Orchestrator无EXPLAIN ANALYZE调用。`test_tree_adapter.py:527`明确断言不调用ANALYZE；普通查询计数集成测试`test_orchestrator_live.py:16`。 |
| 重复执行/缓存污染/未消费结果 | PASS（不可变epoch范围） | `execution/fallback.py:57`超时不重试；Native失败不重复自身（:24）；`test_tree_adapter.py:294`禁止SDK重连重发。`:614`消耗全部结果，`:645`finally关闭，缓存超限不截断存入（:660）。`scripts/phase7_delayed_batch.py:176`每批清缓存，序列校验归因；真实缓存认证覆盖等价和来源。动态写入失效仍BLOCKED，见上文。 |
| 测试真实执行/失败路径 | PASS（已有覆盖）；回滚用例缺口FAIL | `console.log:4`154 passed in9.29s，`:384`942 passed in43.14s；staged入口:34显式IOTDB_INTEGRATION=1。`test_executor.py:209/226/261`超时/Native/验证失败，`test_delayed_batch.py:71/164`原子失败/损坏，`test_learning_review_regressions.py:9/60`错结果隔离及持久化失败。942条不是942次真实数据库测试；单元模拟与真实集成区分。现有测试未覆盖F1不存在的旧版本恢复流程。 |

测试证据来自用户日志；本次没有运行测试来补足缺项。源代码快照核验88文件在结果文档更新前一致，见`docs/evidence/phase7_manual_v6_audit_20261103.json`。该运行快照不包含所有测试文件hash，日志也没有逐条nodeid，不能把它说成完整测试供应链认证。

## 阶段7任务核对

- PASS：真实样本门槛、两个认证策略、真实A/b更新；17维编码和配置版本；定种子平衡探索；新SQL/时间留出、冻结与独立重放。证据为v6 360训练/540留出批及model_reproduced=true。
- FAIL：当前BatchPolicy的版本回退闭环；必须补齐F1。
- FAIL：当前奖励口径L1统计对照；必须补齐F3。学习曲线/累计收益未在v6报告形成独立交付，也应补齐；若regret无真实可比定义，明确UNKNOWN，不造未执行arm标签。
- BLOCKED（可选，不否决无LLM路线）：真实provider、实际LLM开关质量/延迟对照。安全适配接口和模拟失败测试PASS，不等于provider集成完成。
- FAIL：当前预声明可重复额外收益F2。代码里stage_accepted=False是部署锁，不是报告失败的数值根因；不能改成True修复收益问题。

## 最小修复清单

1. F1：为批次模型增加受控恢复/回退协议，校验revision、checksum、scope、reward版本，CAS校验当前head；选择独立推断head或隔离恢复分支，明确owners/evidence及PENDING/APPLIED处理。不能只UPDATE head然后重放全部队列。测试旧revision预测恢复、错误scope/损坏/过期head拒绝、原子失败、冻结状态、后续消费不重复归因。保留原证据和版本。
2. F3：新增同批次服务奖励的L1统计决策对照，使用真实合格TRAIN批构造，冻结后不更新；和现有策略同预算、顺序随机、全成本计时。新协议/新留出，不能用v6测试结果拟合。学习曲线从已有TRAIN更新按时间重建；额外简单规则对照只作合理建议。
3. F2：先分解准备/选择中的构造、初始化、指纹、预测耗时；仅移除已证明的重复计算，不能扣掉真实成本。之后由用户执行预声明新验证。若相对S1差异仍不成立，保留FAIL并如实收窄研究结论，不重复试至显著。

持续在线调度、动态数据失效与其他SQL族不能在这次静态审查中被算作已完成；它们是部署/外推BLOCKED，并非要求本轮提前实现整个系统。当前最合理顺序是补齐F1和F3设计与验证，再判断F2是否有足够收益空间。
