# 阶段7：v5成本诊断与v6修复验证

后续更新：用户已完成v6，专项154 passed、全量942 passed；正确性与模型重放通过，相对固定S1额外收益仍FAIL。详见[实际结果](PHASE7_V6_RESULTS.md)。下文“待测试”描述保留为运行前记录。

2026-11-03，Asia/Shanghai。范围仅阶段7。**NOT ACCEPTED / NOT READY**：本轮完成离线诊断和候选修复，尚未执行新测试或数据库实验，不能声称收益阻塞已经解除。遵守用户“我执行命令，你分析日志”；默认环境为Conda MANTO。

## v5日志证据

原证据目录`data/phase7_manual_v5_20261103`不修改。离线脚本`scripts/phase7_diagnose_batch_costs.py`读取全部900批/4500请求，核对每批“准备/选择 + trace阶段差 + 请求外围”与原service_ms一致。结果见[evidence/phase7_v5_cost_diagnosis_20261103.json](evidence/phase7_v5_cost_diagnosis_20261103.json)。此脚本是诊断，不替代独立验收分析器。

LOW/8，自适应减固定S1的平均批次成本，单位ms：

| 区间 | 差额 |
|---|---:|
| 批次准备/选择 | +6.812 |
| PARSED（原日志含解析与query持久化） | +12.609 |
| 请求外围（原日志不能进一步分解） | +10.297 |
| LEARNED | +4.443 |
| EXECUTED（含执行及其持久化） | -4.444 |
| 其余trace阶段合计 | -0.209 |
| 完整批次服务成本 | +29.508 |

Adapter差额-2.371ms包含在执行阶段内，不能再次相加，也不是纯服务端执行时间。批次选择包含对象构造、SQLite initialize、解析、快照准入、模型指纹及推断，不能全部称为推断时间。外围包括准入、会话打开、trace落盘和会话关闭等。旧日志未记录细分，因此SQLite checkpoint、调度、GC等具体因果均为UNKNOWN。

LOW/8单请求中位数：自适应50.809ms、固定S1 49.776ms；最大值324.606ms、151.272ms。自适应block14/index0的PARSED为216.803ms、外围56.001ms；Native另有393.130ms请求，外围338.552ms。尾部不只出现在自适应，全部保留，不剔除或截尾后重新宣称通过。

MEDIUM/2训练均值：S0 121.744ms、S1 120.451ms（包括所有完成批次）；冻结模型在留出上下文预测的平均奖励分别-0.121510、-0.120406秒，故选择S1符合训练方向。LOW/2预测S0 -0.129454、S1 -0.130002秒，优势仅约0.548ms。留出MEDIUM/2反向、收益很小，不能凭这轮留出硬编码策略或调整alpha/正则化后复用认证。本轮不修改模型参数、奖励或策略可用条件。

## 修复与计时边界

- Parser在现有请求级memo内保存私有分析事实和特征，消除parse/extract/Agent4等路径对同SQL重复执行`_analyze`。仅精确SQL、内置Parser、匹配解析/分析函数及版本时复用；每次请求清空，每桶最多32项，SQL长度上限65536。AST与特征返回深拷贝，校验query_hash/template，未知语法不缓存成功特征；不缓存负载、Registry资格、模型建议或查询结果。
- 新增Pydantic `RequestTiming`记录store_open/body/store_close/total，来自同一个monotonic时钟，分段之和必须等于total。body包含trace持久化与WorkflowResult构造；close异常保留原有显式警告。不会改变数据库调度次数。
- PARSED事件新增parsing_ms与query_persistence_ms，前者含安全分类/请求构造/解析与特征提取。SequenceRequest核对会话总计被外层请求计时覆盖、body覆盖trace。外层计时仍包括准入与计时对象构造；不从奖励中扣除任何成本。
- 旧WorkflowResult缺计时字段时，序列化不补null，避免改变历史反馈摘要。离线读取现有900个BatchFeedback，900个摘要均与journal一致；这不是新代码单元测试通过证明。
- 未优化SQLite持久化模式、事务、fsync或checkpoint，也未将其费用移到计时外。修复的真实性能影响UNKNOWN，需用户运行验证。

新增/扩展测试覆盖：分析只执行一次、特征/AST篡改隔离、函数变更失效、异常清理、metadata与未知语法、历史序列化、store会话计时及失败、计时超界、诊断成本对账与不完整日志拒绝。**这些新增测试尚未运行。** v5的929 passed仅适用于v5运行时源码。

## v6预声明与执行

`delayed-batch-v6`：新训练第12行，新留出第13行，文件`query/heavy_scale/small_scale/q03_count_aggregation.sql`；在本地50个历史journal首条协议中未发现这两条SQL，不据此声称覆盖所有外部实验。新SQL的真实可执行性与CSV结果审计由运行时检查；不臆造结果。

继续固定45块、长度2/8、LOW/MEDIUM，360训练批和540留出批；min_samples30，alpha0.1、lambda1，负秒奖励，原随机种子与99%bootstrap门槛、三个15块同向要求不变。仍须同时优于Native和固定S1；不延长运行直至显著。不调CPU/内存安全线，不影响MATAOF进程。先独占等待与pilot，再重新训练/冻结，留出不得更新模型。每个v6请求必须有新计时证据；独立分析器继续兼容v2–v5原证据。

先由用户执行不连IoTDB的回归：

```bash
cd /opt/program-code/MANTO
conda run --no-capture-output -n MANTO python -m pytest -q tests/unit/test_request_memo.py tests/unit/test_wal_request_session.py tests/unit/test_orchestrator.py tests/unit/test_sequence_e2e.py tests/unit/test_delayed_batch.py tests/unit/test_phase7_cost_diagnosis.py tests/unit/test_sql_parser.py tests/unit/test_query_features_schema.py
```

通过后，由用户执行完整新实验（若DataNode已重启，替换PID；3879777只是v5已记录PID）：

```bash
bash scripts/phase7_run_staged_validation.sh data/phase7_manual_v6_20261103 3879777
```

目录必须不存在；脚本遇错停止，不覆盖/拼接/自动重试。入口增加新回归、成本分解；完整回归仍在实验末尾显式启用IoTDB集成。收益以新report.json为准，exit0仅表示执行和重放完成。新实验如果仍未显示额外收益，应继续NOT ACCEPTED；不能把这次解析优化等同于阶段验收通过。

本轮实际执行的离线诊断命令：

```bash
PYTHONPATH=src conda run --no-capture-output -n MANTO python scripts/phase7_diagnose_batch_costs.py data/phase7_manual_v5_20261103/study/journal.jsonl --output docs/evidence/phase7_v5_cost_diagnosis_20261103.json
```

exit0，900批/4500请求，journal SHA256仍为`bb37e4d947cd4416a6710be6c5a803fae2065c9f66e8d9fbbb6d35bcb43f8416`。另以只读Python逐批构造历史BatchFeedback核对摘要，900/900一致；未写回源日志/SQLite、未训练模型、未发SQL。

静态检查：Conda MANTO下用`ast.parse`检查13个变动Python文件，语法全部可解析；`bash -n scripts/phase7_run_staged_validation.sh` exit0。这些不是pytest或性能验证。仓库实现文件仍多为未跟踪状态，`git diff --check`不能代表全部文件审查。

改动文件：`src/tsdb_optimizer/parsing/sql_parser.py`、`request_memo.py`、`orchestration/models.py`、`orchestration/orchestrator.py`、`experiments/sequences.py`；三个Python脚本`phase7_delayed_batch.py`、`phase7_analyze_delayed_batch.py`、`phase7_diagnose_batch_costs.py`及staged shell入口；五个测试文件`test_request_memo.py`、`test_wal_request_session.py`、`test_orchestrator.py`、`test_sequence_e2e.py`、`test_phase7_cost_diagnosis.py`；README、IMPLEMENTATION_STATUS、本报告及诊断JSON。
