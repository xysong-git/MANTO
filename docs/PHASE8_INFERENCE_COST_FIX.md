# 请求内重复模型指纹最小修复

2026-11-04。整体仍NOT ACCEPTED；不按已观察留出结果调整算法。

## 根因和范围

benchmark/policy.py的PolicyBank.advice计算整库fingerprint并写入AdaptiveAdvice.model_version；BenchmarkEngine._benchmark_decide紧接着为inference日志再次计算同一指纹。E4/A1都发生重复。新增离线反例修复前2 failed/2 passed：有建议时调用2次，无建议时1次。

唯一运行文件改动为src/tsdb_optimizer/benchmark/policy.py：日志复用本次建议PHASE8:后面的指纹，无建议时仍计算。未增加跨请求缓存，未改变模型、奖励、参数、策略选择、质量门禁、Validator、资源保护或Pydantic协议。模型恢复和下一请求仍重新计算；完整决策与外层请求计时继续包含实际成本。该字段来自内部PolicyBank，不是LLM或用户输入。

读取12:49轮models.json恢复模型，100次计算均与原fingerprint一致：4模型/960条evidence，单次均值3.72069208ms、中位3.721808ms。该数字是本机离线哈希微测量，不是数据库延迟，也不能从既有真实结果中扣除。此次仅消除第二次哈希，未证明它解释全部约23ms决策成本；余下成本不凭猜测改写。

## 验证

新增tests/unit/test_phase8_inference_fingerprint.py，覆盖E4/A1 × 有/无建议，检查调用次数、建议与日志指纹一致、无建议保留。修复后专项4 passed in0.39s。完整非IoTDB回归1025 passed in96.70s，exit0。

实际命令：`conda run -n MANTO python -m pytest -q tests/unit/test_phase8_inference_fingerprint.py`；`conda run -n MANTO python -m pytest -q tests/unit tests/test_phase0_fixtures.py tests/test_phase2_sql_features.py`。

扩展配置未改：small_count_multiload_c2.json，checksum 8e6a0361b354ea7715d11dd633f713e9ad7edaa16b8caeab2a87781aecfc7e3f，2160 allocations、25920 measured requests；本轮重复schedule相等。LOW/MEDIUM × 并发1/2，显式CPU co-tenant，不放宽停止线。

## 用户执行顺序

源码已经变化，旧两轮仍是旧实现证据。先在当前源码复跑基本配置，离线确认完整、正确、源码清单和指纹一致，再执行扩展。以下命令默认终端MANTO；助手没有启动IoTDB。

```bash
PYTHONPATH=src python benchmarks/run_benchmark.py \
  --config benchmarks/workloads/small_count.json \
  --output "benchmarks/results/phase8_hash_reuse_$(date +%Y%m%d_%H%M%S_%N)" \
  --immutable-epoch-confirmed --exclusive-window-confirmed
```

基本配置结果检查通过后，在允许受控CPU负载的独占窗口执行：

```bash
PYTHONPATH=src python benchmarks/run_benchmark.py \
  --config benchmarks/workloads/small_count_multiload_c2.json \
  --output "benchmarks/results/phase8_multiload_c2_$(date +%Y%m%d_%H%M%S_%N)" \
  --immutable-epoch-confirmed --exclusive-window-confirmed \
  --cpu-cotenant-authorized
```

不得覆盖旧结果或自动重试失败实验。已有SQL用于工程回归与扩展覆盖，不作为新的未见workload泛化证据；本次没有训练参数选择。若要开展算法调参，需要另行预声明未使用的训练/留出划分。真实LLM、动态写入、远程监控和训练回滚保持原有未认证边界。
