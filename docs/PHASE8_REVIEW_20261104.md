# 阶段8严格验收（2026-11-04）

**结论：NOT ACCEPTED。** 工具链已有一次完整真实运行及可重放证据；比较公平性FAIL，真实跨会话/多负载/并发覆盖BLOCKED。不能将analysis_qualified=true等同阶段验收。阶段7原NOT ACCEPTED与生产NOT READY保持。

依据IMPLEMENTATION_GUIDE.md:975阶段8、:996实验定义、:1027公平执行、:1312阶段提示；本次仅审查与离线测试，不修改生产代码、不运行IoTDB实验。

## 主要发现

### F1 / P1：组间基础设施不对称，无法隔离优化/学习净效应

runtime.py:140按组分流；execution/service.py:66无请求内复用，orchestration/orchestrator.py:59启用request_scope/store_session。request_memo.py与storage/sqlite.py:284表明分别复用解析事实和连接，提交语义仍逐操作。

真实预热E1−E3差65.633ms：决策43.476、执行器非Adapter21.583，Adapter仅0.061。合成SDK调用计数guard解析28对2、特征解析2对1；E1连接8次，E3计时内1次（另1次在计时外读取经验）。不能据此给每次重复解析定价，但足以证实非研究变量不同。

最小修复：给旧服务复用现有request_scope及连接session，确保异常时关闭、每操作提交、完整关闭计入外层E2E；不得复制一套语义不同的优化或移除Validator。增加相同SQL/动作/缓存/资源下结果、派发数、持久化、失败清理等价测试和重复解析/连接计数回归。修复后另行真实对照；不从旧结果扣减估算成本，不改算法适配本轮结果。

analysis.py:366仅验证证据域/完成/正确性/负载/样本门槛，无比较公平性认证；:370固定stage_accepted=false。应在修复前的解释/验收中保留F1，不应将qualified改成accepted。

### B1：扩展配置与复现证据不足

真实完整证据只有small_count一个训练/留出SQL对、LOW、并发1、单会话。并发隔离及同配置结构一致已有合成测试，但不能代替多负载/并发真实验证或第二次完整会话。修复F1后由用户执行预声明验证，保留失败轮；不得未经授权自行压测。

### B2：学习增益和持续在线结论不成立

E4是受控训练后冻结留出（protocol.py:115、policy.py:40），不是留出期间持续更新。真实960次训练已重放，不是假学习；但各非Native组策略/命中相同。E4相对E3在空缓存/预热分别慢13.999/15.560ms，99%区间全正；A1−E4区间跨零。未证明额外学习收益或状态感知收益。负结果本身不是Benchmark算法bug，不能以改门槛/修改算法后重复使用同一留出认证来“修复”。

## 阶段任务

|任务|判定|代码与证据|
|---|---|---|
|固定SQL、参数、数据集、行数、fetch|PASS|protocol.py:32–79整数参数/SQL摘要/FULL；runner.py:48证书绑定；真实5940 MATCHED|
|warmup、次数、顺序种子、并发、负载|PASS实现 / BLOCKED真实扩展|protocol.py:100–145、:174；随机/ABBA/并发隔离测试；真实仅LOW/1|
|E0–E4|FAIL公平性；执行实现存在|runtime.py:140；F1；E4为冻结评估需准确表述|
|A1/A2|PASS实现 / BLOCKED收益|policy.py:17和:145；A1保留安全门槛，A2当前等效E3；消融与冻结测试|
|逐次原始数据|PASS|journal.py:9、:29独占创建/哈希链/终态；5940原记录|
|均值、标准差、CI、分位数、吞吐、资源、决策、E2E、正确性|PASS限定口径|analysis.py:94、:376；公式测试、原报告重放、3360逐请求成本对账；server execution UNKNOWN|
|随机/ABBA、缓存分开|PASS限定应用缓存|protocol.py:174；runtime.py:172；不冒称OS/IoTDB冷缓存|
|CSV/JSON/图表|PASS|analysis.py:395；真实产物、分页测试、同配置合成结构一致|
|报告模板与不可控因素|PASS|benchmarks/REPORT_TEMPLATE.md；analysis.py:426；EXPERIMENT_PROTOCOL.md|

## 用户要求逐项

|检查|结论|证据/边界|
|---|---|---|
|阶段所有任务真实实现|FAIL|主要功能存在，F1违反公平比较；扩展真实验收B1未完成|
|TODO/pass/mock假实现|PASS（审查范围内）|benchmark及入口搜索未发现占位实现；SyntheticEnvironment/FakeSession仅tests，REAL_IOTDB与SYNTHETIC_TEST_ONLY分域，analysis.py:365拒绝模拟认证|
|字段、单位、UTC、source|PASS限定协议|results.py:37–105校验UTC、ns/ms、资源来源、执行关联；完整外层计时可比，内部execution.end_to_end_ms截点不同不得混用；诊断CPU原始秒数与monotonic_ns明确独立|
|异常被吞掉|PASS（关键失败路径）|runtime.py:150保存error_type/last_error；runner.py:230恢复异常链、:298清理失败记事件并重抛；supervisor测试中止/超时/残留；辅助持久化失败以警告公开，不假装模型更新成功|
|未经验证IoTDB API/SQL|PASS限定2.0.6树模型及fixture范围|protocol.py:53只读守卫/摘要；runner.py:171解析门槛与reference doctor；真实5940结果验证；不推广全部方言|
|client冒充DB指标|PASS|monitoring/local_db.py:25核对DataNode身份/listener/namespace；:51独立db_host采样；whole-host不冒充进程/服务器查询指标|
|LLM绕过Registry/Validator|PASS|policy.py:145只注入注册策略建议再走原DecisionEngine；无LLM入口；executor及Orchestrator复验；A1安全门槛回归|
|错误样本正向学习|PASS限定本路径|policy.py:40要求CompletedRequest、MATCHED、质量/资源门槛、真实arm、去重与冻结；analysis.py:300独立重建更新；960 APPLIED重放一致|
|普通路径EXPLAIN ANALYZE|PASS|tree_adapter.py:522明确不使用；runner.py:168记录无服务端计时；普通执行代码未调用，doctor仅独立能力探测|
|重复执行/缓存污染/未消费|PASS限定不可变epoch|runtime.py:172明确预热/填充/重置；benchmark测试:160无隐藏双执行、:193 lane独立、:347重复行语义；tree_adapter.py:614完整消费、:648关闭；真实全部MATCHED，动态写入场景未认证|
|测试真实运行/失败覆盖|PASS离线 / BLOCKED真实扩展|本轮专项实际执行，结果在下方；旧日志含真实CPU拒绝、Ctrl+C及完整运行；不将合成并发说成真实并发|

PASS均限本次审查的实现/配置和已有证据，不表示对所有输入、主机或数据库版本的保证。

## 证据与执行

- 完整真实运行目录：benchmarks/results/phase8_small_count_20261104_101201_154809532。540/540 allocations、5940 MATCHED、960 APPLIED、3360留出；此前独立重放report逐字段一致、源码摘要一致。
- 成本审计：PHASE8_PATH_COST_AUDIT.md；docs/evidence/phase8_path_costs_20261104/{requests.csv,summary.json,call_counts.json}。真实数据与合成调用计数分开；源journal摘要保持359ca1fef28b723acd21b99984f390cc7856f9b247c7c62486f1cae7d75e9da2。
- 已有非IoTDB全回归1015 passed in124.01s，之后最后新增关联测试随模块6项通过；不把它们冒称本轮重新跑的全套。
- 本轮专项命令（默认已激活MANTO）：`python -m pytest -q tests/unit/test_phase8_benchmark.py tests/unit/test_phase8_admission.py tests/unit/test_phase8_cpu_evidence.py tests/unit/test_phase8_cli.py tests/unit/test_experiment_supervisor.py`。
- 本轮实际结果：**64 passed in83.67s**，均为非IoTDB测试。覆盖篡改/缺失记录、低质量/冻结门槛、资源拒绝、旧目录保护、Ctrl+C、子进程阻塞清理、并发隔离、产物结构复现。未运行真实数据库集成或性能实验。

最终需先修复F1并验证相同基础设施条件，再补B1证据；B2保持如实报告。当前NOT ACCEPTED，不进入下一阶段。
