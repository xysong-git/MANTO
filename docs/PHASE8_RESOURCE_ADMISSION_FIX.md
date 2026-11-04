# 阶段8资源拒绝定位与最小诊断修复

2026-11-03。范围仅限用户报告的 `ResourceAdmissionError`；不改变优化算法、资源阈值、采样协议、SQL、并发或实验计划。

## 事实与根因

原始运行目录：`benchmarks/results/phase8_small_count_v1/`。`journal.jsonl`的index=250是资源拒绝事件，UTC时间2026-11-03 11:59:44.054786（北京时间19:59:44.054786）。

| 来源/指标 | 原始值 | 实验停止线 | 结果 |
|---|---:|---:|---|
| client_host CPU | 97.009% | >80% | 超限 |
| db_host CPU | 100.000% | >80% | 超限 |
| 两类主机 memory | 67.4% | >90% | 未超限 |
| 两类主机 disk capacity | 22.0% | >90% | 未超限 |

client/DB快照年龄分别约10.063ms/28.581ms，均有效、来源标签正确且低于250ms准入期限。根据代码，首先命中client CPU检查；DB CPU也独立满足拒绝条件。CPU是整机指标，不是Python进程或IoTDB查询独占指标。

**直接原因是记录到的CPU超限，保护拒绝符合协议。** supervisor的`experiment child failed`是该异常传播后的结果，不是原始根因。此前已完成9个allocation、72条训练测量；全部记录99条（19 warmup、8 cache prep、72 measured）均MATCHED。最后一条是该allocation的WARMUP index0；结合traceback和warmup循环，失败发生于下一条预热准入，尚未dispatch该请求。没有HOLDOUT。

CPU峰值由哪个进程引起仍为 **UNKNOWN**。现有日志没有该时刻的逐进程CPU或原始累计CPU counters，不能追溯证明是IoTDB、其他作业或采样误差；不得据此修改CPU公式或把高读数丢弃。两个采样来源同处一台机器，并不构成相互独立的进程归因。

用户描述Python3.10，但traceback路径为`python3.12`，本轮`conda run -n MANTO python`实际为3.12.13，符合手册Python3.12要求。客户端2.0.6、Pydantic2.13.5、psutil7.2.2。没有更换环境或重新连接IoTDB。

## 已修复的代码问题

原错误文本只写`resource safety stop; no dispatch`，没有触发源、指标值/阈值；旧事件也未直接标出请求角色和序号。这是本轮可证实且可安全修复的诊断缺陷。

- `src/tsdb_optimizer/benchmark/runtime.py`：新增只用于诊断的Pydantic `ResourceViolation`；对同一捕获样本报告全部触发项，区分LIMIT_EXCEEDED、MISSING_METRIC、UNUSABLE_SNAPSHOT。异常保留原快照和时间，附请求role/index，继续抛出。
- `src/tsdb_optimizer/benchmark/runner.py`：原`resource_rejected`事件增加`violations`、`request_role`、`request_index`。原有失败终态、关闭及异常传播保留。
- 新增`tests/unit/test_phase8_admission.py`，使用从原事件原样提取的`tests/fixtures/monitoring/phase8_resource_rejection_20261103.json`。

原快照现在可离线重现如下错误：

```text
ResourceAdmissionError: resource safety stop; no dispatch: client_host.cpu_percent=97.009 > 80; db_host.cpu_percent=100 > 80
```

这是可诊断性修复，不是“超限情况下继续执行”的修复。没有自动等待较低样本、重新采样直到通过、重试SQL、提高阈值或删除失败批次。

## 接口与规范检查

Registry/Validator、Adapter、工作流及共享Execution/Experience协议未改；指标仍按client_host/db_host区分，原始时间不刷新，未知值不填0。CPU80%、memory/disk90%、TTL和250ms准入不变，精确等于上限允许、超过即拒绝。准入在发SQL之前，异常不触发fallback或模型更新。新增事件字段向后兼容现有离线分析器，旧证据不追溯改写。

## 实际验证

| 命令/检查 | 实际结果 |
|---|---|
| `conda run -n MANTO python -m pytest -q tests/unit/test_phase8_admission.py::test_real_rejection_reports_both_sources_values_and_limits`（修复前） | 1 failed in0.45s；真实快照可拒绝，但异常缺少指标详情，复现诊断缺陷 |
| `conda run -n MANTO python -m pytest -q tests/unit/test_phase8_admission.py tests/unit/test_cpu_counter_isolation.py` | 16 passed in0.56s |
| `conda run -n MANTO python -m pytest -q tests/unit/test_phase8_admission.py tests/unit/test_phase8_benchmark.py tests/unit/test_cpu_counter_isolation.py tests/unit/test_resource_policy.py tests/unit/test_system_monitor.py tests/unit/test_experiment_supervisor.py` | 71 passed in78.81s |
| 原日志调用`tsdb_optimizer.benchmark.analysis.analyze`（Conda MANTO、PYTHONPATH=src） | exit0，重算report与原report.json逐字段一致；原journal摘要未变 |
| `conda run -n MANTO python -m compileall -q src/tsdb_optimizer/benchmark/runtime.py src/tsdb_optimizer/benchmark/runner.py tests/unit/test_phase8_admission.py` | exit0 |

相关Benchmark/资源/采样/supervisor回归通过。所有本轮执行均为非IoTDB验证，未新增真实SQL、训练或性能实验。机器可读证据见[evidence/phase8_resource_admission_fix_20261103.json](evidence/phase8_resource_admission_fix_20261103.json)。

## 遗留问题与后续

原实验仍为aborted、9/540 allocations、72个APPLIED训练请求、0留出，无收益结论。CPU峰值来源和后续稳定性未确定，修复后的真实完整运行未验证。

先排查/等待同机CPU密集任务结束并确保独占、稳定负载；不要靠反复重跑挑选通过批次。用户决定重新运行时必须保留旧目录、使用新输出目录，不续接这72条训练数据：

```bash
PYTHONPATH=src conda run --no-capture-output -n MANTO python benchmarks/run_benchmark.py --config benchmarks/workloads/small_count.json --output benchmarks/results/phase8_small_count_v2 --immutable-epoch-confirmed --exclusive-window-confirmed
```

如果再次超过80%，仍应停止并依据新日志中的明确触发项定位。阶段8未验收，阶段7NOT ACCEPTED与生产NOT READY保留。
