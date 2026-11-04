# 阶段 7 复审与最小修复（2026-11-02）

**结论：离线接口范围修复已验证；阶段 7 完整验收 NOT ACCEPTED，真实部署 NOT READY。**
本轮只审查已有离线实现并修复评估层，不接入生产 Bandit/LLM，不进入阶段 8。
阶段 5/6 已验收边界保留。该结论没有把离线模拟通过等同于真实自适应收益。

## 已确认缺陷与最小修复

修改前，原有全量测试实际复跑 **741 passed in 29.66s**，阶段 7 原有用例
**22 passed in 1.98s**。另立三个反例，实际结果 **3 failed in 1.31s**：

| 编号 | 原 FAIL | 最小修复及当前结论 |
|---|---|---|
| F7-1 | 同一 execution_id 重复出现在留出集，frozen_evaluate 重复计算样本和误差，未拒绝 | `bandit/evaluation.py:62` 在评估前拒绝任何重复 ID；审计按完整证据摘要去重，冲突重放拒绝。PASS / CLOSED |
| F7-2 | 隔离 S1 后，audit / frozen_evaluate 未处理 StrategyRegistryError，工具中断 | `bandit/evaluation.py:38/81` 将明确的 Registry 拒绝记录到 rejections，隔离样本不参与训练/误差，其他合格样本可继续。PASS / CLOSED |

不使用通用 except Exception 吞掉程序错误；训练/测试重叠、冲突身份、安全基线不可用等
仍明确失败。每个被跳过的样本带 execution_id 与原因，拒绝数量可核对。
`sample_status_counts` 是输入条数分布（含重复），`unique_execution_count` 和各分区
`valid_counts` 为去重后统计，二者不能混为样本量。审计支持一次性 iterable。

正式回归新增 `tests/unit/test_phase7_review_regressions.py` 共 9 项，保留独立三反例于
`docs/evidence/phase7_review_probes.py`。两者部分重叠，报告不把重复执行累加为独立覆盖。
扩展用例初跑另发现测试夹具未使用 Pydantic round_trip 序列化，修正后全部通过；未删测试。

## 按指南逐项验收

以下路径均相对 `src/tsdb_optimizer/`；测试路径相对仓库根目录。

| 检查 | 判定 | 位置与实际证据 |
|---|---|---|
| 阶段所有任务达到完整通过条件 | FAIL / NOT ACCEPTED | `docs/IMPLEMENTATION_STATUS.md`：可比 SERVER 每 arm=4，小于 30；真实多负载、探索概率、独立策略收益不足；真实 LLM on/off 未完成。离线接口允许先实现不等于可部署 |
| 算法、向量、单 arm 更新真实实现 | PASS（离线） | `bandit/context.py:14` 固定 17 维、缩放与 missing mask；`bandit/model.py:166` 更新 A/b；test_linucb.py:37/48/66 手算、种子复现、上下文偏好与门槛 |
| TODO/pass/mock 假实现 | PASS（限定范围） | bandit/ 与 advisor 无冒充算法的占位；`model.py:217` 的 online_advice=None 是明确部署锁，配置 Literal[False]；模拟数据标为 SYNTHETIC，真实提供方未实现并记 BLOCKED |
| 字段、单位、时间戳和来源 | PASS（本轮核查） | `context.py:14` 分离 client/db，新鲜度按执行开始时刻；`model.py:195` 重验 Experience/质量/策略/资源指纹/reward；test_linucb.py:94/134；新增 :141 重算真实 A/b 与误差 |
| 异常是否被吞掉 | PASS（修复后） | evaluation.py 的 rejections 明确列出失效样本与错误类别；store.py:35 回滚重抛；advisor.py:64/66 返回 TIMEOUT/FALLBACK 类别；新增 :93/:111 核对拒绝原因。非预期错误不转为成功 |
| 未验证 IoTDB API/SQL | PASS（本轮范围） | 本轮只改离线评价，无新增 IoTDB API/SQL。原始真实数据仍为已审计 2.0.6 tree；全量真实集成测试通过；其他版本/模型 UNKNOWN |
| client metrics 冒充 DB metrics | PASS | context.py 的来源检查与 missing mask；原 DB 代理仍缺失，不从 client 补值；test_linucb.py:94；真实原始证据中 DB 指标仍 UNKNOWN |
| LLM 绕过 Registry/Validator | PASS（接口范围） | advisor.py:37 仅给已验证候选 IDs，严格 JSON，响应后再查 Registry；Executor :92/:304 仍做 preflight/central validation；test_llm_advisor.py:16/34/54 覆盖非法 SQL 字段、未知 ID、资源否决与隔离 |
| 错误样本正常学习 | PASS（当前接口） | model.py:195 质量准入与真实/合成域隔离，只有实际 arm 更新；test_linucb.py:77/88/134；修复后隔离样本有拒绝明细，新增 :45/:55/:93 |
| 训练/评估隔离、重复计数、反事实奖励 | PASS（修复后） | evaluation.py:62 拒绝训练 ID、时间重叠、留出重复；未实际执行 arm 的 chosen_reward=null，policy_value=null；新增 :39/:65/:75/:86/:141 |
| 普通查询 EXPLAIN ANALYZE / 额外执行 | PASS（保持既有契约） | 本轮不改 Orchestrator/Executor/Adapter；tree_adapter.py:531 显式 EXPLAIN，:614 完整迭代，:645 finally 关闭；13 个真实集成用例通过。独立反例仅重放旧事实 |
| 模型原子保存、版本与回退 | PASS（离线） | bandit/store.py:41/72 使用独立 SQLite、BEGIN IMMEDIATE/CAS/回滚；test_linucb.py:106/126 覆盖 head 失败、模型体回滚、旧版本、校验和和非正定拒绝 |
| 测试真实运行与失败路径 | PASS（本次范围） | 修复后全量 750 passed、无 skipped；阶段 5/6/7 独立反例 11 passed；见命令表。没有用既有文字报告替代执行 |
| 真实 Bandit/LLM 部署及生产收益 | BLOCKED / UNKNOWN | 无真实 LLM provider；样本/负载/探索/收益不足；没有任何线上 Bandit 建议。当前不能宣称阶段 7 完整通过 |

## 实际运行命令

以下均在仓库根目录 `/opt/program-code/MANTO` 执行，python 是
`/opt/anaconda3/envs/MANTO/bin/python`；测试禁止写入 bytecode/cache。

```bash
PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=src /opt/anaconda3/envs/MANTO/bin/python -m pytest -q -p no:cacheprovider tests/unit/test_linucb.py tests/unit/test_llm_advisor.py tests/unit/test_phase7_review_regressions.py docs/evidence/phase7_review_probes.py
# 34 passed in 2.37s

IOTDB_INTEGRATION=1 PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=src /opt/anaconda3/envs/MANTO/bin/python -m pytest -q -p no:cacheprovider
# 750 passed in 35.83s，包含 13 个真实 IoTDB 集成用例，无 skipped

PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=src:tests/unit /opt/anaconda3/envs/MANTO/bin/python -m pytest -q -p no:cacheprovider docs/evidence/phase6_review_probes.py docs/evidence/phase5_review_probes.py docs/evidence/phase5_rereview_probes.py docs/evidence/phase7_review_probes.py
# 11 passed in 2.53s

PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=src /opt/anaconda3/envs/MANTO/bin/python scripts/phase7_offline_evidence.py --output /tmp/manto-phase7-review-offline-20261102.json --model-store /tmp/manto-phase7-review-offline-20261102.sqlite
# exit 0；240 次固定种子模拟，LOW->SIM_BASE、HIGH->SIM_ALT，online_ready=false
# 未指定 --benchmark，未重新采集真实 Benchmark
```

另用 ast.parse 对 src/tests/scripts 的 106 个 Python 文件逐个检查语法，exit 0。
pyproject.toml 没有配置 lint/type-check 工具，本轮未声称运行这些工具。

## 真实证据独立重算

新增回归从阶段 5 修复后真实训练记录及阶段 7 原始留出记录出发，独立累加
`A=lambda*I+sum(xx^T)`、`b=sum(rx)`，再使用不同于生产 Cholesky 的高斯消元求解。
各 arm 的参数、4 条样本数和 8 条留出预测误差与原报告一致：
`factual_reward_mse=1.0000098217615624e-6`（reward²，无量纲）。
这只验证数值实现和已记录事实，不是新采集的性能结果，不是策略净收益。
原始 Benchmark JSON 保留不改；冻结评估参数未更新、没有给未执行策略构造 reward。

结构化复审证据与本轮源码/原始证据哈希见
[evidence/phase7_review_2026-11-02.json](evidence/phase7_review_2026-11-02.json)。
本轮离线脚本输出保存为
[evidence/phase7_review_offline_2026-11-02.json](evidence/phase7_review_offline_2026-11-02.json)，
其中 independent_benchmark=null，LLM 对照明确为本地接口模拟。

## 未完成事项与下一步

最小修复清单 F7-1/F7-2 已关闭，当前没有遗留的已复现阻塞性代码 FAIL。
阶段 7 的真实启用与收益条件仍未达到，整体结论仍为 **NOT ACCEPTED**。
下一步仍在阶段 7：在真实 epoch/缓存条件可信的前提下，完善受控探索的逐次分配概率
日志、可比较动作定义和独立留出协议，再补足每 arm 样本及真实负载覆盖；缓存命中
不能冒充同源服务器样本。LLM 可选增强须有实际 provider 才能做真实 on/off 对照。

DB 主机指标代理、缓存跨进程/并发保证、概念漂移和实际线上净收益仍未验证。
不得据本轮通过的离线测试开启部署锁或直接推进阶段 8。
仓库原有文件全部未跟踪，本轮未把现有整仓内容代为提交；Git 整理仍待处理。
