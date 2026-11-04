# 阶段 7：完整请求序列验证

2026-11-03，实施前协议。生产 Bandit 关闭，不进入阶段 8。

目的：验证现有单请求奖励模型在真正从空缓存开始的连续请求中是否支付填充成本并复用结果。缓存填充改变后续状态，单步最优不能直接视为序列最优。本轮不根据留出结果调参。

使用既有自然 LOW 小规模 v8 训练证据；独立分析器须完整重放通过，模型冻结，仅使用训练部分参数。留出为 small_scale COUNT 文件第 7 条，禁止与来源训练/留出 SQL 相同。固定不可变 epoch heavy-complete-20261102-v2；真实源数据与 Native 结果核验保留。每序列重新建立策略引擎并清空客户端缓存，不清除 OS/IoTDB 缓存。

预声明 30 配对块，每块长度 1、2、8 的三种序列，分别运行 S0、固定 S1、冻结 ADAPTIVE；seed=20261106 随机排列长度和策略。总计 990 正式请求；首次请求由原规则降级 S0，第二次及以后才允许 S1。无额外预热/填充请求。每个序列总成本是其所有 Orchestrator.run 完整耗时之和，包括同步 L1、持久化和安全回退；原始每次请求均保存。实验初始化、独立 Native 参考、外部审计序列化另列，不混成用户请求耗时。

按分配策略分析，禁止删除成功但低质量/安全回退的成本。这些请求仍不能进入 Bandit 学习。真实失败、未知服务端完成状态、结果不一致、资源越限立即停止，保留失败终态，不自动重试；残缺实验不输出正式收益区间。每序列 60 秒进程组监护，单查询 30 秒超时，单连接，不启用人工 CPU 加压。维持 500ms TTL、250ms 请求快照年龄和 0.5s 有界等待。

每长度每比较使用 30 配对总耗时差，4000 次 bootstrap，点态 99% 区间，seed=20261106；连续三个 10 块均值同向才报告单会话更快。比较固定 S1−S0、ADAPTIVE−S0、ADAPTIVE−S1。全部请求纳入，包括首次访问/填充；不能把不同长度或缓存状态的收益拼接。多重比较、串行相关、单会话和自然负载为限制。

本实验只解除“完整序列未经验证”缺口，无论结果如何均不能独自满足原多负载、独立复现及生产在线部署门槛。若 EMPTY 模型持续选择 S0 导致永不填充，报告实际行为，不使用未来重复次数偷偷指导当前模型，也不把选择变化当成收益。

## v1 失败与 v2 预声明

`data/phase7_sequence_20261103` 在 block18、length8、ADAPTIVE 请求间检查中止。被拒绝 DB 快照 captured_at=03:18:10.327139Z、CPU=4.366%、内存=65.4%，最后请求返回03:18:10.829757Z、耗时524.630246ms。数值未越限，快照已过500ms；前置检查未调用既有新鲜度等待。现场停顿归因 UNKNOWN，不从不完整批次提取正式收益。

v2 把资源准入和既有0.5s有界等待纳入请求计时，随后调用 Orchestrator（仍自行重新验证快照）。每次保存准入快照/观测时间，分析器校验时间包络、来源、250ms年龄和500ms TTL。未降低安全或质量门槛。新批次 `data/phase7_sequence_fresh_20261103` 使用第8条留出SQL，仍30块/990请求，其余顺序/统计计划相同；不复用失败批次结果或更新冻结模型。

## L1 扫描开销修复与 v3 预声明

v2 全部990请求成功/VALID，冻结模型重放通过。trace 的 LEARNED 区间均值244.168ms；三个连续批次全请求均值134.173/319.434/476.335ms，存在明显随历史增长的开销。本轮以只读SQLite中最大的810条真实经验组定位：旧128条LRU顺序扫描三遍，2430 miss、0 hit，各173.909/171.854/173.595ms。

修复保留完整Pydantic校验和内容失效，将键替换为SHA-256摘要，只保留不可变统计字段，最多4096条、4MiB估计字段/容器预算（不声称进程RSS精确上界），超大body仍不缓存。真实组首遍218.663ms，后两遍23.111/22.710ms；紧凑缓存739896 bytes。原L1完整统计、所有sample IDs和revision逐字段一致。这是组件重放，不是查询端到端加速结论，首次散列/校验成本保留。

预声明 v3：`data/phase7_sequence_compact_20261103` 使用第9条留出SQL，仍30块/990请求，原冻结训练模型/随机顺序/统计门槛不变。用新批次验证修复后的实际请求行为；不将v2与v3的不同SQL及不同时段均值作为受控因果加速比。生产Bandit仍关闭，L1统计公式、奖励及样本门槛不变。

## 最终实测与判断

v2和v3均完整执行270序列/990请求；全部VALID/MATCHED、自然LOW、无真实fallback。合成失败注入另行覆盖fallback，不伪称实测发生过。每长度每策略30序列；模型只取此前经完整审计的训练参数，留出请求不更新Bandit。同步L1仍记录/更新，成本已计入。

| v3序列长度 | S0总耗时均值ms | 固定S1总耗时均值ms | ADAPTIVE总耗时均值ms | 结论 |
|---|---:|---:|---:|---|
| 1 | 137.463 | 138.117 | 142.356 | 全部首次访问S0，无稳定差异 |
| 2 | 258.413 | 275.750 | 265.153 | S1包含填充，无后续命中；无稳定差异 |
| 8 | 1174.276 | 690.975 | 1056.690 | 固定S1稳定更快；当前自适应未通过 |

长度8：固定S1−S0平均−483.301ms，99%区间[-778.763,-312.650]，三批次同向；ADAPTIVE−固定S1平均+365.715ms，[279.794,457.351]，三批次同向更慢。固定S1为30次首次Native+210次S1（其中180命中）；自适应240次全部S0、零命中。不能把随机噪声下相对Native的均值差异宣称稳定收益。短序列不能证明S0最快。

v3的LEARNED trace区间均值70.626ms，v2为244.168ms；两批次不同SQL/时段，不构成端到端受控加速比。可信的缓存修复因果证据是同810条真实经验的组件重放及L1逐字段相同。完整统计构建仍遍历所有样本，超过4096条或4MiB工作集仍可能淘汰；未声称解决无限历史扩展问题。

当前冻结模型的EMPTY分区几乎总选择S0，无法进入READY分区享受已学到的命中收益。实测支持“该冻结单请求模型不适用于这些完整序列”，不支持“所有LinUCB都失败”或“改成任意序列奖励即可通过”。v3修复改变运行成本，旧模型未针对新成本重新训练；它是有意保留的冻结回归对照，不能代表未来重新训练后的最优效果。

### 多负载预检

`phase7_sequence_load_pilot_20261103` 实际执行exit0，独立分析exit0：LOW/MEDIUM各两轮，每轮20样本全部达到原稳定性要求。两轮LOW的CPU范围1.168–7.779%、0.965–15.070%；两轮MEDIUM为51.930–63.705%、53.492–58.404%。共租CPU设定55%，仍80% CPU/90%内存停止、30秒自行到期、60秒进程组监护。未改IoTDB配置。该预检没有训练/留出查询，所以不能把环境可行性PASS写成多负载模型验收PASS。

### 阻塞清单

| 项目 | 结果 | 尚缺内容 |
|---|---|---|
| 首次访问/填充/重复序列完整记账 | PASS | 泛化到其他SQL/缓存容量仍未验证 |
| 旧快照请求间直接中止问题 | FIXED | 严格等待超时仍会安全中止，不能保证服务永不抖动 |
| 超过128条时L1重复解析瓶颈 | FIXED | 更大工作集、全历史统计/版本保存仍有扩展成本 |
| 两档负载环境有界预检 | PASS | 修复后完整多负载训练/留出及独立复现 |
| 当前冻结自适应优于固定S1 | FAIL | 先填充后复用的反馈归因、适用场景与重新训练 |
| 生产E2E延迟反馈更新 | NOT IMPLEMENTED | 生产L1仍为Adapter目标；不能在请求完成前伪造总耗时 |
| 阶段7总验收/部署 | NOT ACCEPTED / NOT READY | 不进入阶段8，不启用Bandit |

下一步需预先定义有真实边界的请求序列/复用窗口、延迟反馈与成本归因；不能把未知未来请求数输入单次决策，不能将未发生的缓存命中写成reward。若仍采用单请求Bandit，必须明确填充动作对后续状态的影响并设计受控探索；若采用序列策略，需新增版本化协议、真实序列训练样本及独立留出，不能重用本轮留出调参后宣称独立通过。当前没有足够证据选择并部署新的学习方案，不能靠打开配置或改阈值完成阶段验收。

## 实际命令与测试

全部Python命令使用Conda环境MANTO。以下真实执行均exit0（v1中止另列），输出目录须不存在。

```bash
PYTHONPATH=src:scripts conda run --no-capture-output -n MANTO python scripts/phase7_sequence_e2e.py \
  --output data/phase7_sequence_compact_20261103 \
  --training-journal data/phase7_reward_small_ambient_20261103/journal.jsonl \
  --local-db-pid 3879777 --immutable-epoch-confirmed

PYTHONPATH=src:scripts conda run -n MANTO python scripts/phase7_sequence_e2e.py \
  --analyze data/phase7_sequence_compact_20261103/journal.jsonl \
  --output docs/evidence/phase7_sequence_compact_20261103.json

PYTHONPATH=src conda run --no-capture-output -n MANTO python scripts/phase7_controlled_e2e.py \
  --output data/phase7_sequence_load_pilot_20261103 --epoch heavy-complete-20261102-v2 \
  --local-db-pid 3879777 --immutable-epoch-confirmed --cpu-cotenant-authorized \
  --pilot-only --background-snapshots --wal-full --e2e-reward --fresh-snapshot-wait --cpu-target-percent 55

PYTHONPATH=src conda run -n MANTO python scripts/phase7_analyze_controlled_e2e.py \
  data/phase7_sequence_load_pilot_20261103/journal.jsonl \
  --output docs/evidence/phase7_sequence_load_pilot_20261103.json

PYTHONPATH=src conda run -n MANTO python scripts/phase7_profile_fact_cache.py \
  --database data/phase7_sequence_fresh_20261103/study.sqlite \
  --output docs/evidence/phase7_sequence_fact_cache_replay.json

IOTDB_INTEGRATION=1 \
IOTDB_TEST_SQL='SELECT s_355 FROM root.small_scale.g_0.d_1 WHERE time >= 1640968700000 AND time < 1640978355000 LIMIT 5' \
conda run --no-capture-output -n MANTO python -m pytest -q

conda run -n MANTO python -m pytest -q docs/evidence/phase7_sequence_review_probes.py
conda run -n MANTO python -m compileall -q src scripts tests docs/evidence/phase7_sequence_review_probes.py
```

- 最终全量：**900 passed in42.29s**，含13项真实集成，无skipped。
- 两批独立重放及篡改拒绝：**20 passed in79.35s**。删除首条请求、漏整序列、计时/模型/缓存来源/准入/TTL/终态/SQL行号篡改均拒绝。
- 缓存与学习专项：`conda run -n MANTO python -m pytest -q tests/unit/test_overhead_learning.py tests/unit/test_learning.py tests/unit/test_learning_review_regressions.py tests/unit/test_sequence_e2e.py`，**83 passed in10.57s**；后续序列/缓存专项19 passed in1.08s。上述用例重叠，不累计独立覆盖。
- 首轮新增测试曾1 failed/43 passed：合成统计fixture漏了policy字段，补全后44 passed；没有删除断言或绕过业务验证。
- compileall exit0；两份最终SQLite `integrity_check=ok`。
- v1真实实验exit1，613条请求原样保留，未输出正式收益区间。v2/v3各自独立分析，不合并为同一批。

## 改动与证据

业务修改仅`src/tsdb_optimizer/learning/repository.py`的事实缓存；新增`experiments/sequences.py`、`scripts/phase7_sequence_e2e.py`、`scripts/phase7_profile_fact_cache.py`及相应测试。其余为AGENTS.md、README、环境/状态/学习设计与本阶段报告更新。未改Registry、Validator、IoTDB源码、奖励公式、数据库schema或生产Bandit配置。

最终结果：[v3完整序列](evidence/phase7_sequence_compact_20261103.json)、[v2重放](evidence/phase7_sequence_v2_reviewed_20261103.json)、[多负载预检](evidence/phase7_sequence_load_pilot_20261103.json)、[缓存组件重放](evidence/phase7_sequence_fact_cache_replay.json)、[原L1统计快照](evidence/phase7_sequence_l1_before.json)、[源码与SQLite摘要](evidence/phase7_sequence_artifact_manifest_20261103.json)。

v3所有运行Python文件与启动摘要一致。v2后`learning/repository.py`做缓存修复，序列脚本增加v3及更严格来源检查；保留v2原摘要，不宣称旧实验运行了新实现。文档后续追加结果不要求与预声明时摘要相同。工作区原有大量未跟踪文件，本轮未将整仓旧内容打包提交。
