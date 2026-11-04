# 实现状态

## 当前阶段与范围

日期：2026-11-02。依用户明确授权实现 **阶段 5：Agent 4 可靠经验与 L1 结构化统计**。
状态：**ACCEPTED（阶段 5 L0/L1 库接口，严格复审通过）**。未进入阶段 6。
修复前严格审查为 NOT ACCEPTED（原有 646 passed，独立反例 3 failed），报告保留在
[PHASE5_REVIEW.md](PHASE5_REVIEW.md)。本轮三个反例全部通过，正式套件纳入 28 个
回归用例。本轮严格复审完整真实套件 674 passed（20.69s），独立检查 6 passed
（1.24s）；原三个 FAIL 已关闭。验收范围和保留限制见最新复审报告。

阶段 4 的原始任务表、修复过程与历史测试原样归档至
[PHASE4_IMPLEMENTATION_STATUS.md](PHASE4_IMPLEMENTATION_STATUS.md)，其中的“未授权阶段 5”
与待复审字样只描述当时状态，不是本轮状态。阶段 4 策略认证范围没有扩大。

| 项目 | 当前值 |
|---|---|
| 环境 | Conda MANTO，Python 3.12.13，apache-iotdb 2.0.6；IoTDB 2.0.6 tree |
| 已认证策略 | S0_NATIVE、固定 epoch 下 S1_REPEAT_CACHE；CAND_* 仍禁用 |
| 学习等级 | L0 可靠记录 + L1 分组统计；无 Bandit/向量数据库/LLM 学习 |
| SQLite | v7，v1–v6 增量迁移；保留阶段 4 审计表与记录 |
| 普通查询 | 单候选执行；NOT_CHECKED 正常；不为学习双执行或自动 EXPLAIN ANALYZE |
| 接入边界 | Agent 4 observe/recommend 显式库接口；不接管 CLI，不实现阶段 6 Orchestrator |
| DB 指标 | 无真实代理，UNKNOWN；不使用客户端 psutil 冒充 DB 指标 |
| Git | 未提交；没有修改 IoTDB 源码 |

## 阶段 5 任务与证据

| 要求 | 实现与验证 |
|---|---|
| 1. experience schema / repository | schemas/learning.py、learning/schema.py、learning/repository.py；真实 executions 外键及唯一 execution ID |
| 2. 查询/决策/执行/策略/模型版本关联 | Experience 保存实际 IDs、策略版本、与持久化 decision 精确一致的模型版本（含 None）；模型保存源 execution IDs；v7 迁移保留旧审计记录 |
| 3. VALID/LOW_CONFIDENCE/INVALID | Agent 4 按执行时刻评估所有有效来源的版本化资源阈值；质量版本 2 和策略指纹参与训练准入；NOT_CHECKED 可 VALID；失败和低可信 reward 为 null |
| 4. 经验检索 | 模板、具体 QueryFeatures、服务器身份/版本、数据模型、epoch/规模、缓存/fetch/source、实际结果行数、负载来源/主机/分桶严格隔离；策略版本单独检索 |
| 5. 结构化统计 | 每策略 observed/valid_count、均值/中位数 ms、总体方差 ms²、成功率、mean_reward；无有效样本不填假 0 |
| 6. 固定奖励 | negative-log1p-ms-v1：-log1p(client_wall_ms/1000.0)，只对真实 VALID 执行计算 |
| 7. 最小样本与变更门槛 | 默认 min_samples=3、严格中位数改善 >5%；参照 Native 也须合格；未知/不足返回 None |
| 8. 版本与原子保存 | SQLite 单事务发布不可变 model_version + head；每 group revision 检查；保留旧版本且拒绝 stale 模型 |
| 9. 学习故障不改变查询 | 写经验/模型异常记日志和 feedback warning，run 保持不变，不重执行；检索故障无建议 |
| 10. 模拟和真实验证 | 64 个学习相关单元用例；真实 ExecutionRecord 集成；实际 q4/q5 Benchmark 原始 JSON |
| MISMATCHED 隔离 | 无 I/O 核验真实 attempt 后先内存隔离，再读写存储；持久化失败返回 pending，可重试；持久化 ID/版本隔离，撤销该版本所有 context 历史 reward；重启绑定恢复、旧决策禁止执行 |

阶段 5 十项任务及指南四项通过条件在本轮严格复审中通过。当前证据包括：

- 有效经验能检索并影响后续 Agent 2：模拟 LOW 组偏向快速策略，MEDIUM 组保留 Native；建议仍经过 Registry/Validator。
- 错误样本不污染 reward：失败、超时、fallback、未支持模板、上下文未知、未认证和 MISMATCHED 用例。
- 数据库/策略版本隔离：严格 context key、版本过滤与持久化 quarantine 测试。
- 模型更新失败不影响查询：写库、publish、检索故障和原子回滚用例；没有增加 Adapter 调用。

## 改动文件

新增：

- src/tsdb_optimizer/schemas/learning.py
- src/tsdb_optimizer/learning/__init__.py、schema.py、repository.py
- src/tsdb_optimizer/agents/learning_feedback.py
- tests/unit/test_learning.py
- tests/unit/test_learning_review_regressions.py（本轮 28 个正式回归）
- tests/integration/test_learning_feedback.py
- scripts/phase5_learning_evidence.py
- docs/LEARNING_DESIGN.md
- docs/evidence/phase5_learning_2026-11-02.json（保留初版历史）
- docs/evidence/phase5_learning_fixes_2026-11-02.json（本轮真实证据）
- docs/PHASE4_IMPLEMENTATION_STATUS.md（原状态文档归档）

修改：

- src/tsdb_optimizer/storage/sqlite.py：schema v7 增量迁移。
- src/tsdb_optimizer/strategies/registry.py：运行时和持久化策略版本隔离检查。
- tests/unit/test_storage.py：保留原测试，仅将当前 SQLite 版本断言更新为 7。
- README.md、docs/IMPLEMENTATION_STATUS.md、docs/STRATEGY_CATALOG.md、
  docs/METRICS_DICTIONARY.md、docs/evidence/README.md：当前阶段、协议、证据和边界。

## 实际命令与结果

以下 python 均为 `/opt/anaconda3/envs/MANTO/bin/python`。没有使用可选 venv，
也没有改变共享 Python 包。最终结果为实际运行输出（并行套件的耗时不用于性能评估）：

| 命令 | 实际结果 |
|---|---|
| PYTHONPATH=src python -m pytest -q tests/unit/test_learning.py tests/unit/test_learning_review_regressions.py | 64 passed in 12.94s |
| PYTHONPATH=src:tests/unit python -m pytest -q docs/evidence/phase5_review_probes.py | 3 passed in 0.81s（原三个 FAIL） |
| IOTDB_INTEGRATION=1 PYTHONPATH=src python -m pytest -q | 674 passed in 23.35s，包含 12 个真实集成用例 |
| PYTHONPATH=src python -m compileall -q src tests scripts | exit 0 |

真实 Benchmark：

```bash
PYTHONPATH=src python scripts/phase5_learning_evidence.py \
  --dataset-epoch phase5-static-20261102 --repeats 4 \
  > docs/evidence/phase5_learning_fixes_2026-11-02.json
```

实际 exit 0。全部新经验 quality_version=2，并记录资源策略版本与指纹。q4/q5 各 12 条观测，共 24 条；测量请求 dispatch 16 次，另有 2 次
未计奖励的预热对照以及 doctor 探测。24 条全部与实际完整 Native 结果一致。
12 VALID（q4），12 LOW_CONFIDENCE（q5，UNSUPPORTED_TEMPLATE），后者无 reward。
真实客户端负载为 LOW，4 个严格统计 group；没有声称测得 DB 负载。

| q4 分组 | 策略 | VALID n | 中位 ms | 总体方差 ms² |
|---|---|---:|---:|---:|
| LOW / SERVER | S0_NATIVE | 4 | 6.6270 | 0.1402266875 |
| LOW / SERVER | S1_REPEAT_CACHE miss | 4 | 6.6830 | 1.131828 |
| LOW / CACHE_HIT | S1_REPEAT_CACHE hit | 4 | 0.0870 | 0.00033475 |

SERVER 候选中位数未优于 Native；hit 无同组 Native 参照，不产生跨来源建议。
这次实验验证记录与过滤/统计，不构成生产加速或在线持续学习有效性的证明。
原始 JSON 保留上下文、源 IDs、快照、质量原因与奖励；详细设计见 LEARNING_DESIGN.md。

## 已知限制与未完成事项

- CLI 不自动调用 Agent 4；生命周期调度、四 Agent 闭环属于未授权的阶段 6。
- DB_HOST/DB_METRICS 无真实来源，真实 DB 负载统计 **BLOCKED/UNKNOWN**；分负载不同决策目前由明确模拟数据验证。
- 只对已支持模板、exact SQL 不改写契约训练；q5 可执行但不能冒称解析模板已验证。
- dataset epoch、cache condition 和 server identity 依赖调用方真实声明；规模未知保留 null。
- 全历史统计，无漂移检测/衰减/在线探索；min_samples 和 5% 门槛不是统计显著性检验。
- 只有绑定 learner 的 Registry 读取隔离库；写库故障时不能保证跨重启隔离已持久化。
- S1 固定 epoch、进程内、无并发 single-flight 等旧限制仍保留；学习库上下文隔离不等于修复 S1 缓存边界。

阶段 5 本轮复审通过；当前未进入阶段 6，后续阶段须另行指定任务。

## 本轮最小修复清单

- F-1：learning_feedback.py 拆出无 I/O _validated_attempt，确认 MISMATCHED 后先
  内存隔离；读取/写入故障均保留阻断。LearningFeedback 增加 pending 标记，恢复后
  用原 run/record 重试审计；不重执行查询，不伪称跨重启已持久化。
- F-2：复用 ResourcePolicy，对所有有效来源按执行开始时间评估；阈值来源不由
  load_source 选择。Experience/L1Model 增加质量版本和策略指纹，旧准入经验保留
  审计但不用于当前 reward/延迟统计，旧模型/不同策略指纹模型不再提供建议。
- F-3：repository._verify_execution 在同一事务中核对 decisions.model_version
  与 experience.decision_model_version，不一致拒绝写入。
- 主要生产改动为 agents/learning_feedback.py、schemas/learning.py、
  learning/repository.py；SQLite 仍为 v7，无 IoTDB 源码改动。
- 新增测试覆盖三个反例、读写故障恢复、身份不一致、三来源×三指标、自定义阈值
  与等号边界、过期/未来/未知状态、None/外部模型版本、旧准入与策略指纹隔离。

## 最新验收复核

完整记录见 PHASE5_REVIEW.md 最后一节：逐项 PASS，真实 DB 负载证据 BLOCKED/UNKNOWN。
独立证据 docs/evidence/phase5_rereview_probes.py 检查并发反馈幂等、隔离后旧模型
不能重新发布，以及从实际 Benchmark ExecutionRecord 重算所有分组统计。
本轮只新增审查证据与更新文档，没有修改生产实现。
