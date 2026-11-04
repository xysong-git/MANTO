# 阶段7：填充收益归因与延迟批次反馈

2026-11-03，实施前协议 `delayed-batch-v1`。使用Conda MANTO；不进入阶段8，生产部署锁保持关闭。

## 决策与归因边界

仅对调用方已明确提交的、不可变epoch内、同一SQL的有限批次建模，长度2或8。长度是声明输入，不是从未来观测推断的特征；未知长度/开放流继续使用原请求路径。本研究只验证small_scale COUNT族，不外推其他SQL或数据库内部计划优化。

两个批次选项映射到现有注册策略：S0每次原查询；S1保留首次Native门槛，第二次填充，后续通过原Registry/Validator/Executor复用。不是新增未经认证的数据库策略。每批新建DecisionEngine并清空客户端结果缓存；不清OS/IoTDB缓存。

反馈单元是完整批次，只对实际被分配选项更新一次：`reward=-batch_service_ms/1000`。`batch_service_ms`为策略选择（解析、快照等待、模型推断）的实测耗时加全部完整请求耗时；请求覆盖有界准入、Orchestrator、完整消费、经验持久化与连接收尾。实际wall时间另外记录，外部实验JSON审计间隙不冒充服务耗时。没有虚构相对Native的节省值，没有对未执行动作或未发生命中生成标签。

每次命中通过cache_source_execution_id关联本批次已测量填充；首次、填充及命中成本不能省略或重复计数。错误/回退/过期/质量不合格/不完整批次保留为结果与失败证据，不进入训练；测试对照保留所有成功成本，真实失败立即中止，不挑选完成子集宣称收益。

## 延迟反馈

请求内原质量过滤和经验持久化保留，显式批次路径关闭未被该决策使用的同步L1统计发布（默认路径仍同步）。批次完成后才构造反馈，独立SQLite队列持久化，下一批之前消费；不重新执行查询。队列和模型版本在同一事务内提交，执行ID跨批去重，重投幂等、冲突拒绝，未知异常回滚并显式失败。序列结束前不能更新；冻结后拒绝新增训练与所有留出反馈。队列提交、校验、更新成本分别记账，报告累计/每批均值，不将延后成本称为零，也不宣称冻结留出证明了生产异步学习吞吐。

每长度独立LinUCB模型，沿用17维执行前编码、alpha=.1、lambda=1、min_samples=30；奖励/分区新版本隔离。开始时没有旧模型或旧经验。只在真实完整质量闸门通过后更新分配arm；首条Native是S1选项共同的安全前缀，不另外产生一个S0批次标签。选择仍经Registry及逐请求Validator。模型冻结前排空队列，留出期间校验模型摘要恒定。

## 预声明实验

- dataset_epoch=`heavy-complete-20261102-v2`，small_scale/q03_count_aggregation.sql第10条训练、第11条留出；两者都未用于此前收益训练/评估，不复用旧留出调参。
- 固定seed=20261107；训练30轮，每轮LOW/MEDIUM随机顺序，每负载包含长度2/8×S0/S1四批次随机顺序，共240训练批次、1200请求。两个arm每单元恰好30完整批次；批次顺序随机平衡，首位置p=.5，不虚称整组各位置独立随机。
- 留出30块，每块两负载×两长度×S0/固定S1/ADAPTIVE，共360批次、1800请求；固定seed=20261108。训练与留出SQL、execution IDs和时间严格隔离。先完成全部训练，再冻结，再运行留出。
- 每负载/长度/arm至少30有效训练批次，每负载/长度/策略至少30实际留出批次；按实测DB负载计数，错档不改标签、不补单条。两轮pilot每档20样本、80%目标稳定率；实际训练/留出也保留逐请求与选择时采样。
- 人工整机CPU共租目标55%，MEDIUM目标50%–70%，CPU>80%/内存>90%/磁盘容量>90%停止；30秒worker自行到期，60秒进程组监护，单连接、查询30秒超时。快照TTL500ms，准入年龄250ms，最多等待0.5s。不修改IoTDB配置。
- 同负载/长度/块计算实际配对服务成本差，4000次bootstrap，seed=20261109，点态99%区间、三个连续10块均值同向。总体按四场景等权；ADAPTIVE必须相对Native和固定S1均符合原收益门槛才称单会话额外收益。报告Adapter与完整服务成本，不能用缓存命中速度替代全路径收益。
- 不同负载未必必须选择不同策略：若没有实证偏好交叉则如实报告，不能人为制造切换。独立留出只证明本批协议范围；其他会话复现/生产部署仍需独立验收。

预计输出 `data/phase7_delayed_batch_20261103`。目录必须不存在，失败日志保留；不自动重试失败查询，不根据留出收益改配置。接口与队列测试先于真实实验。

## v1中止与v2修订（重新运行前记录）

v1在TRAIN block25/LOW的第二个批次首请求准入前中止，已完成201批、1008请求；没有留出执行。原目录保留，不能用于完整验收。错误为`fresh resource admission unavailable`；原日志缺少被拒快照，具体超龄量UNKNOWN。代码存在先检查年龄、复制快照后再次检查的时间边界，250ms附近可能过期。

v2不放宽250ms准入：后台返回快照上限收紧为200ms、周期100ms（原200ms），预留50ms交接余量；准入失败记录完整快照与检查时刻。持续过期仍中止，不重试SQL。另将训练/留出覆盖严格绑定实际档位与计划目标同时一致，禁止错档交换凑够计数；按phase/load汇总选择与逐请求准入CPU，目标区间稳定率至少80%，不足禁止正式收益区间。

新目录`data/phase7_delayed_batch_v2_20261103`、协议`delayed-batch-v2`，完整零初始化重训；训练/留出SQL和随机计划不变，留出未曾执行。不复用v1权重或样本。奖励/模型分区仍是同一批次语义版本v1，只有实验采样协议版本升级。此修订依据准入失败，未查看留出收益调参。

## 实际结果：实现通过，完整训练/留出仍阻塞

| 实验 | 完整训练批次 | 已记录请求 | APPLIED / REJECTED | 冻结后留出 | 停止原因 |
|---|---:|---:|---:|---:|---|
| v1 | 201 | 1008 | 201 / 0 | 0 | TRAIN block25/LOW准入快照不可用；具体被拒快照/超龄量UNKNOWN |
| v2 | 98 | 492 | 96 / 2 | 0 | TRAIN block12/MEDIUM：DB_HOST CPU=83.183%，超过80%停止线 |

v2被拒快照captured_at=2026-11-03T04:14:38.584103Z，准入时刻04:14:38.705413Z，年龄121.31ms；client快照年龄41.22ms。`fresh=true/safe=false`，内存65.8%、磁盘容量21.7%。因此这次是实际整机CPU超线，不是过期；CPU峰值的进程来源UNKNOWN，不声称来自IoTDB本身。控制器此前约55%，最后一次较宽窗口读数67.4%，不能用较低窗口均值覆盖83.183%的准入证据。退出时共租worker按既有上下文收尾，未继续查询或加压。

已记录请求的Experience分别1008/492条VALID；这不等于所有批次可训练。v2有2个批次因`BATCH_LOAD_CHANGED`拒绝，均保留，无更新。未完成批次没有反馈、没有借用其他批次收益。两轮数据/模型完全分离，不合并达到门槛。

独立失败审计从零版本逐条校验队列checksum、质量、分配arm和延迟更新，重建的201/96次更新与journal receipt、SQLite head模型摘要一致，四个SQLite integrity_check均为ok。原始journal及队列不改写。v2启动时85个源码/协议文件在结果文档更新前逐一hash一致；此后本文件增加结果，属于结果文档变更，不声称文档最终hash仍与启动时相同。

延后成本不是零：v1入队均值43.425ms、消费/更新均值133.414ms；v2分别44.201ms、122.478ms（包括2次拒绝消费）。这些是已执行训练前缀的真实成本，没有独立留出总体收益含义；也不能据此声称生产后台吞吐。默认L1路径未改成异步，当前消费者在下一批前显式调用。

**没有完成240批训练，没有冻结后的360批独立留出，没有本轮收益置信区间。收益UNKNOWN、阶段7 NOT ACCEPTED、部署NOT READY。** 不能将之前序列实验的固定S1收益或本次部分训练结果当成本模型验证。

机器证据：[完整失败审计](evidence/phase7_delayed_batch_failure_audit_20261103.json)。早期精简审计`phase7_delayed_batch_aborts_20261103.json`也保留；两者原始journal摘要一致，后者增加拒绝原因和延迟成本。正式分析器对v2以`aborted or unknown journal event; no filtering`拒绝，exit1，未创建伪成功报告。

## 改动文件与验证

- `src/tsdb_optimizer/bandit/episodes.py`：Pydantic批次契约/闭合反馈、真实完整成本、归因与模型分区。
- `src/tsdb_optimizer/bandit/delayed_store.py`：独立持久化队列、事务ACK/模型版本、重启、冻结。
- `src/tsdb_optimizer/bandit/model.py`：增加隔离的新奖励版本，原接口不能混用。
- `src/tsdb_optimizer/agents/learning_feedback.py`：显式record_only；默认同步行为保留。
- `src/tsdb_optimizer/experiments/batches.py`：预声明随机顺序、实际目标覆盖与配对统计闸门。
- `scripts/phase7_delayed_batch.py`、`phase7_analyze_delayed_batch.py`、`phase7_audit_delayed_abort.py`：有界真实实验、独立完整重放、失败证据重放。
- `tests/unit/test_delayed_batch.py`、`tests/fixtures/batch_feedback_historical.json`：18个测试；fixture明确为历史派生合成测试材料，禁止用作真实训练证据。
- `docs/evidence/phase7_delayed_batch_review_probes.py`：原始中止日志拒绝及8种流式篡改拒绝。
- 本文、`IMPLEMENTATION_STATUS.md`、`LEARNING_DESIGN.md`、`README.md`同步边界与真实状态。默认环境遵循根目录`AGENTS.md`的Conda MANTO约定。

实际命令与结果：

```bash
conda run -n MANTO python -m pytest -q tests/unit/test_delayed_batch.py tests/unit/test_learning.py tests/unit/test_linucb.py
# 最终 68 passed in 7.98s

IOTDB_INTEGRATION=1 IOTDB_TEST_SQL='SELECT s_355 FROM root.small_scale.g_0.d_1 WHERE time >= 1640968700000 AND time < 1640978355000 LIMIT 5' conda run --no-capture-output -n MANTO python -m pytest -q
# 918 passed in 40.52s，含13项真实集成，无skipped

PYTHONPATH=src:scripts conda run --no-capture-output -n MANTO python -m pytest -q docs/evidence/phase7_delayed_batch_review_probes.py
# 9 passed in 14.10s

conda run -n MANTO python -m compileall -q src scripts tests
# exit 0
```

最初专项运行出现12个fixture设置错误、1个不存在的store.count调用失败：Conda默认捕获stdin导致生成fixture的heredoc未执行，改用`--no-capture-output`；计数改为真实SQL查询。保留测试并修正，未伪造通过；以上为修复后的实际最终结果。

```bash
PYTHONPATH=src:scripts conda run --no-capture-output -n MANTO python scripts/phase7_delayed_batch.py --output data/phase7_delayed_batch_20261103 --epoch heavy-complete-20261102-v2 --local-db-pid 3879777 --immutable-epoch-confirmed --cpu-cotenant-authorized
# v1 exit 1：快照准入失败

PYTHONPATH=src:scripts conda run --no-capture-output -n MANTO python scripts/phase7_delayed_batch.py --output data/phase7_delayed_batch_v2_20261103 --epoch heavy-complete-20261102-v2 --local-db-pid 3879777 --immutable-epoch-confirmed --cpu-cotenant-authorized
# v2 exit 1：CPU安全阈值超限

PYTHONPATH=src:scripts conda run --no-capture-output -n MANTO python scripts/phase7_analyze_delayed_batch.py data/phase7_delayed_batch_v2_20261103/journal.jsonl --output docs/evidence/phase7_delayed_batch_v2_20261103.json
# exit 1：正确拒绝中止记录；没有生成该输出文件

PYTHONPATH=src:scripts conda run --no-capture-output -n MANTO python scripts/phase7_audit_delayed_abort.py data/phase7_delayed_batch_20261103 data/phase7_delayed_batch_v2_20261103 --output docs/evidence/phase7_delayed_batch_failure_audit_20261103.json
# exit 0：仅审计失败与已提交模型，不生成收益判定
```

下一步仍在阶段7：先定位共租CPU瞬时峰值与批内跨档，保持80%停止线、250ms正式准入、30批/单元门槛；稳定预检后另行预声明从零完整实验，不续接/拼接本次失败前缀。然后完成训练、冻结、独立SQL多负载留出与实际服务成本对照。未知长度请求归因、跨会话复现、生产后台反馈吞吐仍未覆盖，不进入阶段8。
