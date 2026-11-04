# 阶段 5 严格验收记录

**最新结论：ACCEPTED（阶段 5 L0/L1 库接口）**。原 F-1/F-2/F-3 已关闭；
本轮完整套件 674 passed，独立复审 6 passed。最新逐项判定和范围见文末。
以下初次审查及修复记录保留为历史，不覆盖最新结论。

日期：2026-11-02。依据 IMPLEMENTATION_GUIDE.md §10.2、阶段 5（898–923 行）
及用户验收清单。审查对象为 L0/L1 库接口；阶段 6 CLI/Orchestrator 闭环不在本轮范围。
本次未修改生产代码，新增独立审查反例和本报告，并同步当前验收状态。

## 实际测试

解释器：/opt/anaconda3/envs/MANTO/bin/python。

```bash
IOTDB_INTEGRATION=1 PYTHONPATH=src /opt/anaconda3/envs/MANTO/bin/python -m pytest -q
# 646 passed in 16.67s（含 12 个真实集成用例）

PYTHONPATH=src:tests/unit /opt/anaconda3/envs/MANTO/bin/python -m pytest -q docs/evidence/phase5_review_probes.py
# 3 failed in 0.89s，三个安全/一致性断言均未满足
```

反例仅复用 tests/unit/test_learning.py 的 TEST ONLY 模拟执行与临时 SQLite，不把
构造的 DB 指标、耗时或测试策略混入真实 Benchmark。审查反例位于 docs/evidence，
需显式调用；646 passed 不包含这三个新增反例，不能用来覆盖其失败结论。

## FAIL 与最小修复

### F-1 / P1：MISMATCHED 隔离依赖先完成可能失败的数据库读取

- 位置：agents/learning_feedback.py:53–55、115；strategies/registry.py:164–174；
  learning/repository.py:143–146（路径均位于 src/tsdb_optimizer）。
- observe 先执行 _experience，再执行内存 quarantine。_experience 为查认证调用
  Registry.assert_selectable，后者会查询隔离库。SQLite 读取失败时直接进入外层
  LEARNING_FAILED，内存隔离根本没有发生。
- 复现：有效 run/record，runtime_result_check=MISMATCHED；注入 _connect 读取故障，
  observe 返回可见 warning；恢复连接后 TEST_ONLY_FAST 仍出现在 selectable_ids。
- 现有 tests/unit/test_learning.py:286 只模拟 repository.record 写入失败，未覆盖
  内存隔离之前的读取失败。异常有日志，但安全事实丢失，不能因此判隔离通过。
- 最小修复：将仅依赖 run/record 的身份、attempt 与版本核对拆为无 I/O 步骤；
  确认 MISMATCHED 对应实际执行后，先隔离内存版本，再做认证查询/经验构造/持久化。
  保留异常报告，不能将读取失败解释成未隔离。增加读取故障、写入故障及恢复后仍
  不可执行的回归测试；对隔离持久化失败明确记录待恢复状态。

### F-2 / P1：已知其他来源越限仍参与正常奖励和模型更新

- 位置：agents/learning_feedback.py:156–162；schemas/learning.py:58–66。
- 准入只检查 context.load_source 选中的快照。client_host LOW 时，已知、有效且
  新鲜的 db_host CPU=99% 被忽略；同一个 before 快照经过当前资源策略判断为
  breached=True，却仍被 Agent 4 判 VALID。
- 复现：连续三个这样的成功执行都获得 reward=-0.009950330853168083，第三次
  model_updated=True。不是只存一条审计记录，而是污染了正常性能统计。
- 分桶硬编码 50/90，学习准入没有使用版本化 ResourcePolicy；分组来源不应决定
  是否忽略另一个已知来源的硬约束。DB 指标 unavailable 可跳过，已知越限不能忽略。
- 最小修复：区分负载分组与安全准入；复用或显式传入同一版本化资源策略，按执行
  时刻检查所有有效来源。任一已知越限则 LOW_CONFIDENCE/INVALID、reward=null，
  不训练；记录策略版本和具体原因。补 client/DB/db_metrics 越限、反向来源、
  自定义阈值、过期/未来/未知来源和等于阈值边界测试。

### F-3 / P2：经验中的决策模型版本没有与持久化决策核对

- 位置：agents/learning_feedback.py:173；learning/repository.py:23–56。
- Experience.decision_model_version 直接取调用方 run.decision.model_version。
  Repository 虽读 decisions 行并检查策略版本，却没有检查 model_version。
- 复现：原 decisions.model_version=TEST_ONLY-model；只把内存 run 的该字段改为
  never-used-model，observe 仍保存 VALID 经验，其 decision_model_version 与原决策
  不一致。query/decision/execution ID 均未改变。
- 最小修复：从持久化决策提取该版本，或在同一写事务中逐项核对，不一致拒绝写入。
  同时覆盖 None/非空、真实 L1 模型与外部 advice 版本的关联回归。不能仅凭合法
  字符串或 Pydantic 类型正确认定关联真实。

## 逐项判定

| 检查项 | 判定 | 文件位置与证据 |
|---|---|---|
| 当前阶段任务是否全部达到条件 | FAIL | 指南 918–923 行要求错误样本不污染、版本关联可靠；F-1/F-2/F-3 均实测失败。经验表、检索、统计、门槛、原子发布等确有实现 |
| TODO/pass/mock 假实现 | PASS（阶段 5 范围） | learning/ 与 Agent 4 无占位；schemas/execution.py:137、strategies/models.py:141 是有条件 no-op；sql_parser.py:1371 明确拒绝未实现 metadata enrichment，test_sql_parser.py:498 覆盖，不伪造成功 |
| 字段/单位/时间戳/source 一致 | FAIL | F-3 模型关联不一致；其余 reward 固定 ms/1000、总体方差 ms²、UTC/source 验证有 tests/unit/test_learning.py:70、test_monitoring_schema.py:88/234 支持 |
| 异常是否吞掉 | PASS（错误可见性） | learning_feedback.py:73–80/180–182 日志与 warning；test_learning.py:211/316。F-1 是异常时漏隔离，不是无日志吞异常 |
| 未验证的 IoTDB API/SQL | PASS（已审计 2.0.6 tree 范围） | 阶段 5 repository 只执行 SQLite；benchmark 脚本:50 读取既有 q4/q5；真实集成全过；不支持模板只 LOW_CONFIDENCE |
| client metrics 冒充 DB metrics | PASS | schemas/monitoring.py:190、monitoring/system.py:348；test_monitoring_schema.py:139；F-2 是漏检查真实来源，并非伪装来源 |
| LLM 绕过 Registry/Validator | PASS | Agent 4:212 只返回 AdaptiveAdvice；Executor:90 重做 preflight；test_decision_engine.py:212/698/916；无 LLM 执行链 |
| 错误/不合格样本参与正常学习 | FAIL | F-2 三次越限样本正常 reward 并发布模型；F-1 可让已知不一致版本重新被选择。一般失败/timeout/fallback 测试通过不能覆盖这两点 |
| 普通路径运行 EXPLAIN ANALYZE | PASS | tree_adapter.py:519–531 只构造 EXPLAIN；test_tree_adapter.py:527；Agent 4 无 SQL 执行 |
| 重复执行/缓存污染/未消费结果 | PASS（既有固定 epoch 契约） | tree_adapter.py:614 完整迭代，645 关闭；test_tree_adapter.py:185/583/843；真实 cache certification 通过。并发、跨进程与 epoch 失真不在该证明范围 |
| 测试真实运行且失败路径覆盖充分 | FAIL（覆盖缺口） | 原有 646 passed 实际复跑，但独立三反例 3 failed；写入失败、timeout、fallback、原子回滚已有覆盖，读取故障与跨来源准入等遗漏 |
| 真实 DB 负载下学习有效性 | BLOCKED / UNKNOWN | UnavailableDbHostSampler，无 DB 代理；真实 Benchmark 只有客户端 LOW。不同负载偏好目前只有明确模拟证据 |

## 已确认通过的阶段 5 基础能力

- SQLite v7 additive schema、查询/决策/执行外键、execution 唯一性；v6 迁移保存
  原审计记录（test_learning.py:250）。模型版本关联仍受 F-3 限制。
- 严格 context/策略版本检索、数量/均值/中位数/总体方差/成功率；min_samples
  和改善门槛（test_learning.py:70/173/182/239）。准入仍受 F-2 限制。
- 原子模型与 head 发布、过期 revision 拒绝、旧版本保留
  （learning/repository.py:175，test_learning.py:192）。
- 写经验或模型失败不改变已完成 run，不增加 SQL 调用
  （test_learning.py:211）。隔离容错仍受 F-1 限制。

既有 Benchmark JSON 的 24 条观测、12 VALID/12 LOW_CONFIDENCE 属于此前真实实验；
本次未将模拟反例伪装成真实 DB 实验，也未重新宣称收益。没有要求阶段 6 提前接线。

结论：**NOT ACCEPTED**。完成 F-1/F-2/F-3 最小修复并把这些反例纳入正式回归后，
再对阶段 5 复审；不建议直接进入阶段 6。

## F-1/F-2/F-3 修复后证据（2026-11-02）

| FAIL | 修复 | 验证 |
|---|---|---|
| F-1 | 无 I/O 身份/attempt/版本校验先于隔离，内存隔离先于认证查询与写库；pending 状态支持恢复后重试 | 原反例通过；正式回归覆盖读/写故障、恢复不解封、重试持久化、错误身份不隔离其他策略 |
| F-2 | 复用版本化 ResourcePolicy，执行时刻检查所有有效来源；记录 quality_version=2 和策略指纹 | 原反例通过；三来源×三指标、自定义阈值、等号、invalid/stale/future/unknown、旧模型与策略指纹隔离通过 |
| F-3 | 写事务中核对持久化 decision.model_version | 原反例通过；None/非空、外部版本和已有真实 L1 建议关联回归通过 |

实际使用 /opt/anaconda3/envs/MANTO/bin/python：

```bash
PYTHONPATH=src python -m pytest -q tests/unit/test_learning.py tests/unit/test_learning_review_regressions.py
# 64 passed in 12.94s
PYTHONPATH=src:tests/unit python -m pytest -q docs/evidence/phase5_review_probes.py
# 3 passed in 0.81s
IOTDB_INTEGRATION=1 PYTHONPATH=src python -m pytest -q
# 674 passed in 23.35s
PYTHONPATH=src python -m compileall -q src tests scripts
# exit 0
```

修复后另存真实 Benchmark：evidence/phase5_learning_fixes_2026-11-02.json，
12 VALID / 12 LOW_CONFIDENCE，24 条实际观测、16 次测量 dispatch，全部带质量版本 2。
初版文件保留历史，未给旧经验补造新准入证明。DB 负载仍 UNKNOWN，无阶段 6 工作。
状态为修复已应用、等待严格复审，不自行宣布 ACCEPTED。

## 修复后严格复审：ACCEPTED（阶段 5 L0/L1 库接口，2026-11-02）

本节为最新验收结论，前面的 NOT ACCEPTED 和 PENDING RE-REVIEW 为历史记录。
范围限定为用户明确指定的阶段 5：可靠经验、结构化检索/统计、质量准入、奖励、
最小样本、模型原子版本、失败隔离及显式库接口。不包含阶段 6 自动闭环、Bandit、
生产性能收益或真实 DB metrics agent。当前阶段未发现阻塞性 FAIL。

### 本轮实际运行

```bash
IOTDB_INTEGRATION=1 PYTHONPATH=src /opt/anaconda3/envs/MANTO/bin/python -m pytest -q
# 674 passed in 20.69s（包括 12 个真实集成用例）

PYTHONPATH=src:tests/unit /opt/anaconda3/envs/MANTO/bin/python -m pytest -q docs/evidence/phase5_review_probes.py docs/evidence/phase5_rereview_probes.py
# 6 passed in 1.24s
```

第二条命令含原三个 FAIL 的断言，以及新增加的并发幂等、隔离后旧模型重发布拒绝、
真实 Benchmark 原始记录重算。两套数字分别报告，不把重复执行的反例累加为新覆盖。
本轮不修改生产实现，只新增复审证据及同步验收文档。

### 原 FAIL 关闭

| 编号 | 结论 | 当前实现位置（src/tsdb_optimizer/ 下） | 复核证据 |
|---|---|---|---|
| F-1 | PASS / CLOSED | agents/learning_feedback.py:57 先无 I/O 验证与内存隔离；:89 暴露待持久化状态 | test_learning_review_regressions.py:9/60/81/201：读写故障、恢复重试、不一致身份/版本拒绝；原独立反例通过 |
| F-2 | PASS / CLOSED | agents/learning_feedback.py:188；learning/repository.py:167；:212 | test_learning_review_regressions.py:96/114/131/158/185：各来源/指标、自定义阈值、时间边界、旧模型和策略指纹隔离 |
| F-3 | PASS / CLOSED | learning/repository.py:39 核对持久化决策模型版本 | test_learning_review_regressions.py:37/146；test_learning.py:99 真实 L1 建议写入后关联核对 |

### 阶段 5 十项任务

| 任务 | 判定 | 实现与测试证据 |
|---|---|---|
| experience schema / SQLite repository | PASS | schemas/learning.py:69；learning/schema.py:10；repository.py:75；test_learning.py:250 迁移保留审计行 |
| 查询/决策/执行/策略/模型版本关联 | PASS | repository.py:23；agents/learning_feedback.py:94；test_learning.py:99；test_learning_review_regressions.py:146 |
| 三种样本质量过滤 | PASS | agents/learning_feedback.py:159–192；test_learning.py:114/408/437；test_learning_review_regressions.py:96 |
| 模板/特征/DB/策略版本/负载检索 | PASS | repository.py:133；schemas/learning.py:28；test_learning.py:173/182/345 |
| 样本量/均值/中位数/总体方差/成功率 | PASS | repository.py:150；test_learning.py:70；phase5_rereview_probes.py 重算真实证据 |
| 固定延迟 reward | PASS | agents/learning_feedback.py:192；test_learning.py:76；真实证据独立重算 |
| min_samples / 策略变更门槛 | PASS | repository.py:178；agents/learning_feedback.py:216；test_learning.py:239 |
| 模型版本与原子保存 | PASS | repository.py:184；test_learning.py:192/334；复审旧模型重发布反例 |
| 学习写库失败不改变查询结果 | PASS | agents/learning_feedback.py:81；test_learning.py:211；test_learning_review_regressions.py:60 |
| 模拟测试与真实 execution record/Benchmark | PASS | test_learning.py / test_learning_review_regressions.py；tests/integration/test_learning_feedback.py:19；phase5_learning_fixes_2026-11-02.json |

### 用户审查清单

| 检查 | 判定 | 位置与证据 |
|---|---|---|
| 阶段任务真实实现并达到条件 | PASS | 上表十项；指南阶段 5 四项通过条件都有行为证据，而非仅有类/文件 |
| TODO/pass/mock 假实现 | PASS（当前阶段） | learning/ 与 Agent 4 无占位；schemas/execution.py:137、strategies/models.py:141 是有条件 no-op；sql_parser.py:1371 明确拒绝未实现 metadata，test_sql_parser.py:498 验证；TEST ONLY 数据仅用于测试 |
| 字段/单位/时间戳/source 一致 | PASS | schemas/learning.py:103、repository.py:23；test_learning.py:70/99，test_monitoring_schema.py:139/234；真实 JSON 重算与来源检查 |
| 异常被吞掉 | PASS | Agent 4:81–90/208 记录异常与 warning 或安全退化；storage/sqlite.py:280 回滚并重抛；故障注入已通过 |
| 未验证 IoTDB API/SQL | PASS（2.0.6 tree 已审计范围） | 阶段 5 新增存储为 SQLite；scripts/phase5_learning_evidence.py:50 使用既有 workload；真实集成通过；未知模板不训练 |
| client metrics 冒充 DB metrics | PASS | schemas/monitoring.py:190；monitoring/system.py:348；test_monitoring_schema.py:139；复审原始 JSON 验证 DB 来源 unknown/invalid |
| LLM 绕过 Registry/Validator | PASS | 无 LLM 执行路径；Agent 4:245 只返回策略 ID 建议；execution/executor.py:90 重新校验；test_decision_engine.py:212/698/916 |
| 错误/不合格样本更新正常奖励 | PASS | 准入、quality_version=2/策略指纹、MISMATCHED 撤销及旧模型拒绝；test_learning.py:140/382/408；新增跨来源回归 |
| 普通请求 EXPLAIN ANALYZE | PASS | tree_adapter.py:531 仅显式 EXPLAIN；Agent 4 无 SQL 调度；test_tree_adapter.py:527；真实普通记录保持 NOT_CHECKED |
| 重复执行/缓存污染/未消费结果 | PASS（固定 epoch、现有单进程契约） | tree_adapter.py:614/648 完整消费并关闭；result_cache.py:177 复制元数据；test_tree_adapter.py:185/583/843；真实缓存认证；并发反馈仅计一个 execution |
| 测试真实运行与失败路径覆盖 | PASS（本次核查范围） | 674 passed + 独立 6 passed；覆盖超时/fallback、解析/来源/版本不合格、读写故障、模型回滚、隔离、幂等、旧质量与策略变更 |
| 真实 DB 负载/生产收益证据 | BLOCKED / UNKNOWN | 无 DB metrics agent，只有真实客户端 LOW 与明确模拟的多负载；不属于当前 L0/L1 库接口通过声明 |

### 保留边界

- 调用方须提供真实环境/epoch/缓存条件和相应负载上下文；dataset_size 未知保留 null。
- 只有绑定 learner 的 Registry 读取隔离库；存储故障时内存隔离有效，pending 状态
  不能被解释为已跨重启持久化，调用方须在恢复后重试原证据。
- 旧质量版本经验仅保留审计，旧模型不用于当前建议；自定义 ResourcePolicy 应与
  Agent 2 同步传入，策略指纹不匹配时无建议。
- 缓存固定 epoch、进程内等旧契约继续有效；CLI 自动学习调度属于尚未实现的阶段 6。
- 本次没有新增阻塞性 FAIL，最小修复清单为空；验收通过不等同于生产收益验证或整个系统完成。
