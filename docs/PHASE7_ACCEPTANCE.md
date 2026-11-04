# 阶段 7 历史代码验收

最新验收见 [完整数据修复复审](PHASE7_COMPLETE_DATA_REREVIEW.md)。本页为此前750项测试时的历史记录，不代表当前样本量。

日期：2026-11-02。结论：**整体 NOT ACCEPTED；离线实现与三项修复 PASS，真实启用 BLOCKED / NOT READY**。
本轮重新检查代码并实际运行测试，不沿用上轮测试耗时。没有发现新的可复现 FAIL；不因测试通过而宣称阶段真实目标完成。未修改业务代码或测试。

## 逐项检查

| 项目 | 判定 | 代码位置与本轮测试证据 |
|---|---|---|
| 阶段所有任务真实实现 | BLOCKED | 手册950起的阶段7任务：离线LinUCB、编码、隔离、存储和可选建议接口有实现；真实多负载适应与收益未证实。evaluation.py:11的audit重跑显示每arm4条<30、仅LOW、无propensity和独立收益 |
| TODO/pass/mock假实现 | PASS（当前离线范围） | model.py:251的None是显式部署锁；llm_advisor.py:42无provider返回UNAVAILABLE。schemas/execution.py:137与strategies/models.py:141的pass是有意合法分支；parsing/sql_parser.py:1371元数据扩充明确不支持。模拟有SYNTHETIC标识，未冒充IoTDB测量 |
| 字段/单位/UTC/source | PASS | context.py:7、14固定17维和missing；model.py:89时间统一UTC，奖励-log1p(ms/1000)；test_linucb.py:37、94通过 |
| 异常是否可观测 | PASS | evaluation.py:82仅捕获预期准入/策略拒绝，返回ID/原因；store.py:35回滚后重新抛出；llm_advisor.py:64返回错误分类。phase7_review_probes.py内部ValueError/RuntimeError传播反例通过 |
| 未验证IoTDB API或SQL | PASS（现有2.0.6/tree范围） | 阶段7未新增数据库执行接口；tree_adapter.py:580调用既有SDK，13项真实只读集成通过。不推广到未知版本或语法 |
| client冒充DB | PASS | context.py:16分别按source_type与freshness判断，DB未知用missing；SystemSample有来源验证；监控单元与真实集成通过 |
| LLM绕过Registry/Validator | PASS | llm_advisor.py:45候选验证、58严格JSON、60隔离复查；executor.py:304再次central validation。test_llm_advisor.py:34、44、54涵盖未知ID/SQL字段/超时/资源否决/隔离 |
| 错误样本用于学习 | PASS（既有经验导入契约） | model.py:202重校验，213非FULL拒绝、215未知身份拒绝、224认证套件检查；schema仍拒绝失败/超时/MISMATCHED/fallback。上轮准入反例及模型不变检查通过；不声称可鉴别伪造来源 |
| 普通路径EXPLAIN ANALYZE | PASS | tree_adapter.py:523、790禁止普通路径ANALYZE，performance_monitor.py:41不自行执行；test_tree_adapter的never_analyze与真实workflow计数通过 |
| 双执行/缓存污染/结果消费 | PASS（不可变epoch契约内） | tree_adapter.py:614完整读取、645关闭；fallback.py:55超时拒绝回退；repeat_cache.py:1明确固定epoch。真实缓存差分、无epoch、回退和workflow执行计数通过。不保证外部写入后的缓存新鲜度 |
| 测试真实运行及失败覆盖 | PASS | 本轮全量750通过、专项53通过；涵盖原子保存失败、损坏、CAS、回滚、未知策略、质量拒绝、训练/测试重叠、重复/冲突ID、内部异常传播、连接/拉取/超时失败 |

以上路径省略前缀时，业务文件位于 src/tsdb_optimizer，测试位于 tests/unit 或 docs/evidence。

## 阶段任务细分

- PASS：LinUCB独立arm A/b/n更新、固定种子、手算及独立高斯消元验证（test_phase7_review_regressions.py:141）。
- PASS：固定编码、缩放与缺失处理；模型原子保存、版本化、CAS与回滚（bandit/store.py:41、72）。
- PASS：训练截止与执行ID隔离；重复留出整体拒绝、明确拒绝原因、冻结模型不变（evaluation.py:63）。
- PASS：无LLM正式工作流、可选LLM受限接口、默认关闭。
- BLOCKED：真实多负载偏好/独立策略收益/安全在线探索证据。历史随机平衡SERVER顺序不能替代逐次探索记录，CACHE_HIT也不能与SERVER混算。
- BLOCKED（可选增强）：实际LLM on/off质量与时延；无provider。不是要求四个Agent必须调用LLM，本地接口测试不能代替真实LLM实验。

## 本轮实际命令

在仓库根目录使用Python 3.12环境执行：

```bash
IOTDB_INTEGRATION=1 /opt/anaconda3/envs/MANTO/bin/python -m pytest -q
# 750 passed in 33.98s；包含13项真实只读集成，无skipped，获准在沙箱外运行

PYTHONPATH=src:tests/unit /opt/anaconda3/envs/MANTO/bin/python -m pytest -q tests/unit/test_phase7_review_regressions.py tests/unit/test_linucb.py tests/unit/test_llm_advisor.py docs/evidence/phase7_review_probes.py docs/evidence/phase6_review_probes.py docs/evidence/phase5_review_probes.py docs/evidence/phase5_rereview_probes.py
# 53 passed in 4.09s；与全量部分重叠，不能相加为独立测试总数

PYTHONPATH=src /opt/anaconda3/envs/MANTO/bin/python scripts/phase7_offline_evidence.py --output /tmp/manto-phase7-acceptance.json --model-store /tmp/manto-phase7-acceptance.sqlite
# exit 0；online_ready=false，LOW模拟SIM_BASE/HIGH模拟SIM_ALT，无新真实Benchmark

/opt/anaconda3/envs/MANTO/bin/python -m compileall -q src tests scripts
# exit 0
```

本轮audit：24个唯一历史执行ID，12 VALID/12 LOW_CONFIDENCE；可比较SERVER样本S0=4/S1=4；生产配置SQLite不存在。真实留出评估由已运行的独立高斯消元回归重算A/b、预测和MSE，与已有证据一致，不是重新采集Benchmark。

## 修复清单与未完成事项

没有新增已证实FAIL，因此本轮无新增代码修复清单。上轮三项已关闭，保持部署锁。

整体验收仍NOT ACCEPTED：真实启用需要独立任务补足可比样本、多负载覆盖、可审计探索与留出收益证据；缺少这些条件时不能声称自适应优化收益已成立。不建议直接进入阶段8。
