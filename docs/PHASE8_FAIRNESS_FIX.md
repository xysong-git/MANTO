# 阶段8公平性最小修复（2026-11-04）

## 变更与边界

只修改两个运行文件：

- `execution/service.py`：run加上与Orchestrator相同的request_scope，并通过execution_store_session使用既有SQLiteStore.request_session。
- `request_memo.py`：新增适用于OptimizationRun返回协议的会话包装。正常返回前关闭会话；主体失败原样抛出；完成查询后的关闭失败记录日志并给ExecutionReport添加STORE_SESSION_CLOSE_FAILED，不重复执行查询。

不改Orchestrator、SQLite提交实现、解析/Validator、策略算法、SQL、资源阈值、学习参数或数据schema。会话内仍每操作提交/失败回滚，未改为整请求单事务。所有E0/E1/E2及预热/准备均使用同一旧服务入口改动，不针对某一组单独优化。

完整公平比较继续使用RequestRecord.elapsed_ns，从准入前到service/flow返回，包含SQLite会话打开、关闭和checkpoint成本。旧ExecutionReport.end_to_end_ms的历史截点及持久化值不迁移，不冒充完整E2E；新关闭警告发生于报告入库之后，返回结果/Benchmark原始事件与日志可见，既有数据库report行不会追写。进程异常/KeyboardInterrupt继续向supervisor传播。

## 测试

新增tests/unit/test_service_request_scope.py：

- 同一请求guard解析2次、特征解析1次、SQLite连接1次；连续请求重新解析/新建连接，query ID不同，缓存不跨请求泄漏。
- 真实工作SQL通过合成SDK运行，结果MATCHED、策略S1、缓存命中不派发数据库。
- 主体失败关闭实际SQLite连接并清空作用域；关闭失败显式警告，不重派发。
- 注入关闭延迟，检查关闭完成时间位于RequestRecord端到端区间内。
- 独立SQLite连接在执行器调用前可读取已提交query/decision，证明连接复用不延迟审计提交。

修复前新测试3 failed/1 passed，实际观察28/2/8而非期望2/1/1。修复后（默认MANTO）：

```bash
python -m pytest -q tests/unit/test_service_request_scope.py tests/unit/test_decision_execution_service.py tests/unit/test_request_memo.py tests/unit/test_wal_request_session.py
```

**28 passed in2.38s**。既有服务失败/fallback和request_memo边界测试一并通过。compileall exit0。

完整非IoTDB回归 `python -m pytest -q tests/unit tests/test_phase0_fixtures.py tests/test_phase2_sql_features.py`：**1021 passed in97.00s**。与10:12真实运行快照相比仅execution/service.py、request_memo.py两个运行文件改变，原journal SHA-256仍为359ca1fef28b723acd21b99984f390cc7856f9b247c7c62486f1cae7d75e9da2。

旧成本审计call_counts.json保留修复前证据，不重写为修复后的计数。源码变更意味着旧真实实验不再认证当前实现的性能；不从旧结果扣除估计开销，不改变旧结果或算法。

## 用户真实验证步骤

所有命令默认已在MANTO；助手未启动以下数据库实验。阶段仍NOT ACCEPTED，F1工程修复已具备离线证据，真实公平对照待执行。

先在独占窗口按原配置完成一次新运行：

```bash
PYTHONPATH=src python benchmarks/run_benchmark.py \
  --config benchmarks/workloads/small_count.json \
  --output "benchmarks/results/phase8_fairness_$(date +%Y%m%d_%H%M%S_%N)" \
  --immutable-epoch-confirmed --exclusive-window-confirmed
```

完成后核对全覆盖/正确性/成本与策略命中，再在另一个独占会话重复同一命令（新目录、新模型、同配置）。两次结果独立报告，不合并失败前缀。相同SQL用于本次基础设施修复复现，不作为新工作负载泛化证明；任何算法调参须另行划分未使用留出。

扩展验证配置已单独准备 `benchmarks/workloads/small_count_multiload_c2.json`：LOW/MEDIUM × 并发1/2，原SQL、算法、种子、warmup、样本量和安全门槛不变。此配置显式启用受控CPU co-tenant，不能当普通自然负载运行。先完成并检查上述单并发结果，再由用户在允许主动CPU负载的独占窗口执行：

```bash
PYTHONPATH=src python benchmarks/run_benchmark.py \
  --config benchmarks/workloads/small_count_multiload_c2.json \
  --output "benchmarks/results/phase8_multiload_c2_$(date +%Y%m%d_%H%M%S_%N)" \
  --immutable-epoch-confirmed --exclusive-window-confirmed \
  --cpu-cotenant-authorized
```

两个配置均已离线load_config/schedule验证且重复生成相同计划。原配置540 allocations/4320测量；扩展2160 allocations/25920测量，不保证耗时或成功。扩展config SHA-256为8e6a0361b354ea7715d11dd633f713e9ad7edaa16b8caeab2a87781aecfc7e3f。未缩减样本或放宽CPU停止线；如资源拒绝，保留原始证据后分析，不自动重试。
