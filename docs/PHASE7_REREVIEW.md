# 阶段 7 缺陷修复与复审

日期：2026-11-02。本轮仅修复上轮列出的三项离线缺陷，未进入阶段 8。

结论：**三项缺陷修复复审 PASS；阶段 7 真实启用验收仍 NOT ACCEPTED / BLOCKED，部署 NOT READY**。
离线算法/接口范围可继续保留使用，不构成真实自适应收益、在线探索或实际 LLM 质量证明。

## 修改与证据

| 原缺陷 | 复审 | 修复位置与验证 |
|---|---|---|
| LIMITED / UNKNOWN 记录仍更新模型 | PASS | `bandit/model.py:202`：Pydantic 重校验后复核 FULL、必需上下文、已知结果规模、有效延迟、质量原因、认证套件及资源策略版本；拒绝发生在参数更新前。4 个上下文反例检查完整参数和去重台账不变，另3项覆盖质量原因/套件/版本 |
| 留出集重复 execution ID 计数 | PASS | `bandit/evaluation.py:63`：保留本轮开始时已有的“整体拒绝重复ID”实现；相同及冲突重放均拒绝，未通过删除测试改为接受重复数据。该选择比自动去重更保守，不改变样本权重 |
| 拒绝静默丢弃，内部错误被过滤 | PASS | `bandit/evaluation.py:82`：仅捕获 AdmissionError 和 StrategyRegistryError；返回ID、reason、reason_code、detail及输入/唯一/接纳/拒绝计数。分区不匹配明确记录。普通ValueError/RuntimeError继续传播；模型冻结检查使用显式异常，不依赖可被优化关闭的assert |

`AdmissionError` 是 ValueError 子类，保留调用者既有异常兼容性。报告旧 `reason` 字段继续表示 ValueError/StrategyRegistryError；新增 `reason_code` 承载具体原因。Pydantic 数据字段、生产数据库 schema、奖励与特征版本均未更改。

`scripts/phase7_offline_evidence.py` 同样缩小捕获范围，只跳过已由 readiness 审计记录的预期拒绝；内部错误不伪装为正常完成。没有修改线上 Agent 4；原始执行事实的真实性仍依赖已审计的经验生成链路，离线导入不是来源签名认证接口。

## 测试过程

本轮开始时仓库已有额外改动：评估去重/拒绝记录、9项 `test_phase7_review_regressions.py`、3项独立审查反例。已保留这些改动；未删除或弱化已有测试。

补充反例后先运行：**9 failed, 5 passed in 0.93s**，确认剩余准入/计数/异常传播缺口。实现后新测试的参数摘要遗漏 JSON 序列化导致4个测试自身错误，已修正构造，未将其归为业务缺陷。

首次全量：**2 failed, 748 passed in 30.36s**；两项是已有报告 reason 分类兼容性测试。采用保留原reason、增加reason_code解决，未改原测试期望。

最终命令均从仓库根目录运行，解释器 `/opt/anaconda3/envs/MANTO/bin/python`：

```bash
PYTHONPATH=src:tests/unit /opt/anaconda3/envs/MANTO/bin/python -m pytest -q tests/unit/test_phase7_review_regressions.py tests/unit/test_linucb.py tests/unit/test_llm_advisor.py docs/evidence/phase7_review_probes.py docs/evidence/phase6_review_probes.py docs/evidence/phase5_review_probes.py docs/evidence/phase5_rereview_probes.py
# 53 passed in 3.89s

IOTDB_INTEGRATION=1 /opt/anaconda3/envs/MANTO/bin/python -m pytest -q
# 750 passed in 34.29s；13项真实只读集成测试，无skipped，沙箱外运行

PYTHONPATH=src /opt/anaconda3/envs/MANTO/bin/python scripts/phase7_offline_evidence.py --output /tmp/manto-phase7-fixed-review-final.json --model-store /tmp/manto-phase7-fixed-review-final.sqlite
# exit 0；online_ready=false，模拟上下文偏好保持；无 --benchmark

/opt/anaconda3/envs/MANTO/bin/python -m compileall -q src scripts docs/evidence/phase7_review_probes.py
# exit 0
```

## 真实历史记录重放

重放既有训练及留出记录，未新增真实Benchmark、未覆盖旧证据。24条输入：8条接纳、16条排除且逐条说明原因。有效 test_rows 与旧报告逐项一致，MSE仍为 `1.0000098217615624e-6`；模型未更新，policy_value仍null，online_ready=false。

新重放证据：[phase7_rereview_replay.json](evidence/phase7_rereview_replay.json)。独立高斯消元复核A/b、预测与MSE的既有回归测试也已实际通过。

## 未解除的限制

- 同源SERVER合格训练样本每arm仍4条，小于30；只有LOW负载，没有可靠逐次propensity与独立策略收益。
- DB主机指标UNKNOWN；实际LLM provider及收益/时延对照未接入。
- S1仍受调用方不可变epoch契约限制；没有新增数据库缓存失效机制。
- online_enabled/online_ready保持false，未接入Orchestrator，未修改IoTDB、SQL或正式查询执行次数。

下一步仍须单独指定阶段任务。不能以本次缺陷关闭宣布阶段7真实启用条件满足或自动进入阶段8。
