# 阶段7：完整请求奖励与策略适用场景

日期：2026-11-03。本轮不进入阶段8，不启用生产Bandit。

## 实验前声明

奖励版本 `negative-e2e-seconds-v1`，公式 `-end_to_end_ms/1000`，与最小化平均完整请求延迟同目标。使用客户端 `perf_counter_ns` 围绕 Orchestrator.run，包含解析、监控读取、决策、执行/完整消费、同步L1学习、trace提交、请求存储连接关闭。请求完成后才能构造反馈；离线模型拟合和实验审计序列化单独处理，不算在线更新成本。不存在能在当前请求结束前知道其完整耗时的假设。

CompletedRequest绑定UTC起止、单调计时、query/decision/execution ID、完整工作流和经验摘要。拒绝警告、失败/回退、过期/不合格经验、计时不足、UTC与单调时钟明显不一致、留出样本越界；继续调用原策略认证/资源/质量闸门。旧Experience/L1的Adapter奖励只作历史兼容与诊断；新实验决策仅使用完整请求模型，分区及reward_version隔离。生产在线L1默认目标未改名成E2E，生产Bandit仍关闭。

预声明两个独立工作负载：`small_scale` 与 `large_scale` 的 `q03_count_aggregation.sql`。各自第1条训练、第5条留出，训练不跨规模混合。只测试已审计COUNT子集，不声称覆盖所有SQL类型。两种负载LOW/MEDIUM、两种客户端缓存状态EMPTY/READY；每工作负载6轮×4场景×最多20训练请求，留出固定36块×4场景×三策略。实际每负载/缓存/策略至少30样本；不事后追加补齐。策略S0/S1经原Registry/Validator；不新增SQL改写。

保留原CPU安全界限：每轮30秒自行到期、设定55%、授权目标50%–70%、CPU>80%或内存>90%停止；单查询连接、查询30秒超时、每轮进程组60秒监护。CPU共租是整机竞争，不是IoTDB进程/查询CPU。

每场景配对差值 S1−S0，4000次bootstrap、固定seed20261105、点态99%区间；三个连续等长批次方向一致才标记单会话更快。多个场景属探索性适用范围，不是同时置信或部署认证。自适应仍须优于两个真实固定策略，沿用原预声明99%收益门槛。36块中的原前三个10块连续复现条件保留，整体区间使用全部块。

READY明确预热S1完整结果，成本单独记录且不计单次已就绪查询；不能据此声称包含填充成本的总体工作负载收益。EMPTY每次清除客户端缓存，仍保留已访问资格；IoTDB内部/OS缓存未清除，不能称数据库冷缓存。首次请求、无immutable epoch、未知不可缓存语法等安全适用性继续由既有测试验证。

预声明目录：`data/phase7_reward_small_20261103`、`data/phase7_reward_large_20261103`。失败批次保留，不从失败部分筛选收益。

## 结果

已完成小规模v8与大规模v9的自然LOW适用性实验；详见后续实测章节。多负载/部署仍 NOT ACCEPTED / NOT READY。

## 训练期时效失败与修复后的预声明

首批 `phase7_reward_small_20261103` 在训练期间中止，未执行任何正式留出对照（准备阶段Native参考查询另计）。原日志保留：DB captured_at=02:11:57.314090Z，执行开始02:11:57.817833Z，超过500ms；客户端captured_at=02:11:57.710881Z。候选S1预执行检查失败，仅Native请求成功，LOW_CONFIDENCE/FALLBACK_CONDITIONED_SAMPLE/LOAD_CONTEXT_UNAVAILABLE_OR_MISMATCHED被新奖励拒绝。不得从已完成训练片段提取收益结论。

事后4次独立身份核验8.066/7.000/6.954/7.013ms；不能据此确定该次397ms发布间隔的原因，实际调度归因UNKNOWN。不修改或省略身份核验。[psutil文档](https://psutil.io/api/)及[Linux proc TCP说明](https://kernel.org/doc/html/v5.12/networking/proc_net_tcp.html)仅用于核实探测接口；未引入新的proc解析器。

修复为显式有界新鲜度等待（controlled-e2e-v7）：后台最新样本必须两来源可用且年龄≤250ms；否则在请求内最多等待0.5s，仍不采硬件、不改时间戳，超时明确异常并走既有保守路径。等待发生于Orchestrator内部，包含在E2E和新奖励中。默认latest行为兼容，只有显式参数启用。原500ms TTL、CPU保护、结果检查、样本数与统计门槛不变。

重新声明完整小规模批次目录 `data/phase7_reward_small_fresh_20261103`，大规模目录仍 `data/phase7_reward_large_20261103`；两者均加 `--fresh-snapshot-wait`。SQL、固定样本计划与分析阈值不变，不复用失败批次的训练或模型。修复由训练期时效故障触发，不依据留出收益调参。

第二批 `phase7_reward_small_fresh_20261103` 在MEDIUM pilot被资源保护中止，0条训练/留出。CPU控制器末值79.9%；独立sampler触发unsafe/unavailable，但旧异常未记录该次sample，精确触发指标UNKNOWN。补充resource_rejected原始样本日志，不降低保护阈值。此后本轮停止人工CPU加压，不反复重跑争取通过。

为完成可安全开展的策略适用性验证，预声明 **controlled-e2e-v8 / AMBIENT_STRATEGY_APPLICABILITY_ONLY**：仅当前自然LOW负载，2个独立pilot仍各20样本，训练6轮×2缓存状态×最多20，留出36块×2缓存状态×三策略。仍要求每个实际单元至少30条，不丢弃错档/失败样本。采样与新奖励采用v7有界等待。目录为 `data/phase7_reward_small_ambient_20261103`、`data/phase7_reward_large_ambient_20261103`，SQL仍第1/5条。

这是范围受限的新适用性实验，**不是放宽原多负载验收条件**：分析器强制qualification_complete=false、within_session_benefit_gate=false、online_ready=false，单独报告ambient_scope_complete。未测MEDIUM单元保持UNKNOWN，阶段7总验收仍NOT ACCEPTED。不复用此前两批训练，CPU共租全部关闭；该范围只能给出自然低负载下的条件结论。

## 复现命令（自然负载适用性范围）

输出目录必须不存在；不能覆盖或追加历史实验。大规模只把scale与output改为large_scale / phase7_reward_large_ambient_20261103。

```bash
PYTHONPATH=src /opt/anaconda3/envs/MANTO/bin/python scripts/phase7_controlled_e2e.py \
  --output data/phase7_reward_small_ambient_20261103 \
  --epoch heavy-complete-20261102-v2 --local-db-pid 3879777 \
  --immutable-epoch-confirmed --cpu-cotenant-authorized \
  --train-rounds 6 --test-blocks 36 --background-snapshots --wal-full \
  --e2e-reward --fresh-snapshot-wait --ambient-only \
  --scale small_scale --test-query-line 5 --cpu-target-percent 55
```

`--ambient-only`覆盖人工CPU负载启用，保留的55设定点不执行；cpu_load_enabled=false，cpu_source=NATURAL_HOST_ACTIVITY，cpu_control必须为空。授权flag只是已有授权声明，不表示实际启动了负载。每个输出目录implementation.json记录启动时运行源码与预声明文档摘要；文档后续追加结果会改变文档摘要，原预声明摘要保留，运行源码需逐项一致。

```bash
PYTHONPATH=src /opt/anaconda3/envs/MANTO/bin/python scripts/phase7_analyze_controlled_e2e.py \
  data/phase7_reward_small_ambient_20261103/journal.jsonl \
  --output docs/evidence/phase7_reward_small_20261103.json
```

分析器独立重放随机分配、校验receipt与完整工作流/经验摘要、原有准入、时间隔离、模型参数及实际执行策略；拒绝混用奖励或改写单个耗时。所有收益由真实对照给出，不生成未执行策略的反事实耗时。

## 小规模真实结果（v8）

实验命令exit 0；独立分析exit 0。240条训练、216条留出全部通过原质量闸门及CompletedRequest核验。训练四单元55/65/63/57条；留出每缓存状态×策略各36条。456个执行前观测均为LOW；DB快照在dispatch最大259.980ms，client最大222.561ms。模型参数独立重建一致，实际选择EMPTY→S0共36次、READY→S1共36次。

| 缓存状态 | S0均值ms | 固定S1均值ms | 自适应均值ms | S1−S0配对99%区间ms | 适用性结论 |
|---|---:|---:|---:|---|---|
| EMPTY | 101.529 | 92.654 | 91.580 | [-37.515, 13.157] | 无稳定差异；不能宣称S0更快 |
| READY | 86.695 | 58.966 | 57.948 | [-52.804, -12.236] | S1单会话稳定更快，三个批次同向 |

READY固定S1与自适应各36次真实缓存命中，S0为0次。EMPTY三策略均0次命中，S1真实访问数据库并填充客户端缓存。总体均值S0 94.112ms、固定S1 75.810ms、自适应74.764ms，仅作当前范围的描述，不能证明自适应稳定优于固定S1；原多负载收益门槛未满足。报告`ambient_scope_complete=true`，但`qualification_complete=false`、`within_session_benefit_gate=false`、`stage_accepted=false`。

当前模型选择S0用于EMPTY是训练后的偏好，留出CI没有证明S0优势。不得把选择行为当作独立收益证据。小规模证据：`docs/evidence/phase7_reward_small_20261103.json`。

## 修改文件与测试范围

- `src/tsdb_optimizer/bandit/completed_request.py`：完整请求Pydantic反馈、时间/身份/摘要/质量核验。
- `src/tsdb_optimizer/bandit/model.py`：奖励版本隔离、完整请求准入、线性E2E目标。
- `src/tsdb_optimizer/experiments/controlled_policy.py`：独立训练、完整receipt绑定及冻结重放。
- `src/tsdb_optimizer/experiments/scenarios.py`：预声明缓存场景配对区间，未测场景UNKNOWN。
- `src/tsdb_optimizer/agents/performance_monitor.py`：可选有界新鲜度等待，计入完整请求。
- `scripts/phase7_controlled_e2e.py`、`scripts/phase7_analyze_controlled_e2e.py`：新协议/规模/自然负载范围、原始证据、严格分析与验收隔离。
- 新增完整请求和场景测试、两条带历史来源说明的fixture，扩展后台监控失败路径测试；证据篡改测试在`docs/evidence/phase7_reward_audit_probes.py`。

当前专项命令：

```bash
PYTHONPATH=src /opt/anaconda3/envs/MANTO/bin/python -m pytest -q \
  tests/unit/test_completed_request_reward.py tests/unit/test_strategy_scenarios.py \
  tests/unit/test_background_snapshot.py tests/unit/test_controlled_study.py \
  tests/unit/test_controlled_analysis.py
```

40 passed in 2.67s。覆盖Adapter排名与完整请求排名反转、奖励版本隔离、无效/冲突/重复反馈、训练截止边界、snapshot恢复、过期等待/超时、不同场景样本不足与批次不一致。合成计时仅用于单元验证，未计入真实收益。

## 大规模首批失败与内存保留修复

`phase7_reward_large_ambient_20261103` 在block34/READY/ADAPTIVE中止。原始完整耗时688.397356ms；VALIDATED时02:33:01.835883Z，Native fallback实际开始02:33:02.351007Z。DB/client快照分别02:33:01.644849Z/01.668079Z，执行前过期；结果正确但LOW_CONFIDENCE，不进入新奖励。前34个完整块不能当作预声明36块完成，分析器拒绝，不输出正式大规模收益。

历史对象保留探针发现：240条训练WorkflowResult在冻结后仍占用大量受GC跟踪对象。保留时1,030,816个对象、8次完整GC中位222.013ms；释放后仍保留240条Experience，202,097个对象、GC中位48.052ms。该离线探针不是前次现场停顿的因果证明，也不是查询收益；现场原因仍UNKNOWN。原始结果`docs/evidence/phase7_training_retention_probe_20261103.json`，可用`scripts/phase7_profile_training_retention.py --journal ... --output NEW_PATH`重新测量。

最小修复：训练原始证据已完整写入journal且模型已冻结后，清空训练工作流与经验列表，显式GC一次；释放前后检查模型摘要相同，并记录training_memory_released事件。分析器校验释放数量、时序与模型一致性。GC成本属于离线训练收尾，不移动正式请求内学习工作，不关闭运行时GC。

预声明`controlled-e2e-v9`大规模完整新批次：`data/phase7_reward_large_released_20261103`，仍自然LOW、6轮训练、36块留出、原阈值；训练仍第1条，留出改为未用于收益评估的**第6条**，增加`--release-training-evidence`。不复用失败批次样本或模型；修复针对无用对象保留，不依据已完成留出数据调节策略参数或收益门槛。small/v8和large/v9不能用于宣称跨规模因果收益，只各自在同协议内比较策略。

已实测的适用性与安全适用性需要区分：

- S1命中收益只适用于已声明不可变epoch、exact SQL重复、完整结果在缓存容量内且实际命中的情形。重复SQL本身不保证命中。
- 首次访问与未声明epoch时保留S0；未知/不允许缓存语法由原Validator拒绝S1。相关单元测试：`tests/unit/test_decision_engine.py`的first-occurrence测试、`tests/unit/test_repeat_cache_strategy.py`的无epoch/未知SQL测试、`tests/unit/test_result_cache.py`的容量拒绝与epoch隔离测试。
- EMPTY不是数据库冷缓存；READY的填充在显式准备阶段完成。本轮不证明自动从EMPTY学习填充并持续复用的长期收益，也不证明某个重复次数一定能摊销填充成本。对于未知复用机会，S0是安全基线，不等于已实证最快。
- 新奖励的对齐范围是阶段7显式完整请求模型；生产L1默认Adapter目标尚未迁移，生产Bandit保持关闭。后续在线接入需明确延迟反馈与更新成本，不能把离线拟合描述成请求内在线学习。


## 大规模最终结果（v9）

命令exit 0，独立分析exit 0。240训练+216留出完整完成，模型独立重建一致；每个实际缓存×策略留出单元36条，全部VALID/MATCHED。训练收尾释放240条经验与240条完整工作流，174.515ms单独记账，模型摘要不变。DB/client执行前快照最大423.799/388.538ms，均低于500ms。

| 缓存状态 | S0均值ms | 固定S1均值ms | 自适应均值ms | S1−S0配对99%区间ms | 适用性结论 |
|---|---:|---:|---:|---|---|
| EMPTY | 306.195 | 303.490 | 300.417 | [-47.674, 38.586] | 无稳定差异 |
| READY | 221.810 | 118.575 | 117.857 | [-130.138, -78.696] | S1单会话稳定更快，三个批次同向 |

READY固定S1与自适应各36次命中；EMPTY三策略均0次命中。自适应在READY选择36次S1，在EMPTY选择31次S1、5次S0；不能把这5次变化直接解释成CPU因果效应，当前只验证自然LOW。总体描述性均值：S0 264.003ms、固定S1 211.033ms、自适应209.137ms。没有符合原多负载协议的自适应增益认证。

小规模COUNT输出160列、大规模1600列，均完整消费1行聚合结果；不能把数据库总规模等同于每条SQL扫描的点数。未测medium_scale数据集或MEDIUM人工CPU场景的本轮有效收益，不外推。

大规模复现命令与前述小规模命令相同，但使用：
`--output data/phase7_reward_large_released_20261103 --scale large_scale --test-query-line 6 --release-training-evidence`。
独立分析：

```bash
PYTHONPATH=src /opt/anaconda3/envs/MANTO/bin/python scripts/phase7_analyze_controlled_e2e.py data/phase7_reward_large_released_20261103/journal.jsonl --output docs/evidence/phase7_reward_large_20261103.json
```

## 复审与未完成事项

| 项目 | 判定 | 代码/证据 |
|---|---|---|
| 完整请求平均延迟奖励一致性（阶段7显式模型） | PASS | bandit/model.py:184、205；completed_request.py:45、79；两份独立模型重放报告 |
| 错误/回退/冲突/留出样本不得更新 | PASS | 新准入沿用原质量闸门；三份失败批保留；完整请求与篡改测试 |
| 新旧奖励及模型隔离 | PASS | Config.reward_version，update_experience拒绝E2E模型，分区隔离 |
| 已命中缓存的S1适用收益 | PASS（自然LOW、各自单会话范围） | 小/大规模各36配对块，99%区间均低于0、三批次同向 |
| EMPTY中某策略稳定更快 | BLOCKED/未证明 | 两规模99%区间跨0；不把S0安全基线误称为最快 |
| 完整多负载与自适应优于固定策略 | BLOCKED | 人工CPU预检安全中止；v8/v9明确不能满足多负载验收 |
| 自动填充及重复序列的摊销收益 | BLOCKED/未验证 | READY显式准备成本单列，未做完整生命周期对照 |
| 生产在线学习/部署 | NOT READY | 新目标离线拟合后冻结；生产L1保留历史Adapter目标，online_enabled=false |
| 阶段7总验收 | NOT ACCEPTED | 不进入阶段8 |

下一步应预声明包含首次访问、缓存填充、后续重复的完整请求序列，按被分配策略统计全部完成请求与安全回退成本（不将低质量样本用于学习）；另行解决稳定多负载环境与生产延迟反馈调度。不得追溯修改本轮失败批的排除/中止规则，或将其残缺结果拼接成通过。

最终全量回归命令：

```bash
IOTDB_INTEGRATION=1 IOTDB_TEST_SQL='SELECT s_355 FROM root.small_scale.g_0.d_1 WHERE time >= 1640968700000 AND time < 1640978355000 LIMIT 5' /opt/anaconda3/envs/MANTO/bin/python -m pytest -q
```

**882 passed in 34.27s**，含13项真实IoTDB集成，无skipped。最终专项41 passed in2.66s。`python -m compileall -q src scripts tests` exit0。不把不同命令的重叠测试次数相加。

最终证据回归命令：

```bash
PYTHONPATH=src /opt/anaconda3/envs/MANTO/bin/python -m pytest -q docs/evidence/phase7_reward_audit_probes.py docs/evidence/phase7_overhead_audit_probes.py
```

**23 passed in 114.45s**。覆盖新奖励/计时/身份/经验内容/模型版本/重复执行证据/CPU范围/等待预算/中止终态以及训练对象释放事件篡改，旧v5证据重放仍通过。两份最终SQLite integrity_check均ok。大规模v9运行源码与启动摘要一致；小规模v8之后仅两个实验/分析脚本增加v9支持，核心奖励和监控代码一致，v8原启动摘要保留且独立重放通过。文档后续追加结果，不声称其最终摘要与预声明时相同。

来源摘要：`docs/evidence/phase7_reward_artifact_manifest_20261103.json`；失败批汇总：`docs/evidence/phase7_reward_aborted_20261103.json`。仓库现有项目文件整体为untracked，未将此前全项目未跟踪内容打包提交；阶段7未通过，不宣称已交付通过阶段的commit。
