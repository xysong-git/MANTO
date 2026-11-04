# 代码目录与模块职责

依据 IMPLEMENTATION_GUIDE.md §5，采用模块化单体。目录示例是职责建议，不代表未认证的能力已经实现。2026-11-04整理四Agent入口；不改算法、SQL、执行次序或Pydantic协议。

```text
src/tsdb_optimizer/
├── main.py / cli.py              # CLI入口
├── agents/
│   ├── query_analysis/agent.py   # Agent 1：解析输入、输出QueryAnalysisResult
│   ├── optimization_decision/agent.py # Agent 2：唯一DecisionEngine的公开入口
│   ├── performance_monitor/agent.py   # Agent 3：监控关联、计划证据
│   └── learning_feedback/agent.py     # Agent 4：质量过滤、记录和L1建议
├── orchestration/               # 手册orchestrator职责：调度/状态/错误隔离
├── decision/                    # Agent 2核心服务：决策、成本、资源策略
├── strategies/                  # Registry、候选插件、Validator
├── execution/                   # 手册executor职责：一次执行、安全fallback
├── adapters/iotdb/              # 唯一IoTDB SDK访问层、结果缓存
├── parsing/                     # fixture验证的SQL语法子集和特征提取
├── monitoring/                  # 客户端/DB主机采样、计划解析
├── learning/                    # 经验仓库、检索、L1统计与发布
├── bandit/                      # 上下文、LinUCB、真实反馈与版本管理
├── schemas/                     # 共享Pydantic协议
├── storage/                     # SQLite事务、迁移、审计存储
├── benchmark/                   # 阶段8固定协议、比较组、独立分析
└── experiments/                 # 受控实验负载、监督、占用检查
```

## 四个Agent与兼容性

原先agents目录只有三个文件，是Agent 2实现放在decision/engine.py，而不是缺少决策过程。原agents包说明和导出停留在阶段4，也没有公开Agent 4。本轮纠正这两个结构问题。

三个已有实现实际迁移到各自agent.py，不保留重复实现；旧`from tsdb_optimizer.agents.query_analysis import QueryAnalysisAgent`等路径继续有效。四个Agent均可从agents包导入。决策入口OptimizationDecisionAgent是DecisionEngine的身份别名，明确复用唯一既有实现；不是新增空Agent或复制算法。Orchestrator已通过该入口创建默认Agent 2，BenchmarkEngine继承关系与注入仍兼容。

agents包对决策/学习入口采用惰性导出，避免已有decision→agents→learning→decision循环。三个旧模块的类路径导入保留；performance_monitor.time为现有测试/仪表兼容导出。日志logger模块名随物理迁移变化，query/decision/execution ID和结构化数据不变。

## 手册示例与实际实现对照

| 手册建议 | 当前职责归属 | 本次处理 |
|---|---|---|
| agents/*/agent.py | 四个Agent包 | 已整理 |
| agents/base.py | Agent输入输出方法不同，以既有Pydantic协议约束 | 不添加无实际用途的空基类 |
| orchestrator/controller、workflow、state_manager、error_handler | orchestration/orchestrator.py、models.py | 保持实现；不将短状态机机械拆成四份 |
| optimizer/ | strategies/与decision/ | 保持已测试边界；Registry/Validator仍为强制门禁 |
| executor/ | execution/executor.py、fallback.py、service.py | 保留稳定导入和行为 |
| parsing/parser_adapter、feature_extractor | parsing/sql_parser.py | 同一验证语法子集，不在目录整理中改解析逻辑 |
| learning/contextual_bandit、model_manager | learning/和bandit/ | L1与实验模型分别维护 |
| llm/client.py | decision/llm_advisor.py的可选安全接口 | 真实provider未实现，不创建假客户端 |
| adapters/iotdb/table_adapter.py | 未认证表模型 | 不创建假适配器 |
| schemas按模型命名文件 | 现有schemas与领域models.py | 不迁移协议或更改序列化字段 |

这次是Agent入口整理与职责说明，不宣称将所有路径机械改成示例名称。原实验implementation.json保留原路径与hash，新源码快照因迁移必然不同；旧性能结果不能冒充新源码实测。历史文档和日志不追溯替换路径。未连接数据库，也不因此解除阶段7/8验收门槛。

## 实际验证

新增test_agent_layout.py检查四个公开入口身份一致、DecisionEngine未复制及BenchmarkEngine继承兼容。test_phase8_admission.py仅迁移时钟monkeypatch目标，全部原断言保留。

```bash
conda run -n MANTO python -m pytest -q tests/unit tests/test_phase0_fixtures.py tests/test_phase2_sql_features.py
# 1033 passed / 1 failed in96.98s；唯一失败为旧模块时钟补丁位置
conda run -n MANTO python -m pytest -q tests/unit/test_agent_layout.py tests/unit/test_phase8_admission.py tests/unit/test_background_snapshot.py tests/unit/test_orchestrator.py tests/unit/test_learning.py
# 补丁目标修正后99 passed in10.76s
```

四Agent与Orchestrator独立导入在PYTHONPATH=src、MANTO下通过。没有将相关复跑冒充第二次完整回归，没有IoTDB测试。
