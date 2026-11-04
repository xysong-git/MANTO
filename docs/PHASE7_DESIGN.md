# 阶段 7：离线 LinUCB 与可选建议接口

日期：2026-11-02。状态：离线复审修复已验证；**阶段 7 完整验收 NOT ACCEPTED，真实自适应部署 NOT READY**。
没有进入阶段 8。阶段 6 已验收线上链路不接入本阶段实验模块。

## 1. 先决证据审计

配置指定的生产 SQLite 文件当前不存在，不将不存在的记录算作训练量。
审查最近的 `phase5_learning_fixes_2026-11-02.json`：24 个真实 execution IDs，
12 VALID / 12 LOW_CONFIDENCE。质量版本为 2；只使用这一份最新证据，不将早期旧规则
报告与新报告拼接以抬高样本量。注册策略为 S0_NATIVE@0.1.0、S1_REPEAT_CACHE@1.0.0；
后者仍要求调用方声明不可变 cache epoch，CAND_* 未启用。

同一个可比较 SERVER 环境仅有 S0=4、S1=4 个合格样本；其余 4 个 VALID 是 CACHE_HIT，
没有同源 Native 对照，不能作为服务器延迟样本。负载只有 LOW。
旧诊断脚本使用固定种子随机打乱平衡 SERVER 顺序，但记录没有逐次选择概率与完整
探索策略凭证；后续紧跟 miss 的 hit 是条件采样，不能冒充独立随机分配。

版本化工程门槛为每 arm **min_samples=30**，至少两个可比较已认证策略、两个实测负载
上下文、可审计随机探索概率及独立留出评估。30 是保守工程门槛，不是统计功效保证。
当前样本量/覆盖/探索记录/收益证据均不足。所有 Snapshot 使用 `online_ready: Literal[False]`，
`online_advice()` 固定返回 None；配置不允许 online_enabled=true。
这是明确的部署锁，不是假装已完成生产模型。即使离线 n 达门槛，也不能绕过部署锁。

## 2. 上下文编码 v1

固定版本 `linucb-context-v1`，17 维，完全不读取本次实际延迟、实际行数、执行后快照或 reward。
不使用测试集拟合 scaler。输入来自 Agent 1 和执行开始前仍有效的快照。

| index | 特征 | 缩放 |
|---|---|---|
| 0 | bias | 1 |
| 1 | device_count | log1p(min(v,1000))/log1p(1000) |
| 2 | series_count | 同上，cap=1000 |
| 3 | filter_count | 同上，cap=100 |
| 4 | time_range_ms | 同上，cap=86400000 ms |
| 5 | client_cpu_percent | clip(v,0,100)/100 |
| 6 | client_memory_percent | 同上 |
| 7 | db_cpu_percent | 同上，仅 source_type=db_host 且有效、新鲜 |
| 8 | db_memory_percent | 同上 |
| 9–16 | 对应 1–8 的 missing indicator | 缺失=1、已知=0 |

未知/无效/过期/来源错误的数值填 0 并置 missing=1；已知零负载 missing=0。
负数、非有限数值拒绝，不隐式纠正。当前无 DB agent，DB 特征保持 missing，不能复制客户端数值。

模型分区固定 server身份/版本、数据库模型、dataset epoch/size、缓存条件、结果来源、
fetch mode、负载来源/主机、质量资源策略指纹和特征版本。查询形状与负载桶由向量建模，
不再硬分组；因此离线可研究跨查询/负载泛化，但尚无生产泛化证据。
SERVER 与 CACHE_HIT 绝不合并。结果来源本身可能是执行后的条件变量，这也是当前模型
不能直接部署的原因之一；未来部署必须解决执行前缓存状态与可比较 arm 定义。

## 3. 算法与反馈

采用 disjoint LinUCB，依据 [Li 等原论文 Algorithm 1](https://arxiv.org/abs/1003.0146)。
每个 arm 由 strategy_id + strategy_version 定义：

- 初始 `A_a = lambda * I_17`，`b_a = 0_17`，`n_a=0`。
- `theta_a = solve(A_a, b_a)`。
- `score_a(x) = theta_a^T x + alpha * sqrt(x^T solve(A_a,x))`。
- 仅已执行 arm 更新：`A_a += x x^T`，`b_a += r*x`，`n_a += 1`。
- 固定 reward：`negative-log1p-ms-v1`，`r=-log1p(client_wall_ms/1000)`，越大越好。
  不填未执行 arm 的 reward，不把没有样本的 arm 延迟填成 0。
- 默认 `alpha=0.1`、`regularization=1.0`、`min_samples=30`、seed=20261102，
  位于 `configs/bandit.yaml`，严格 schema 校验。纯 Python Cholesky 求解，不显式矩阵求逆，
  不新增数值依赖。矩阵维数、对称、正定与非有限值检查失败则拒绝更新/加载。
- `choose(explore=True)` 仅用于离线：不足门槛先选 n 最少的合法 arm（固定排序破平局），
  随后选最高 UCB。`explore=False` 为冻结评估的 greedy；未达到门槛时保留 Native。
  调用方候选集合必须含 baseline 且不能包含模型外 arm；真实反馈再次检查 Registry 当前认证与版本隔离；真实离线评估使用 choose_registered，重新排除已隔离或版本变化的 arm。

更新接口分开：SYNTHETIC 只能 update_synthetic；REAL_SHADOW 只能 update_experience。
真实导入重新检查质量版本 2、资源指纹、认证、版本、reward 公式、有效负载以及分区。
只接收已记录的真实 Experience；这是离线证据导入接口，不替代阶段 5 的数据库审计写入接口。
低可信、失败、超时、MISMATCHED、fallback 等不能进入奖励更新。

每个 execution_id 保存摘要及所属 arm，完全相同重放幂等，冲突重放拒绝；保存恢复后仍去重。
训练截止时间固定，晚于截止的样本拒绝更新。评估要求测试 started_at 晚于训练截止，且 IDs
不重叠；模型参数在评估前后比较摘要，验证冻结。未来时间窗口不是 IID 或因果独立性的保证。

## 4. 参数保存与回滚

`OfflineModelStore` 使用独立实验 SQLite，**不迁移或修改线上 v8 Store**。
每个不可变 model_version 保存 domain、分区、上下文/奖励/算法参数、全部 A/b/n、截止时间、
观测去重台账和校验和。模型体插入与 head 更新使用同一个 BEGIN IMMEDIATE 事务，
expected_head 比较更新拒绝并发陈旧发布；head 失败则模型体也回滚。

回滚只原子切换同一 model_key 的历史 version，旧版本保留；校验和、参数协议错误拒绝加载。
不同 domain/分区/arm 集合/配置不能互相回滚。校验和用于损坏检测，不是来源签名；实验库
不作为不可信输入安全边界。生产模型发布仍未开放。

## 5. LLM 可选接口与对照

`SafeLLMAdvisor` 默认 enabled=false、transport=None，不读取 API key，不调用外部提供方。
仅支持显式注入可取消的异步 transport；prompt 位于 configs/prompts/strategy_advisor.txt。
输入只含通过候选检查的 strategy IDs 与 Native ID，不传原 SQL。返回严格 JSON：
`{"strategy_id":"S0_NATIVE"}`，额外 SQL/plan/任意字段、未知 ID 均拒绝。
等待期间被隔离的策略也拒绝。返回建议仍只能交给 Agent 2/Validator/Executor，不能直接执行。

关闭、不可用、schema 错误、超时返回 advice=None，调用方沿用规则决策。未接入 Orchestrator；
因此没有偷偷改变已验收的生产决策。transport 必须配合取消；当前没有真实提供方实现，
不声称已经验证真实网络超时、成本、模型质量或生产收益。

本地 10 次/场景接口对照（实测，不是实际 LLM）：

| 模式 | 建议/规则回退次数 | 相对 off 中位额外 ms | 含义 |
|---|---|---|---|
| off | 0 / 10 | 0 | 不调用 transport |
| on，无提供方 | 0 / 10 | 约 0.000013 | UNAVAILABLE |
| on，合法模拟响应 | 10 / 0 | 约 0.055245 | schema 合格，建议与规则一致率 100%，不是质量增益 |
| on，非法模拟响应 | 0 / 10 | 约 0.046083 | ValidationError 安全回退 |
| on，模拟超时 | 0 / 10 | 约 5.204356 | 5ms timeout，本地协作式取消 |

真实 LLM 开关对照：**BLOCKED_NO_PROVIDER**，真实质量和延迟均 UNKNOWN。
以上数字仅本机接口开销、样本很小，不包含真实推理、网络或数据库延迟。

## 6. 算法与真实独立 Benchmark

固定种子模拟 240 次、每次仅生成被选 arm 的虚拟执行延迟。训练后 LOW 上下文选 SIM_BASE，
HIGH 上下文选 SIM_ALT；重复训练得到相同 A/b。仅证明实现能表达上下文偏好，不是 IoTDB 收益。

训练使用既有修复后真实证据的 8 条可比较 SERVER VALID 样本（每 arm=4）。随后另跑现有
只读受控 Benchmark，新收集 24 条真实记录，IDs 不重叠且全部时间晚于训练截止：
12 VALID / 12 LOW_CONFIDENCE，16 次测量 server dispatch，另 2 次预热和 doctor 探测。
仍采用声明不变的 phase5-static-20261102 epoch，没有承诺数据库快照隔离。

冻结模型可评价其中 8 条同分区 SERVER 合格样本。已执行 arm 的 reward MSE=
**1.0000098217615624e-6（reward²，无量纲）**。没有 ms 延迟提升或策略价值结论；
模型选择但未执行的 arm，其 chosen_reward 为 null。没有可靠 propensity，因此不做 IPS，
匹配子集均值也不冒充无偏策略价值。留出样本没有合并进训练或发布为可用模型。

原始记录、模型门槛、模拟全部 observations、LLM 场景及评估见
[evidence/phase7_offline_2026-11-02.json](evidence/phase7_offline_2026-11-02.json)。复现：

```bash
conda activate MANTO
PYTHONPATH=src python scripts/phase7_offline_evidence.py \
  --output docs/evidence/phase7_offline_2026-11-02.json \
  --model-store /tmp/manto-phase7-models.sqlite --benchmark
```

省略 --benchmark 则仅进行离线模拟、旧证据审计、接口对照；不访问 IoTDB。

## 7. 风险与未完成条件

- 两个现有策略的差异主要是客户端结果缓存，不等价于多种物理计划优化 arm。
- 真实训练样本少且仅 LOW；无逐次 propensity、无真实收益结论，禁止真实 Bandit 部署。
- 线性假设、固定缩放与 alpha 未调优；log reward 无下界，不能照搬原论文的理论保证。
- 模型无漂移遗忘/滑动窗口，过旧数据与缓存条件变化可能失配。
- Python 求解适合小维度离线验证，不是高并发性能实现。
- LLM 实际提供方与收益/开销评估未完成；可选功能默认关闭。
- 阶段 7 离线复审修复已验证，真实模型启用条件未通过，完整验收 NOT ACCEPTED。不进入阶段 8。


最终验证：两个新增单元文件 **22 passed in 1.47s**；最终真实完整套件
**741 passed in 30.11s**（13 个真实集成用例）；阶段 5/6 独立反例
**8 passed in 2.34s**；compileall exit 0。另从独立 SQLite 恢复真实离线模型，
使用相同留出记录和当前 Registry 过滤重新评估，MSE 与原报告完全一致，未访问数据库或更新参数。


## 8. 本轮评估层修复

冻结评估拒绝留出集内重复 execution_id（含完全相同与冲突版本），防止重复加权。
审计排除完全相同重复事实，冲突证据拒绝；输入状态分布仍按输入条数统计，合格 arm
样本量按去重事实统计。隔离/禁用/未知策略及质量拒绝均写入逐样本 rejections；非预期
异常与基线不可用仍报错，不将错误标成有效证据。返回字段为兼容性追加，生产协议不变。

新增回归独立用高斯消元重算真实 A/b 和冻结误差，原始事实与报告相符。本轮修复后
全量 **750 passed in 35.83s**；严格审查结论与范围见 [PHASE7_REVIEW.md](PHASE7_REVIEW.md)。
本轮没有新采集真实 Benchmark，也没有改变本文件前面的历史测量值。

## 修复后的离线证据边界

2026-11-02复审详见 [PHASE7_REREVIEW.md](PHASE7_REREVIEW.md)。导入显式拒绝非FULL、必需身份UNKNOWN、矛盾质量原因/认证套件/资源策略版本。预期拒绝使用AdmissionError；内部错误不被过滤。冻结评估拒绝任何重复execution ID，报告输入/唯一/接纳/拒绝数量及逐条reason_code，保留原reason分类兼容性。没有修改生产Pydantic字段或数据库schema，也没有打开线上部署锁。

## 阶段7受控随机试验增量（2026-11-02）

新协议 `randomized-cache-state-v1` 详见 [PHASE7_EXPLORATION.md](PHASE7_EXPLORATION.md)。使用执行前EMPTY/READY缓存状态和背景线程配置建立独立实验分区，真实Experience先通过原准入检查，再仅更新实际arm。随机分配概率先于执行落盘，训练/留出严格隔离；离线IPS明确不等于真实部署或端到端收益。未更改旧reward/encoder或Experience schema、未将CACHE_HIT混入旧SERVER分区、未开启生产策略。当前770条均为LOW实测负载，后续仍需隔离实例及监控证据。
