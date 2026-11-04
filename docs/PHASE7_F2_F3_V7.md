# 阶段7 F2/F3：批次统计对照与选择成本验证

后续更新：用户完成v7，专项179 passed、全量966 passed；F3真实对照与重放通过，F2额外收益仍FAIL。详见[实际结果](PHASE7_V7_RESULTS.md)。下文保留预声明与执行前记录。

2026-11-03。用户报告F1回归`49 passed in6.46s`，冻结后离线推断恢复在限定范围内通过，不扩展为训练回退续训。本轮新代码待用户测试，阶段7仍NOT ACCEPTED。

## F2最小改动

此前指纹函数调用snapshot()深拷贝完整模型、生成临时版本号和时间，再序列化丢弃这些字段。现在fingerprint_state()直接导出独立的规范数据，排除相同字段，digest材料不变。不缓存可变状态，不改A/b、奖励、alpha、lambda或选择。新增摘要等价、无snapshot调用、导出数据修改隔离测试；真实性能改善量UNKNOWN。

新增Pydantic SelectionTiming：setup/features/admission/prediction/fingerprint/finalize六段ns必须精确加和为selection_ns。分别包括对象构造/SQLite初始化、解析、快照准入及contract构造、策略分支选择、两种模型审计摘要和剩余赋值。每种对照使用相同框架；计时证据构造和journal写入沿用实验审计边界，不从原服务成本中扣费用。诊断detail_selection_*与selection_ms重叠，不得重复相加。

## F3统计对照

新增实验专用L1_BATCH，不重写生产L1。使用与LinUCB相同的已APPLIED完整批次，经相同质量准入，按审计family/server/epoch隔离，再按实际负载桶、声明长度、策略ID/版本统计count和total_service_ms。保持有限COUNT族范围，不声称任意SQL可比。

两arm都至少30批才比较算术均值；低者胜，平局Native，不足样本或S1不可选时Native。不从留出拟合阈值。只用真实完整成本，Native前缀/命中不拆成额外标签；拒绝冲突、重复归因、低质量和HOLDOUT更新。

TRAIN仍随机固定S0/S1；LinUCB提交后才更新实验L1。若L1失败，整轮中止，不自动恢复或忽略不一致。两者一起冻结，journal保存L1完整Pydantic快照/指纹；权威持久化证据仍是反馈库和journal，不增加生产L1 head。分析器从同一已提交反馈独立重建，核对evidence完全相同，并逐批重放L1选择。策略仍经过Registry/Validator/Executor。

报告增加L1分组统计、选择、指纹和比较，以及真实TRAIN奖励序列、更新前预测和累计负奖励。这些展示训练演化，不直接证明学习改善。未执行arm的反事实regret标UNKNOWN，不造标签。

## v7预声明

协议delayed-batch-v7，新训练SQL第14行/留出第15行，文件query/heavy_scale/small_scale/q03_count_aggregation.sql。本地52份历史journal首条协议未发现两条SQL；运行时仍须Native参考与独立CSV审计。

- 固定45块，LOW/MEDIUM、长度2/8。360训练批/1800请求；四策略720留出批/3600请求；合计1080批/5400请求、2345次独占边界检查。
- 原随机种子、最小样本30、学习参数、4000次bootstrap及99%区间、三个15块同向要求不变。Native与固定S1双基线收益门槛仍必须同时通过。新增l1_contrast单列，不替代或降低原门槛；未优于L1时不得声称模型胜过统计策略。
- CPU80/内存90/磁盘90停止线、30秒CPU窗口、23秒停止新批次预算、60秒supervisor、单连接均不变。新增批次若超预算则中止，不放宽保护凑样本。
- 原v2–v6调度/数量/序列化兼容；旧协议不会接受未声明的L1批次。延迟更新费用含新增L1计算且单列，未证明持续在线训练总体收益。

## 用户执行与风险

新增/扩展测试覆盖完整成本归因、同证据重放、样本门槛/平局/Registry隔离、低质量/留出拒绝、冻结去重、720批计划、双基线门槛保留、计时加和、指纹等价。人工统计只作单元测试，不是实际性能证据。

实际只做Conda MANTO下11个Python文件ast.parse静态检查和shell入口bash -n，均exit0；未运行pytest、数据库查询或新训练。

先执行离线回归：

```bash
cd /opt/program-code/MANTO
conda run --no-capture-output -n MANTO python -m pytest -q tests/unit/test_batch_statistics.py tests/unit/test_bandit_prediction_cost.py tests/unit/test_delayed_batch.py tests/unit/test_delayed_batch_restore.py tests/unit/test_linucb.py tests/unit/test_sequence_e2e.py tests/unit/test_phase7_cost_diagnosis.py
```

通过后运行新实验，输出目录必须不存在；3879777仅为上次PID，DataNode若重启须替换：

```bash
bash scripts/phase7_run_staged_validation.sh data/phase7_manual_v7_20261103 3879777
```

流程包含独占等待、pilot、训练/四策略留出、独立重放、成本分解、完整集成回归；失败即停，不覆写旧证据、不自动重试。收益看report.json而非exit0。

改动：bandit/model.py、bandit/episodes.py、experiments/controlled_policy.py、experiments/sequences.py、experiments/batches.py、新增experiments/batch_statistics.py；runner/analyzer/diagnosis/staged脚本；统计/指纹测试与文档。未改Adapter、IoTDB源码、生产L1或LLM。

F2真实收益和F3真实对照尚未验收。结果可能显示复杂模型无额外价值，必须如实保留，不换门槛。
