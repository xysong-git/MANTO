# 阶段8实施与离线验证

2026-11-03。用户明确指定阶段8，本轮实现实验工具；保留阶段7 NOT ACCEPTED、生产 NOT READY。没有调用IoTDB、没有启动真实训练/压测，也没有改动优化算法来迎合结果。

## 已完成与文件

| 文件 | 职责 |
|---|---|
| `src/tsdb_optimizer/benchmark/protocol.py` | Pydantic配置、固定SQL/参数/hash、数据集身份、确定性random/ABBA计划 |
| `benchmark/runtime.py` | 七组服务/工作流接入、完整计时、参考对比、资源准入、独立lane |
| `benchmark/policy.py` | 原LinUCB数值实现的实验封装、真实质量门槛、A1屏蔽、原子双模型更新与冻结 |
| `benchmark/runner.py` | 独占检查、有限负载、逐次执行、并发连接/缓存/SQLite隔离、训练/留出流程 |
| `benchmark/results.py`、`journal.py` | 完整结果比较、原始记录协议、JSONL完整性与失败保留 |
| `benchmark/analysis.py` | 独立计划/参数/推断重放、block统计、CSV/JSON/图表/报告 |
| `benchmarks/run_benchmark.py`、`analyze_results.py` | 真实运行、纯离线计划与重分析入口 |
| `benchmarks/workloads/small_count.json` | 显式训练/留出查询、参数、行数、fetch mode、数据集证书、实验预算 |
| `benchmarks/REPORT_TEMPLATE.md` | 实验报告模板 |
| `tests/unit/test_phase8_benchmark.py` | 协议/失败/并发/消融/数据重放/产物一致性专项回归 |
| `pyproject.toml`、`benchmarks/requirements.lock` | benchmark可选matplotlib依赖及实际环境版本快照 |
| `docs/EXPERIMENT_PROTOCOL.md`、`docs/IMPLEMENTATION_STATUS.md`、`README.md` | 口径、阶段边界、命令和状态 |

优化器、IoTDB Adapter、原学习算法及原测试均未修改；新增代码只在显式benchmark入口加载。没有提交整仓既有未跟踪文件。

## 实际环境和验证

环境命令使用Conda `MANTO`，Python实测3.12.13；apache-iotdb2.0.6、Pydantic2.13.5、SQLGlot30.18.0、psutil7.2.2、matplotlib3.11.1。matplotlib原已安装，本轮未安装/升级依赖。服务器版本未本轮联网探测。

| 命令 | 实际结果 |
|---|---|
| `conda run -n MANTO python -m pytest -q tests/unit/test_phase8_benchmark.py --disable-warnings --maxfail=1` | 最终专项30 passed in79.49s；此前29 passed in70.40s、25 passed in53.98s |
| `conda run -n MANTO python -m pytest -q tests/unit tests/test_phase0_fixtures.py tests/test_phase2_sql_features.py` | 988 passed in117.02s；在最后吞吐CI/分页调整前，之后以30项专项验证最终分析代码 |
| `PYTHONPATH=src conda run -n MANTO python benchmarks/run_benchmark.py --config benchmarks/workloads/small_count.json --plan > /tmp/manto-phase8-plan.json` | exit0；540 allocations、4320测量请求、1620预热/准备请求 |
| `conda run -n MANTO python -m compileall -q src/tsdb_optimizer/benchmark benchmarks tests/unit/test_phase8_benchmark.py` | exit0 |

首次测试发现测试配置构造重复关键字、SQL守卫异常未转换为Pydantic配置错误，两项修正后通过。后续补充负载标签/推断记录校验、资源拒绝原始快照、同lane次序、模型充分样本与A/b独立重建。全仓离线回归通过后，最后将吞吐CI与比率点估计口径对齐、图表按28分组分页、明确模拟证据不得满足真实资格；30项专项重新通过。没有把两次测试计数相加，也没有声称跑过真实集成。

同配置两次测试实际走Adapter的模拟SDK、现有Registry/Validator和Orchestrator；各自独立输出JSONL、SQLite、冻结模型、CSV/JSON与图表。两次plan逐字节一致、请求计数一致、CSV列/报告键/产物名称一致；实际计时和UUID不要求相同。所有测试产物标为`SYNTHETIC_TEST_ONLY`，不计真实训练量或性能收益。PNG及分页第二页已实际查看，文字和坐标可读。机器可读摘要见[evidence/phase8_offline_validation_20261103.json](evidence/phase8_offline_validation_20261103.json)。

## 限制与未完成

- 真实IoTDB实验、真实并发和CPU档位验证、模型收益、消融结论、独立会话复现均未执行。用户运行命令后再依据日志验收。
- 默认示例是一个small COUNT训练/留出查询对、自然LOW、并发1，不代表广泛workload。
- E4是先训练后冻结的请求级LinUCB，未认证持续在线更新收益；请求级reward无法归因未知未来请求的缓存填充收益。
- A1去掉模型状态特征但保留安全检查；A2与E3当前等价，必须如实报告。
- 应用缓存控制不代表服务端冷/热缓存；服务器纯执行时间UNKNOWN。结果比较限完整1000行，unordered仅精确multiset。
- E0包含基线服务审计成本；所有组记录完整路径成本，模型推断包含在decision overhead与E2E内。
- 阶段7 F2仍未通过，阶段8产物不自动产生PASS或解除生产部署锁。

下一步：用户按照[实验协议](EXPERIMENT_PROTOCOL.md)执行一次预声明实验；读取原始日志核对覆盖、正确性、模型/消融和统计门槛，再进行独立会话复现。负面或不确定结果保留，不修改算法/门槛迎合既有留出结果。
