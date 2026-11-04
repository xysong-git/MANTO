# 实现状态

## 2026-11-04：最新源码完整非IoTDB回归

在Agent入口整理及测试时钟补丁目标修正后，重新运行完整非IoTDB回归：

```bash
PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=src conda run --no-capture-output -n MANTO \
  python -m pytest -q -p no:cacheprovider \
  tests/unit tests/test_phase0_fixtures.py tests/test_phase2_sql_features.py
```

结果：**1034 passed in 97.58s**，exit 0；此前记录的1033 passed/1 failed不再出现。本轮未运行IoTDB集成、训练或性能实验，未修改运行代码；阶段7/8验收状态不变（阶段8仍NOT ACCEPTED，扩展多负载×并发真实验证待用户执行）。

## 2026-11-04：按手册§5整理Agent入口

三个Agent实现迁移到agents/<职责>/agent.py；补optimization_decision/agent.py作为唯一DecisionEngine的身份别名入口，Orchestrator默认从该入口构造。agents公开四个Agent，旧类导入路径保留，决策/学习惰性导出避免循环。未创建空基类、假表模型或LLM模块，其他既有目录通过[职责导航](CODE_LAYOUT.md)说明与手册示例的对应。算法、执行行为、Pydantic协议不变，历史实验不重写。

实际MANTO完整非IoTDB回归1033 passed/1 failed in96.98s：失败是历史测试对迁移前模块注入utc_now，未触及行为断言。仅将补丁目标改为新agent模块后，Agent入口、准入、监控、Orchestrator、学习相关99 passed in10.76s。未宣称最终完整套件重新全跑。PYTHONPATH=src导入四Agent及Orchestrator通过；首次独立导入因漏设PYTHONPATH失败，补上后通过。未运行IoTDB，阶段验收状态不变。

## 2026-11-04：co-tenant停止诊断修复

确认控制器CPU>80或memory>90会安全停止，但通用错误不含触发值，失败yield跳过load_control写入。最小修复仅cpu_load.py和benchmark/runner.py：明确超限项，finally保留控制器样本/error，停止线/负载算法/SQL/协议不变。新增4项修复前全部失败，修复后相关69 passed in61.87s。历史失败具体超限值及峰值来源仍UNKNOWN，未声称高CPU已解决；无IoTDB操作。详见[证据、边界与测试](PHASE8_COTENANT_STOP_FIX.md)。

## 2026-11-04：13:35指纹复用后真实核验

540/540单元、5940请求、960更新、3360留出；独立离线重放逐字段一致，当前源码匹配，原journal未变。E4决策约15.2–15.5ms（前轮约23ms），但相对E1仍慢20.119/23.958ms，相对E3慢8.988/12.042ms，99%区间均为正。基本配置验证完成，可由用户执行预声明多负载/并发扩展；学习收益仍FAIL，整体NOT ACCEPTED。本轮只分析日志，无IoTDB调用和运行代码修改。详见[核验结果](PHASE8_HASH_REUSE_RESULTS.md)。

## 2026-11-04：执行最小处理清单——重复指纹成本

仅运行文件benchmark/policy.py修改：E4/A1推断日志复用当前建议model_version携带的指纹，整库hash每次建议2→1；无建议仍计算，不增加跨请求缓存，不改算法/奖励/验证/数据协议。真实保存模型离线100次单hash均值3.721ms，仅微测量，不代表查询收益。新增反例修复前2 failed/2 passed，修复后4 passed in0.39s；完整非IoTDB回归1025 passed in96.70s。扩展配置重复计划一致，2160单元/25920测量。未启动IoTDB，旧真实两轮不认证此次新源码；整体仍NOT ACCEPTED。用户复跑及扩展命令见[说明](PHASE8_INFERENCE_COST_FIX.md)。

## 2026-11-04：阶段0–8整体审查

**NOT ACCEPTED**。阶段0–6在树模型2.0.6、受支持语法和固定epoch范围通过；阶段7研究收益FAIL，阶段8工具及两会话LOW/并发1复现PASS，扩展真实覆盖BLOCKED。原公平性F1已关闭，不重复列为未修复问题。当前完整非IoTDB回归1021 passed in101.51s，独立阶段5/6验收探针8 passed in1.76s；未运行IoTDB测试。README矛盾时态已澄清，未改运行逻辑、测试、配置和原始结果。逐项位置、证据、最小处理清单见[整体审查](OVERALL_REVIEW_20261104.md)。

日期：2026-11-04。当前任务：**用户明确指定的阶段 8：Benchmark、消融实验与结果分析工具**。
状态：**阶段8仍NOT ACCEPTED：F1修复已通过两次同源码/配置LOW并发1真实会话，独立复现通过；多负载/并发扩展待执行，学习收益未通过。阶段7仍NOT ACCEPTED，生产部署NOT READY**。
本次阶段切换依据用户明确指令，不追溯修改阶段7验收。v7的F1/F3限定通过、F2额外收益FAIL及初始化修复后的真实收益缺证据均保留；以下历史记录中的“未进入阶段8”指各记录当时状态。
阶段 6 已验收记录保留于 [PHASE6_IMPLEMENTATION_STATUS.md](PHASE6_IMPLEMENTATION_STATUS.md)。

## 2026-11-04：12:49独立会话复现通过（限定LOW/并发1）

第二轮与12:04轮源码清单/配置/计划/产物结构完全一致，当前源码匹配。独立重放与保存报告逐字段一致，5940 MATCHED、960训练更新、3360留出，原journal未变。E1比Native快17.819/25.481ms；E4比E1慢25.869/28.380ms、比E3慢16.105/17.870ms，方向与首轮一致。两轮分别报告，不合并扩大置信度。

同实现独立复现缺口在本配置已补齐；多负载/并发、泛化与学习收益仍未通过。阶段NOT ACCEPTED，不由助手启动数据库操作。详见[复现结果及区间](PHASE8_FAIRNESS_REPEAT_RESULTS.md)。

## 2026-11-04：12:04公平性修复后真实结果

540/540 allocations、5940 MATCHED、960训练更新、3360留出；独立重放与原report一致，源码摘要一致、原journal未变。E1/E2/E3预热决策约4ms，F1指定重复解析/连接不对称在本配置得到修复验证。E4决策仍约23ms，相对E1慢27–30ms、相对E3慢17–19ms；动作/命中相同，学习收益未通过。

不将负结果当作执行失败，不按留出结果调整算法。下一步同源码/配置独立重复会话，再执行已预声明的扩展验证；修复前轮不算同实现复现。详见[本轮结果、区间与验收边界](PHASE8_FAIRNESS_RUN_RESULTS.md)。本轮助手仅离线分析，未改运行代码。

## 2026-11-04：按验收清单修复F1

仅改execution/service.py、request_memo.py两个运行文件：旧服务复用既有request_scope/SQLiteStore.request_session，适配OptimizationRun关闭失败警告。保留Validator、策略、每操作提交、异常传播及计入关闭成本的完整外层E2E；无数据协议迁移。

新增测试修复前3 failed/1 passed；修复后专项28 passed in2.38s，完整非IoTDB回归1021 passed in97.00s，compileall exit0。缓存命中guard解析28→2、特征解析2→1、连接8→1；逐操作提交可见、零额外派发、跨请求隔离与关闭行为均验证。原10:12日志摘要未变，不追溯改写收益。

原配置独立两会话复跑及LOW/MEDIUM×并发1/2扩展配置已准备并离线验证计划。未启动IoTDB实验；F1工程修复通过不等于公平收益已实测，阶段保持NOT ACCEPTED。命令、限制和真实后续步骤见[最小修复说明](PHASE8_FAIRNESS_FIX.md)。

## 2026-11-04：阶段8严格验收

结论NOT ACCEPTED。F1/P1：E0/E1/E2与Orchestrator组请求内解析/SQLite连接复用不对称，不能隔离学习或协作净收益；最低修复为复用既有机制并保持验证、提交、关闭与完整计时语义，修复后再做新真实对照。真实跨会话/多负载/并发BLOCKED；冻结模型更新真实，但学习/状态感知收益未证明。无发现本路径LLM越权、隐藏双执行或模拟数据冒充真实证据。

本轮实际非IoTDB专项64 passed in83.67s；未改运行代码、未启动数据库实验。逐项PASS/FAIL/BLOCKED、代码位置、测试证据及最小修复清单见[严格验收报告](PHASE8_REVIEW_20261104.md)。不进入下一阶段。

## 2026-11-04：执行路径成本审计

离线3360条留出成本恒等式、14单元均值与原报告一致。E1−E3预热差65.633ms中，决策43.476ms、执行器其他21.583ms，Adapter仅0.061ms。发现旧服务无request_scope/store_session，而Orchestrator有；合成调用计数确认guard解析28对2、特征解析2对1，SQLite连接8对计时内1。基础设施不对称构成比较公平性缺项，不能把E4相对E1优势归因于学习。

E4−E3预热差15.560ms几乎全在决策差15.587ms；空缓存类似。两者同动作/命中，本轮未证明学习净收益。新增离线账本与复现脚本，原日志不变、未改生产或算法、未连IoTDB。详见[成本、代码证据、实际命令及最小修复建议](PHASE8_PATH_COST_AUDIT.md)。阶段8仍未验收。

## 2026-11-04：10:12运行完整完成

540/540 allocations，5940请求全部MATCHED、960训练更新APPLIED、3360留出测量，无资源拒绝。离线独立重放与原report逐字段一致，运行源码摘要一致。E4比Native快约60ms，但比E3无学习慢14–16ms，99%区间均为正；非Native各组动作/命中数量完全相同，不能把相对E1优势归因于学习。E1/E2与E3路径成本差需离线分解；单会话LOW/并发1不支持整体验收。详见[首次完整结果与边界](PHASE8_FIRST_COMPLETE_RESULTS.md)。本轮仅离线分析，未运行数据库实验或更改原产物。

## 2026-11-04：09:50 CPU停止与原始计数取证补齐

原事件227确认TRAIN block2 TRAIN_S1第二条测量前client99.711%、DB99.645%触发80%保护，快照72.102/107.259ms，内存/磁盘未超限。91请求、64训练更新，无留出认证。现有DataNode日志未提供GC/进程CPU历史，来源仍UNKNOWN，不能宣称已修复实际高CPU。

最小补齐取证：新增benchmark/cpu_evidence.py独立Pydantic证据，runner包装实时采样并在拒绝时写出按原snapshot_id绑定的前后CPU累计计数、时钟和MANTO/DataNode进程累计CPU。原计数原样交给原采样器，32份有界内存；未知为null，不改变阈值/重试/算法/SQL/SystemSnapshot。额外监控成本保留，待实际新日志归因。

专项28 passed in0.61s；补runner关联后模块6 passed in0.59s；compileall exit0。原实验离线重放与原报告一致、所有结果文件不变。详见[根因边界、改动与实际命令](PHASE8_CPU_EVIDENCE_FIX.md)。本轮没有IoTDB查询或性能实验。

完整非IoTDB回归1015 passed in124.01s；最后新增的runner证据关联测试未在该次收集列表中，已随模块6项单独通过。未将新测试计入1015，也未宣称真实高CPU已解决。

## 2026-11-04：Ctrl+C与无进度提示修复

用户重建v1后的本轮journal含4334事件、2054请求（374预热、1490测量、190缓存准备）、960训练更新、已冻结模型，末条请求位于HOLDOUT block4 A2。请求时间北京时间09:00:51–09:06:10，无resource_rejected，终态是supervisor的KeyboardInterrupt；主机只读检查无残留run_benchmark.py。不能将此次中断归因于旧目录或CPU保护。

确认CLI缺陷：长时间无进度提示；捕获BaseException后包括Ctrl+C也自动export，导致退出延迟且最后仍抛traceback。仅改benchmarks/run_benchmark.py：启动/阶段完成进度提示；supervise清理后的KeyboardInterrupt转exit130并跳过导出，保留aborted证据。supervisor、算法、SQL和Pydantic协议不改；普通失败仍保留原导出行为。Conda绝对可执行文件activate不能修改父shell；推荐原有conda run -n MANTO，无需改shell配置。

新增CLI中断及进度测试、supervisor真实子进程中断清理测试。修复前新测试因KeyboardInterrupt退出pytest（exit2，no tests ran），修复后 `conda run -n MANTO python -m pytest -q tests/unit/test_phase8_cli.py tests/unit/test_experiment_supervisor.py tests/unit/test_phase8_admission.py`：**28 passed in3.88s**。未自行运行IoTDB实验，未更改用户日志。完整真实实验仍因中断未完成；Conda外层自身仍可能显示KeyboardInterrupt，退出码130不表示成功。

## 2026-11-03：阶段8第二次CPU停止

用户授权后完成主机只读取证：21:23的30秒整机平均6.23%、1秒峰值8.47%；另100个163–299ms短窗最高21.727%，未重现82.809%。当前主要占用者DataNode与MATAOF基准程序；当前MATAOF/vLLM进程启动均晚于21:15故障，不能追溯归因。原始数据与限制见[取证记录](evidence/phase8_cpu_probe_20261103_2123/README.md)。未启动IoTDB查询或改变外部进程，历史峰值来源仍UNKNOWN。

时间戳目录211539的原事件93确认DB_HOST CPU82.809% >80%，快照15.340ms，CLIENT_HOST42.718%；TRAIN block0首次WARMUP准入停止。32条已记录请求MATCHED，24测量/训练更新、无留出测量。运行源码摘要与实验一致，离线重放与原report一致，原文件均未变。峰值进程及实际采样窗口UNKNOWN，未证实运行代码缺陷，故不改变保护或算法。

新增原事件fixture与latest→Lane.request零派发回归，专项35 passed in0.66s，相关回归78 passed in76.49s；本轮未启动IoTDB实验。详见[事实、测试、限制及后续取证](PHASE8_CPU_STOP_20261103_211545.md)。真实完整实验仍未完成。

## 2026-11-03：阶段8重复输出目录修复

复用已存在的 `phase8_small_count_v1` 触发 supervisor 防覆盖保护，属于预期拒绝，不涉及数据库或环境版本。发现相关 CLI 缺陷：捕获启动拒绝后仍可能 export 旧目录。仅修改 `benchmarks/run_benchmark.py`：入口明确提示改用不存在的新目录，supervisor 抛出 FileExistsError 时直接传播、禁止导出旧结果；本次运行中途失败的部分报告导出保持。未改变 Pydantic、SQL、算法或资源门槛，符合手册§7数据协议和§13实验隔离要求。

新增 `tests/unit/test_phase8_cli.py`，修复前2 failed/1 passed，修复后连同supervisor专项10 passed in3.39s。compileall exit0。README及实验协议命令改用时间戳路径，不预创建目录、不删除原证据。本轮未启动IoTDB实验；先前CPU超限原因仍独立存在，换目录不保证实验完成。

实际专项命令：`conda run -n MANTO python -m pytest -q tests/unit/test_phase8_cli.py tests/unit/test_experiment_supervisor.py`。静态命令：`conda run -n MANTO python -m compileall -q benchmarks/run_benchmark.py tests/unit/test_phase8_cli.py`。实际旧目录入口验证返回预期exit2，24个文件前后SHA-256均一致；首次验证脚本漏设PYTHONPATH导致ModuleNotFoundError，补上`PYTHONPATH=src`后验证通过，未改环境或依赖。

完整非IoTDB回归：`conda run -n MANTO python -m pytest -q tests/unit tests/test_phase0_fixtures.py tests/test_phase2_sql_features.py` → **1006 passed in122.32s**。真实实验仍待用户使用新目录执行。

## 2026-11-03：阶段8资源拒绝定位

用户运行`phase8_small_count_v1`在TRAIN block2的预热准入处中止。原事件index250：client CPU97.009%、DB主机CPU100%，均超过80%；memory67.4%、disk22%，快照新鲜，保护拒绝符合协议。具体CPU峰值进程来源UNKNOWN；不是Python环境版本错误，当前Conda MANTO实测3.12.13。

仅修复两运行文件中的诊断：异常显示来源/指标/实测值/阈值，JSONL附结构化触发项及role/index；阈值、快照时效、停止行为、算法与SQL不变。原快照复现诊断缺陷后，针对性16 passed in0.56s、相关回归71 passed in78.81s、compileall exit0。详见[本轮定位、测试与限制](PHASE8_RESOURCE_ADMISSION_FIX.md)。

原日志完整离线重放与原report逐字段一致、journal摘要未变：99条记录均MATCHED，9/540 allocations完成、72 APPLIED训练、0留出，仍aborted且不合格。本轮未调用IoTDB，真实修复后复跑待用户在稳定负载下使用新目录执行；不得宣称高CPU问题已消失。

## 2026-11-03：阶段8实验工具

新增独立Benchmark协议、固定SQL/整数字面量参数/数据集证书绑定、可配置warmup/轮数/随机或两组ABBA/1–4并发/实测负载；实现E0–E4、A1状态特征屏蔽、A2无历史/学习对照。复用既有Registry/Validator、Adapter、Orchestrator和LinUCB数值实现，生产路径及模型开关未改。

训练和留出SQL隔离、真实合格请求标签、训练完成后冻结；每lane独立连接/缓存/SQLite。原始JSONL保留准备、失败、fallback和实际尝试，独立分析重放计划、正确性、训练参数及推断，输出CSV/JSON、SVG/PNG及报告。客户端/DB指标分开，服务端纯执行时间UNKNOWN，应用缓存空/预热不冒充OS/IoTDB冷/热状态。

全仓非IoTDB回归**988 passed in117.02s**；最后吞吐CI口径/图表分页调整后专项**30 passed in79.49s**。配置plan命令exit0：540 allocations、4320测量、1620预热/准备；同配置两次模拟运行的plan与产物结构一致，图表及分页已渲染检查。全部为非IoTDB验证，不计真实样本或收益。详见[阶段8实施与验证](PHASE8_IMPLEMENTATION.md)及[离线证据摘要](evidence/phase8_offline_validation_20261103.json)。

执行协议、统计口径和用户一条运行命令见[EXPERIMENT_PROTOCOL.md](EXPERIMENT_PROTOCOL.md)。真实多负载/并发、收益、消融和跨会话复现均待用户执行后分析；不宣称阶段8验收通过或解除阶段7门槛。


## 2026-11-03：初始化修复后重新验收（最新）

结论NOT ACCEPTED。F1冻结推断恢复、F3真实统计对照、SQLite初始化修复分别在限定范围PASS；F2最近真实v7额外收益仍FAIL，最新初始化修复后真实收益BLOCKED（缺新证据）。本轮执行完整离线重放，结果与v7原report.json逐字段完全一致，模型/L1均复现，journal摘要未变，无IoTDB调用。

当前相对v7运行快照仅storage/sqlite.py一份运行代码改变，不能用旧实验认证该修改的性能。上一轮针对性124/全部unit863通过无需无条件重复；v7用户179/966通过仅属当时运行。详见[重新验收表与边界](PHASE7_REACCEPT_AFTER_INIT_FIX.md)。不进入阶段8。

## 2026-11-03：共享准备路径最小修复

定位到每批Orchestrator构造重复调用SQLiteStore.initialize；同一当前schema仍执行迁移脚本和列检查。修复仅在成功初始化后、数据库身份/user_version/schema_version均一致时跳过重复迁移，保持版本校验、文件替换/schema变更检测和失败重试。离线临时库trace由27条SQL缩减为2条版本读取，未扣除真实计时成本、未改持久化策略。

助手自行执行非IoTDB测试：针对性124 passed in16.68s；全部tests/unit为863 passed in43.49s。未运行数据库集成/训练/压测，未改v7证据。修复减少共享开销但不能保证F2相对收益，阶段7仍NOT ACCEPTED。详见[根因、边界与实际命令](PHASE7_SQLITE_INIT_FIX.md)。

## 2026-11-03：v7完成，F3通过、F2仍未通过

360训练批、720四策略留出批，5400条请求全部VALID/MATCHED，360次APPLIED；全部训练/留出单元45批、目标稳定率100%、2345个独占检查通过。两种模型训练evidence一致、参数/统计/选择独立重放通过；持久化head校验、两SQLite完整性ok，89个运行源码/协议hash在结果文档更新前一致。用户专项179 passed in14.51s，全量966 passed in44.82s；此前离线81 passed in9.58s。

相对Native差-48.420ms、99%区间全负，PASS；相对固定S1差-3.083ms，区间[-12.100,+6.051]，FAIL；相对L1差-2.736ms，区间[-10.702,+5.050]，未证明更优。L1和LinUCB四场景动作完全一致（长度2 S0、长度8 S1）。F3“对照缺失”已解决，不等于模型收益通过。F2仍未解除，不进入阶段8。详见[v7结果与限制](PHASE7_V7_RESULTS.md)。

协作约定更新：助手可自行执行不调用IoTDB的测试、静态检查与离线重放；涉及IoTDB的集成/训练/压测仍由用户执行，已写入AGENTS.md。

## 2026-11-03：F1用户回归通过；F2/F3准备v7（历史）

用户报告F1回归49 passed in6.46s，在冻结后独立推断恢复范围通过，不扩展为训练续跑或生产部署。F2减少模型指纹无用深拷贝/临时ID并新增六段准备计时；F3增加同APPLIED证据、同完整服务成本的L1均值对照，样本门槛30，冻结后禁止更新，独立重放核对两模型evidence一致。

v7新训练14行/留出15行，360训练批+720四策略留出批；原45块、安全线、参数及Native/固定S1双基线门槛不变。新增训练实际奖励/更新前预测序列，反事实regret UNKNOWN。11个Python文件静态语法和shell语法检查通过；新测试/真实验证未运行，F2/F3仍待用户验收，阶段7NOT ACCEPTED。详见[方案、风险与命令](PHASE7_F2_F3_V7.md)。

## 2026-11-03：F1最小修复已实现（历史准备记录）

DelayedBatchStore增加仅用于冻结、排空后的独立推断版本恢复：校验历史快照、scope/配置和账本包含关系，使用训练revision+推断generation防止旧调用覆盖；推断指针与Pydantic审计记录同事务提交。训练head、版本及反馈归因记录不回拨，不解除冻结。原实验消费路径不切换到恢复指针。

新增旧版预测恢复/重启/切回、过期令牌、损坏/跨范围目标、冻结/PENDING与原子失败测试。仅两Python文件ast.parse静态检查通过；pytest未执行，F1尚不能标记验收通过。F2/F3保持未完成，阶段7仍NOT ACCEPTED。详见[根因、接口边界与用户测试命令](PHASE7_BATCH_RESTORE_FIX.md)。

## 2026-11-03：严格审查补充

阶段7 NOT ACCEPTED不只因收益门槛FAIL。当前DelayedBatchStore保留历史版本但缺少受支持的旧版本回退及队列/归因一致性流程（旧OfflineModelStore.rollback不能替代）；v6缺少手册§10.4要求的同批次奖励L1统计对照。两项分别列F1/P1、F3/P2；收益门槛列F2/P1。持续在线总体收益、动态写入缓存失效和广泛负载外推仍BLOCKED。无LLM路线允许完整运行，未配置真实provider不单独否决它。

未发现当前受控不可变epoch范围内已证实的P0级错误；不能据此保证全输入无致命缺陷。942 passed不覆盖不存在的批次旧版本回退流程。本轮仅静态审查/读取已有证据，未运行测试或改运行代码。详见[逐项验收、代码位置、测试证据与最小修复清单](PHASE7_REVIEW_V6.md)。

## 2026-11-03：用户完成v6，验收仍未通过

900批/4500请求完整完成，全部VALID/MATCHED；360批APPLIED，无拒绝或待处理。持久化模型校验和、冻结指纹与独立报告一致，两SQLite完整性ok，88个运行源码/协议hash在结果文档更新前一致，1985次占用检查通过。用户专项154 passed in9.29s，全量942 passed in43.14s；助手未重跑。

自适应−Native为-57.124ms，99%区间[-64.166,-49.914]，收益PASS；自适应−固定S1为-0.525ms，区间[-7.846,+6.826]，三个分段中一段反向，额外收益FAIL。两档均长度2选S0、长度8选S1，本轮没有同一长度随负载改变选择的证据。目标训练/留出各有一个单元44批，由选择快照实际LOW而非分配MEDIUM造成；所有成本保留，质量合格的360批更新不矛盾。

请求内解析修复及细分计时已通过本次回归和真实流程，仍不能宣称阶段7通过。当前差异接近持平、短批策略差异小，下一步先离线检查准备/选择成本，不盲目重复实验或降低门槛。详见[完整结果与剩余限制](PHASE7_V6_RESULTS.md)。

## 2026-11-03：v5成本定位与v6候选修复（历史准备记录）

对900批/4500请求完成离线成本对账。LOW/8自适应额外29.508ms主要出现在PARSED、请求外围与批次准备；旧计时不能证明SQLite/调度/GC具体因果。MEDIUM/2的S1选择符合训练均值和冻结模型评分，仅约1ms优势，不能根据留出反转认定算法错误或调参复用认证。

修复同一请求内重复AST分析：受限私有分析/特征memo、深拷贝隔离、实现变更失效；增加Pydantic请求会话计时与解析/持久化分段。900份历史反馈摘要在新协议读取后全部保持一致。新增回归**未运行**，不得沿用v5的929 passed声称新代码通过。

v6预声明第12/13行新训练/留出，重新训练，不复用v5模型；45块、两档负载、安全线、奖励、参数和双基线收益门槛不变。用户执行回归和完整实验，助手分析完成日志；本轮没有启动测试或数据库查询。阶段7仍NOT ACCEPTED，生产关闭。详见[诊断、改动、风险与命令](PHASE7_V6_REPAIR.md)。

## 2026-11-03：用户完成v5，离线验收结果

360训练批次/1800请求、540冻结留出批次/2700请求完整结束，4500条VALID/MATCHED；359次模型更新，1批负载变化拒绝，队列已排空。训练各单元44–45批、留出各单元45批，负载稳定性与1985次独占检查通过；参数重放、持久化模型摘要、两份SQLite完整性均一致。87个源码/协议摘要在结果文档更新前未变。

用户日志：专项45 passed in4.23s，全量929 passed in39.59s。助手仅作离线读取/核验，没有自行重跑测试。spawn隔离已通过此次专项及完整真实流程，不再是待执行状态。

自适应长度8在LOW/MEDIUM均选S1、各270次真实命中；长度2在LOW选S0、MEDIUM选S1。自适应−Native平均−48.184ms，99%区间[-61.631,-34.213]，三批均值同向，PASS；自适应−固定S1平均+8.520ms，区间[-2.517,+21.465]，三批均值不同向，额外收益门槛FAIL。不能把点估计更慢说成显著更慢，也不能只看Native通过便宣布阶段通过。

当前阻塞转为稳定超越固定S1的完整服务收益及跨会话复现，生产部署仍关闭。下一步先离线分解LOW/8额外客户端成本和MEDIUM/2评分，再准备必要的新实验命令由用户执行，不在既有留出上调参后复用认证。详见[完整结果](PHASE7_V5_RESULTS.md)及[证据核验](evidence/phase7_manual_v5_audit_20261103.json)。以下为历史记录。

## 2026-11-03：v5准备完成，改为用户执行测试（最新）

用户明确选择后续由用户执行测试/长测，助手在完成后读取日志分析，不再自行启动测试或持续轮询；已记录到AGENTS.md。当前自动流程已exit1结束，没有继续运行的MANTO实验。

独占启动检查已真实识别MATAOF的20个连接。其后两次v4均在完成4个LOW批次/20请求后、首次MEDIUM启动时出现外部PID警报。不能继续将退出PID认定为MATAOF：代码中fork继承已打开Adapter描述符，结合psutil按socket inode归属PID的实现，存在自有worker被误判的明确风险。v5已改为显式staggered-spawn-v2并增加描述符隔离回归，旧profile保留，外部客户端守卫不绕过。**spawn修复及v5尚未运行测试，待用户执行，不能引用旧PASS宣称已验证。**

固定45块预算已预声明（360训练/540留出批次），各单元最低30有效批次及所有资源/收益门槛不变。此前样本预算改动专项43 passed in3.50s；更早非真实集成回归912 passed /13 deselected；以上均在最新spawn修改之前。两个v4失败前缀各4次更新可独立重现、各20条VALID，SQLite完整性均ok；无留出、收益UNKNOWN。

执行入口：`bash scripts/phase7_run_staged_validation.sh data/phase7_manual_v5_20261103 3879777`（在项目根目录，输出目录必须新建）。脚本先跑专项测试，再等待300秒独占、预检、完整实验、独立分析、全量真实集成回归；失败即停。详见[具体文件、原始证据、命令与未完成项](PHASE7_STAGGERED_VALIDATION.md)。下一步：读取用户运行目录，依据实际覆盖/质量/参数重放/收益结果验收，不预先标记阶段通过。

## 2026-11-03：共享实例占用定位与启动守卫（进行中）

发现MATAOF基准进程PID1202893占用同一IoTDB的20条连接。它自然完成后，新PID1237877又启动`p1-repeat-wide-i9.json`；MANTO守卫在下一轮加压前拒绝，零SQL。用户明确选择等待MATAOF完成，不结束外部任务。当前等待连续60秒无外部连接，之后先重做完整无SQL预检，再进行预声明v3训练/留出。

新增错峰压测profile（38进程理论上限59.375%/64核，不含外部活动）和Linux TCP占用守卫；保持CPU80%、正式准入250ms、每单元30样本门槛。第一次错峰预检仍在81.260%处保护停止，不能宣称负载修复已经实测通过。共享占用证据已确认，峰值具体因果贡献仍UNKNOWN，连接数不是活跃查询数。

当前验证：专项44 passed；IPv4映射IPv6补测4 passed；非真实集成回归912 passed / 13 deselected in41.48s；历史中止日志/篡改复审9 passed in14.61s；compileall exit0。真实启动守卫测试exit1符合预期，仅protocol/instance_occupancy/aborted三条事件，没有SQL或CPU加压。完整真实集成待独占窗口后执行。

详见[负载与独占验证](PHASE7_STAGGERED_VALIDATION.md)、[已归属连接证据](evidence/phase7_shared_instance_verified_20261103.json)。本条是进行中状态，不代表训练或验收已完成；阶段7仍NOT ACCEPTED，生产部署关闭。

## 2026-11-03：有限批次归因与延迟反馈（最新）

实现已声明长度2/8的完整批次反馈：首次Native、填充和实际命中合并为一次真实服务成本标签；独立SQLite队列在批次关闭后接收、后续调度消费，原子发布/ACK、重启恢复、跨批执行ID去重、质量拒绝、冻结后禁止训练。生产默认同步L1与Bandit部署锁保留；显式实验使用record_only持久化及独立批次模型，不预测开放流的未知未来请求。

两次从零训练均保留，不能合并凑样本：v1完成201批/1008请求，201次真实更新后因快照准入不可用中止；具体被拒快照未留存，超龄量UNKNOWN。v2收紧返回快照年龄至200ms（正式准入仍250ms），完成98批/492请求，96批APPLIED、2批因批内负载变化REJECTED；随后MEDIUM窗口DB_HOST CPU **83.183% > 80%**，快照年龄121.31ms，按协议停止。两轮均未冻结、留出均为0，**收益UNKNOWN，不生成置信区间，不宣称重新训练/多负载验证完成**。

独立重建两轮已提交模型均一致，四个SQLite integrity_check均ok；v2启动记录的85个实现/协议文件在结果文档更新前hash全部一致。专项68 passed in 7.98s；完整918 passed in 40.52s（含13项真实集成，无skipped）；证据篡改/中止拒绝9 passed in 14.10s；compileall exit0。

详见[协议、实现、命令与失败原因](PHASE7_DELAYED_BATCH_FEEDBACK.md)及[失败证据与模型重放](evidence/phase7_delayed_batch_failure_audit_20261103.json)。未完成：稳定达到各单元30个有效完整批次的训练、冻结后360批独立留出和收益验证。下一步先定位CPU峰值/批内跨档的负载稳定性；在停止线保持不变的前提下再预声明完整新实验。以下为历史结果。

## 2026-11-03：完整序列与阻塞定位（当前最新）

默认运行环境已按用户要求明确为 **Conda MANTO**，使用 `conda run -n MANTO python ...`；历史Python版本只作观测，不是环境名或固定路径要求。

新增预声明完整序列实验与独立分析：30块×长度1/2/8×三策略，首次访问、填充、完整Orchestrator、同步学习、存储关闭、有界快照等待和安全回退均计入请求成本；按分配策略保留低质量成功成本，但不用于Bandit更新。v1快照前置检查中止，613条原始请求与aborted终态保留；修复后v2和v3各990条全部VALID/MATCHED，模型冻结，原始结果不拼接。

发现并修复真实L1解析缓存瓶颈：旧128条LRU对810条历史经验连续扫描三次，2430 miss / 0 hit；新缓存仅保留摘要键和已验证不可变事实，4096条及4MiB估计预算，内容变更失效。真实重复扫描约23ms，旧约174ms，L1统计/样本IDs/revision完全一致。未更改奖励、准入、统计公式或数据库协议；超过缓存容量仍可能重新解析，完整统计重建仍是线性工作。

v3长度8序列：S0均值1174.276ms、固定S1 690.975ms、自适应1056.690ms。固定S1−S0为−483.301ms，99%区间[-778.763,-312.650]；自适应−固定S1为+365.715ms，[279.794,457.351]，三批次同向更慢。自适应全部选择S0、零命中；当前单请求奖励冻结模型未实现有效填充/复用。长度1/2无稳定收益差异，不声明S0最快。v2/v3不同SQL/时段不能用于因果加速比。

一次有界LOW/MEDIUM预检各两轮通过（每轮20个真实样本），MEDIUM范围51.930–63.705%、53.492–58.404%；未调整CPU/内存阈值、未改IoTDB配置。预检零训练/留出查询，不能解除完整多负载样本及收益门槛。

详见[设计、命令、结果及未完成项](PHASE7_SEQUENCE_VALIDATION.md)。下一步必须在新的预声明协议中解决填充动作的后续收益归因、生产延迟反馈调度，并在修复后的成本环境重新训练及独立多负载验证；不得用本轮留出数据调参再宣称通过。生产Bandit关闭，不进入阶段8。以下保留历史记录。

本轮最终全量 **900 passed in42.29s**（含13项真实IoTDB集成，无skipped）；两批证据重放/篡改拒绝 **20 passed in79.35s**；compileall exit0；两份SQLite integrity_check=ok，v3运行源码与启动摘要一致。命令与初次失败记录见上述阶段报告。

## 2026-11-03：完整请求奖励与策略适用性（最新）

**阶段7显式奖励对齐 PASS；自然LOW下S1命中收益验证 PASS；多负载/自适应额外收益仍 BLOCKED，阶段7 NOT ACCEPTED / 部署 NOT READY。**

新版本`negative-e2e-seconds-v1`使用`-end_to_end_ms/1000`，在Orchestrator完整返回（含同步学习、持久化及连接关闭）后收集；旧Adapter奖励隔离，不混训。新增有界新鲜度等待、训练冻结后无用工作流释放；不放宽500ms TTL、Registry/Validator或质量门槛。

完成小规模v8和大规模v9两个独立自然负载实验，各240训练+216留出，每个缓存×策略单元36条，全部VALID/MATCHED且模型独立重建一致。小规模READY：S1−S0平均−27.729ms、99%区间[-52.804,-12.236]；大规模READY：−103.235ms、[-130.138,-78.696]，均三批次同向。EMPTY两个规模都无稳定差异，不能宣称S0最快。不同SQL/协议不支持跨规模因果比较。

自适应小规模EMPTY→S0、READY→S1；大规模READY全部S1，EMPTY为31次S1/5次S0。选择变化不等于额外收益；未证明优于固定S1。人工CPU预检资源保护中止，三份失败批完整保留；自然负载协议强制不能满足原多负载门槛。生产L1默认Adapter目标尚未迁移，生产Bandit保持关闭。

最终全量882 passed in34.27s，含13项真实集成；compileall exit0。详见[完整实现、命令、场景结果与复审](PHASE7_E2E_REWARD_SCENARIOS.md)。下一步是包含填充成本与安全回退的完整请求序列验证、稳定多负载和延迟反馈调度，不进入阶段8。以下保留历史记录。

## 2026-11-03：开销修复及实际复测（最新）

**开销修复与回归 PASS；收益认证 BLOCKED；阶段 7 NOT ACCEPTED / 部署 NOT READY。**

完成请求内确定性解析/只读校验复用、L1 内容缓存、后台监控固定周期采样、greedy 预测去除多余求解，以及显式 SQLite WAL + FULL 请求连接复用。动态 Registry/Validator、质量闸门、500ms 快照有效期与逐操作提交保留；生产 Bandit 未启用。

新协议完整执行 480 条真实训练、360 条三策略留出，全部 VALID/MATCHED，模型独立重建一致。同期完整请求均值：S0 **78.054ms**、固定 S1 **71.147ms**、自适应 **73.379ms**。这不是旧查询/协议与新协议间的受控因果比较。

LOW/MEDIUM 目标稳定率均为 99.524%；但一个实际 MEDIUM/READY/ADAPTIVE 单元仅 **29 条**（门槛 30），分析器正确禁止正式收益区间与认证。自适应全部 120 次选择 S1，没有表现出随 CPU 档位改变策略，也未证明优于固定 S1。不能用补单条样本或放宽门槛取得通过。

详见 [开销定位与复审](PHASE7_OVERHEAD_FIX.md)、[独立分析证据](evidence/phase7_overhead_e2e_20261103.json)。下一步仍在阶段7：建立经审计动作在不同工作负载上的收益交叉，并检查 Adapter 延迟 reward 与完整请求目标的一致性；不进入阶段8。以下保留历史记录。

## 稳定多负载、充分样本与真实 E2E 验证（2026-11-02，最新）

**多负载 PASS；充分样本 PASS；实际收益 FAIL；阶段 7 NOT ACCEPTED / NOT READY。** 使用用户授权的有界整机 CPU 共租负载，不冒充 IoTDB 查询或进程独占负载。

发现并修复同线程 psutil.cpu_percent(None) 的 client/DB 共享基线问题，新增每实例独立 cpu_times 差值与 cpu-activity-v3 隔离。补充实验首次 Native 访问，保留 S1 从第二次访问才可选的安全规则。旧失败批次全部保留，不混入新模型。

最终 479 条合格训练（八单元各 55–65）+360 条实际留出（三策略 × 四场景各 30）。LOW 420 / MEDIUM 419 个执行前观测全部落入目标区间，模型参数独立重建一致。自适应选择 EMPTY→S0、READY→S1，但未证明 CPU 档位驱动的选择变化。

E2E 均值：S0 755.377ms、固定 S1 775.545ms、自适应 779.723ms。自适应−S0 为 +24.346ms，99% 区间 [+0.835,+48.723]；相对固定 S1 区间跨 0，三批次收益不稳定。**不能宣称优化收益或启用部署。** 全量 841 passed in 39.03s（含13项真实集成）；专项19 passed；真实日志篡改副本5 passed。

详见 [完整实验、逐项结论与下一步](PHASE7_CONTROLLED_E2E.md)。下一步限于阶段7内监控/学习开销剖析、目标与奖励一致性、有效动作交叉及独立复现；不扩大数据集来代替定位，不进入阶段8。以下保留历史记录。

## CPU实验三项缺陷修复复审（2026-11-02，最新）

**F1/F2/F3修复PASS；阶段7整体仍NOT ACCEPTED / NOT READY。** 两个实验CLI增加独立Linux进程组监护，60秒窗口预留1秒清理，阻塞worker不能卡住父进程；客户端终止不冒充服务端取消，超限记录aborted/UNKNOWN并停止，不自动重试。轮次完成通知在日志flush后发送。

筛选汇总严格绑定预声明计划与实际SQL摘要/连接数/轮次/结果数量/唯一ID，拒绝终态后事件、abort再complete与失败完成。共享ActivityObservation验证DB_HOST、UTC和CPU标签；过期/未来样本只允许UNKNOWN。历史日志不改写，缺少逐执行SQL摘要的旧记录明确未验证，不追溯补证。

最终全量 **829 passed in 36.44s**（含13项真实集成，无skipped）；专项 **44 passed in 4.81s**；compileall exit0。最终真实筛选7次成功Native、随机冒烟8条VALID/LOW，两个受监护CLI均exit0。历史失败批仍未完成、两批成功筛选仍完成。详见 [修复与逐项复审](PHASE7_CPU_FIX_REREVIEW.md)。

当前仍缺稳定合格多负载、v2充分样本、实际自适应端到端收益及部署认证。不进入阶段8。以下为修复前历史记录。

## 最新严格验收（2026-11-02）

**NOT ACCEPTED：3项新代码FAIL，真实启用仍BLOCKED。** F1：轮次60秒仅事后检查，worker阻塞时无有界退出；F2：筛选汇总忽略aborted且不绑定预声明计划；F3：ActivityObservation可接受错误source/CPU标签/无时区时间。4个隔离反例全部复现；未修改业务代码或原始实验日志。

全量重新运行 **796 passed in 29.90s**（13项真实集成），反例 **4 passed in 0.43s**表示缺陷复现而非修复通过。旧真实日志另行复核未发现source/标签/执行状态不一致。详见 [严格验收与最小修复清单](PHASE7_CPU_ACCEPTANCE.md)。以下“测量修复PASS”是前轮实施记录，应以本轮FAIL结论为准。

## CPU活跃协议与现有数据筛选（2026-11-02，最新）

**测量修复PASS，阶段7仍NOT ACCEPTED / NOT READY。** 已实现显式`cpu-activity-v2`：CPU标签不再由内存/磁盘容量占用决定，50%/90%分界与原安全阈值均未调整；旧协议默认与旧JSON/context key保留，新协议分区隔离。L1、Orchestrator与Bandit按协议重新校验标签，执行前CPU采样窗口0.2秒。

现有三规模COUNT/SUM完成36轮84次成功执行；小/中规模分组完成24轮56次成功执行。筛选CPU采样116 LOW/1 MEDIUM，不能证明稳定第二档位。首批大规模分组4连接出现4次IoTDB查询内存配额拒绝，原失败日志保留，未重试或学习。新协议S0/S1冒烟64条全部VALID且执行前全部LOW，含0/3背景连接；不是充分收益实验。

全量 **796 passed in 31.23s**（13项真实集成，无skipped），专项 **106 passed in 13.05s**，compileall exit0。尚缺稳定可比负载、v2充分样本、实际自适应端到端收益及部署认证。不扩大数据集、不修改服务配置、不进入阶段8。

详见 [本轮协议、失败与实际命令](PHASE7_CPU_ACTIVITY_PROTOCOL.md)。后续应验证DataNode/查询级资源观测，区分整机CPU、客户端拉取与查询内存限制，不能靠改标签或加数据宣称通过。以下保留历史记录。

## 完整数据修复复审（2026-11-02，最新）

**阶段7整体仍 NOT ACCEPTED / NOT READY。** 修复两个真实分析器缺陷：整组实验单元缺失可漏报；不合格/稀疏负载可被错误计入多负载覆盖。新增协议预声明背景单元、种子绑定、质量与训练/留出两臂数量门槛。旧日志不追溯补证，不合格随机试验明确阻止策略评估。

新协议真实364条全部VALID，191训练/173留出，四单元每arm均≥30，完整覆盖/随机日志/导入证书/冻结模型校验PASS；负载全部MEDIUM。此前本轮580条保留，其中1条CPU99.4%的LOW_CONFIDENCE不能用于学习或解除负载门槛；两批不混合。完整回归 **789 passed in 34.58s**（13项真实集成，无skipped），专项 **33 passed in 0.72s**，compileall exit0。

剩余：多合格负载覆盖、部署认证、实际可重复自适应端到端收益。用户确认只使用现有电脑与数据，允许自行安排资源，不再等待外部实例。已只读核实用户级cgroup控制器可用；尚未改变现有IoTDB资源或建立隔离范围监控，不把整机指标改称进程/配额指标。

详见 [本轮逐项复审与命令](PHASE7_COMPLETE_DATA_REREVIEW.md)、[新协议证据](evidence/heavy_planned_review_20261102.json)。以下保留旧证据及历史计数。

## 导入/路径修复完成（2026-11-02，最新）

**导入阻塞已解除，阶段7整体仍NOT ACCEPTED / NOT READY。** 根因是现有IoT-Benchmark按设备名仅保留一个CSV路径，三个文件互相覆盖；数据库路径映射正确。已通过独立分批目录使用原导入器补入64亿缺失点，未删除现有数据、未修改IoTDB源码或原CSV。

全量写前核验320万序列缺失范围均为0；写后1600设备/320万序列均为3000点，共96亿点。三规模原COUNT全部1931；9000行六类型共54000测点值抽样匹配。完整回归 **786 passed in 31.25s**（13项真实集成，无skipped），专项30 passed；compileall通过。

新epoch：`heavy-complete-20261102-v2`。导入报告SHA-256已绑定到heavy_scale manifest；真实8条冒烟已验证实验入口和分析器认可新证书，且不会追溯认证旧epoch。多负载、新epoch充分样本与可重复自适应收益仍需后续工作。

报告：[HEAVY_IMPORT_REPAIR.md](HEAVY_IMPORT_REPAIR.md)；证书：[heavy_import_repair_20261102.json](evidence/heavy_import_repair_20261102.json)。首次启动遗漏function.xml的失败、返回0却没有结果矩阵的异常，以及重新全量核验后的重试均保留在报告和原始日志中。

以下为导入修复前的历史记录。

## heavy_scale 阻塞调查（2026-11-02，导入修复前）

详见 [PHASE7_HEAVY_EXPLORATION.md](PHASE7_HEAVY_EXPLORATION.md)。已接入精确工作负载/背景SQL选择与多轮审计，新增经本机DataNode进程、监听端口、命名空间验证的独立DB主机采样。三规模pilot96条、分轮888条，均为真实观测；正式483训练/405留出，24轮最长30.722秒，DB负载全部MEDIUM。无背景单元数量足够，有背景单元部分arm仍不足30；不混合旧client LOW样本。

**新发现的数据门槛 FAIL：三个规模源CSV所查窗口的s_355实际1931点，数据库仅390点。** CSV抽查每设备确有3000个唯一时间戳；具体导入/映射原因UNKNOWN。新增前置源/库比对，真实失败路径已验证：运行exit1、零随机分配、零经验记录。旧采样保留为当前数据库状态的真实事实，不能认证完整heavy_scale导入。

当前本机显式实验的DB主机指标缺口已解决；完整数据导入、多实测负载、背景单元样本和可重复自适应端到端收益仍未满足。阶段7仍 **NOT ACCEPTED / NOT READY**，未进入阶段8。旧默认root.db800集成查询当前为空，首次全量772 passed / 4 failed；配置现有IOTDB_TEST_SQL为当前非空路径后，最终 **776 passed in 34.37s**（含13项真实集成，无skipped）；专项54 passed，compileall exit0。完整命令见本轮报告。未修改/删除测试断言，未写IoTDB。

以下为此前记录，保留历史证据，不覆盖。

## 当前阻塞解除进展（2026-11-02）

详见 [PHASE7_EXPLORATION.md](PHASE7_EXPLORATION.md)。用户确认数据不变并授权有界只读并发后，已实际采集770条随机试验：402训练、368独立留出，全部VALID；每个训练单元的两个arm均≥30，逐次p=0.5日志重放通过。新离线模型按执行前缓存状态分区，不替换旧Experience/生产模型。

多负载仍未解决：实测全部LOW，不能把3背景线程标成高负载。DB主机指标仍UNKNOWN，部分留出单元不足30，尚无可重复端到端自适应收益。整体仍 **NOT ACCEPTED / NOT READY**。用户表示可提供隔离实例；等待非敏感连接/资源/监控信息与独立凭据环境文件路径后继续。当前未进入阶段8、部署锁保持关闭。最终全量 **763 passed in 34.67s**（含13项真实集成）；新增协议与模型测试 **13 passed in 0.49s**；compileall exit 0。

原始数据保存在 `data/phase7_exploration_20261102/`（本地未跟踪），报告为 [phase7_exploration_20261102.json](evidence/phase7_exploration_20261102.json)。以下保留此前验收与旧样本量，不能与新实验混算。

## 最新独立验收（2026-11-02）

详见 [PHASE7_ACCEPTANCE.md](PHASE7_ACCEPTANCE.md)。本轮重新运行：全量 **750 passed in 33.98s**（含13项真实只读集成），专项 **53 passed in 4.09s**。未发现新的可复现FAIL；三项修复仍PASS。真实样本和探索收益门槛仍BLOCKED，整体 **NOT ACCEPTED / NOT READY**，不进入阶段8。本轮仅更新验收文档，未修改业务代码或测试。以下为此前修复记录。

## 最新修复复审（2026-11-02）

详见 [PHASE7_REREVIEW.md](PHASE7_REREVIEW.md)。三项缺陷均已关闭：离线准入拒绝 LIMITED/UNKNOWN 及矛盾质量凭证；留出评估拒绝重复/冲突 execution ID；预期拒绝有ID、原因码和计数，内部异常继续传播。保留报告原reason字段，新增reason_code。

最终全量 **750 passed in 34.29s**（含13项真实只读集成、无skipped）；针对性与独立审查 **53 passed in 3.89s**。历史24条真实留出记录重放，8条接纳、16条记录排除原因，有效记录和MSE与原证据一致。没有新增真实Benchmark、没有启用线上模型。

[初审报告](PHASE7_REVIEW.md)保留为历史记录。以下原实施记录中的历史测试数字不替代上述最新结果。真实启用条件仍不足，不进入阶段8。

## 样本与启用判断

| 检查 | 实际结果 |
|---|---|
| 当前配置的生产 SQLite | 文件不存在，不能计入真实训练量 |
| L1 最新真实审计证据 | 24 个 execution IDs，12 VALID / 12 LOW_CONFIDENCE，质量版本 2 |
| 已注册启用策略 | S0_NATIVE@0.1.0、S1_REPEAT_CACHE@1.0.0；S1 仍需 immutable cache epoch |
| 同源 SERVER 合格训练量 | S0=4、S1=4；小于配置 min_samples=30 |
| 其他 VALID 样本 | 4 个 CACHE_HIT，缺少同源 Native，不合并为 SERVER 样本 |
| 负载覆盖 | 只有 LOW，缺少不同真实负载下的偏好证据 |
| 探索记录 | 旧脚本固定种子随机平衡顺序，但 Experience 无逐次 propensity 凭证；条件 hit 不是随机对照 |
| 部署决定 | NOT READY；所有模型 online_ready=false，online_advice=None，无线上连接 |

## 本轮实现

- `bandit/context.py`：固定 17 维编码 v1、缩放与 missing mask，仅使用执行前信息，client/DB 分开。
- `bandit/model.py`：disjoint LinUCB，独立 arm A/b/n，Cholesky 求解，固定 reward，门槛、冷启动、版本化快照；真实/模拟域隔离，单执行更新、去重和截止时间拒绝。
- `bandit/store.py`：独立离线 SQLite，不改变生产 v8；模型体+head 原子发布、expected_head CAS、历史版本原子回滚、完整性检查。
- `bandit/evaluation.py`：样本门槛审计，严格时间/ID 隔离、冻结模型，仅实际 arm 的预测误差；无未执行 reward，无无依据 IPS。
- `bandit/settings.py` / `configs/bandit.yaml`：alpha=.1、lambda=1、min_samples=30、seed=20261102，部署锁固定 false。
- `decision/llm_advisor.py` / `configs/prompts/strategy_advisor.txt`：默认关闭、无实际 provider；只接受已验证 Registry 候选 strategy_id，严格 JSON、超时/不可用回退 None；不生成 SQL。
- `scripts/phase7_offline_evidence.py`：旧数据审计、固定种子模拟、接口开关对照、独立真实 Benchmark 冻结评估。
- 新增测试 `tests/unit/test_linucb.py` / `test_llm_advisor.py`，共 22 个；原测试未删除。

详细每维定义、公式、参数、原子保存、回滚、限制见 [PHASE7_DESIGN.md](PHASE7_DESIGN.md)，
同时更新 LEARNING_DESIGN.md、README。本阶段没有更改 Orchestrator/CLI 决策路径，
没有 Web UI、框架、微服务、向量数据库或 IoTDB 源码修改；未提交 Git。

## 实际测试与实验

使用 `/opt/anaconda3/envs/MANTO/bin/python`，Python 3.12.13 / apache-iotdb 2.0.6。

| 命令 | 实际结果 |
|---|---|
| `PYTHONPATH=src python -m pytest -q tests/unit/test_linucb.py tests/unit/test_llm_advisor.py` | 22 passed in 1.47s |
| `IOTDB_INTEGRATION=1 PYTHONPATH=src python -m pytest -q` | **741 passed in 30.11s**，13 个真实 IoTDB 集成用例，无 skipped |
| `PYTHONPATH=src:tests/unit python -m pytest -q docs/evidence/phase6_review_probes.py docs/evidence/phase5_review_probes.py docs/evidence/phase5_rereview_probes.py` | **8 passed in 2.34s** |
| `PYTHONPATH=src python -m compileall -q src tests scripts` | exit 0 |
| `PYTHONPATH=src python scripts/phase7_offline_evidence.py --output docs/evidence/phase7_offline_2026-11-02.json --model-store /tmp/manto-phase7-models.sqlite --benchmark` | exit 0；模拟偏好随上下文改变，真实留出评估完成，online_ready=false |

最后补充接口统计后，仅重新运行 `llm_comparison` 更新本地 LLM 对照部分，未重跑或替换真实
Benchmark 数据；所有场景的原始结果和耗时保存在同一 JSON，未合成真实模型结果。

固定种子模拟 240 次：LOW 选择 SIM_BASE，HIGH 选择 SIM_ALT。手算 A/b、UCB、门槛、
未执行 arm 不变、幂等/冲突、真实/合成域隔离、非有限值、原子保存失败、校验和、回滚均覆盖。

独立 Benchmark 新采集 24 条：12 VALID / 12 LOW_CONFIDENCE，16 次测量 server dispatch，
另 2 次预热和 doctor 探测；与训练 IDs 不重叠且时间全部更晚。冻结模型评价 8 条同分区
合格 SERVER 记录，事实 reward MSE=1.0000098217615624e-6（reward²）。未更新训练参数，
未计算未执行策略奖励，policy_value=null；不能把预测误差当成加速或因果收益。

原始证据：[phase7_offline_2026-11-02.json](evidence/phase7_offline_2026-11-02.json)。

## LLM 开关对照与未完成条件

本地模拟接口每场景 10 次：off 不调用提供方；合法响应 10 次符合 schema、与规则一致率 100%；
非法响应/超时/无提供方各 10 次返回规则回退。合法模拟响应比 off 的中位额外开销约 0.055245ms，
非法约 0.046083ms，5ms 超时约增加 5.204356ms。它们是本地协议测试，不是实际 LLM 质量/延迟。

- **真实 LLM 收益与时延对照 BLOCKED_NO_PROVIDER**，没有配置实际模型，不伪造提升。
- **真实 Bandit 部署 NOT READY**，样本、覆盖、探索日志和独立策略收益均不足。
- DB 主机指标 UNKNOWN；没有真实生产收益或线性模型泛化保证。
- 阶段 7 离线复审及最小修复已完成；完整启用条件仍未满足，不进入阶段 8。


## 阶段 7 本轮复审与修复

严格复审先复跑既有全量测试 741 passed，再独立复现三个失败反例。
最小修复限于 `bandit/evaluation.py`：留出重复 ID 直接拒绝，审计按证据摘要去重、
冲突重放拒绝；策略隔离/禁用等 Registry 拒绝按样本记录原因，其他合格事实继续评价。
增加 9 项正式回归及三反例归档，独立重算真实 A/b 和留出 MSE 与原报告一致。
不改变算法、Schema、生产 SQLite、Orchestrator、CLI 或线上部署锁。

| 本轮实际命令（python=/opt/anaconda3/envs/MANTO/bin/python） | 结果 |
|---|---|
| `PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=src python -m pytest -q -p no:cacheprovider tests/unit/test_linucb.py tests/unit/test_llm_advisor.py tests/unit/test_phase7_review_regressions.py docs/evidence/phase7_review_probes.py` | 34 passed in 2.37s |
| `IOTDB_INTEGRATION=1 PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=src python -m pytest -q -p no:cacheprovider` | **750 passed in 35.83s**，13 个真实集成用例，无 skipped |
| `PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=src:tests/unit python -m pytest -q -p no:cacheprovider docs/evidence/phase6_review_probes.py docs/evidence/phase5_review_probes.py docs/evidence/phase5_rereview_probes.py docs/evidence/phase7_review_probes.py` | 11 passed in 2.53s，部分与正式回归重叠，不累计独立覆盖 |
| `scripts/phase7_offline_evidence.py`（完整命令见审查报告） | exit 0；仅离线模拟/旧事实重放，未重跑真实 Benchmark |
| ast.parse 检查 src/tests/scripts | 106 文件，exit 0 |

详细逐项 PASS/FAIL/BLOCKED、代码位置、证据和下一步见 [PHASE7_REVIEW.md](PHASE7_REVIEW.md)。
原始真实 Benchmark 不改写；本轮不新增真实性能收益结论。样本/负载覆盖/探索凭证不足，
实际 LLM provider 和 DB 指标代理缺失仍为阻塞，阶段 7 完整验收 **NOT ACCEPTED**。
下一步限于阶段 7 的真实受控探索及证据完善，不进入阶段 8；未将整仓未跟踪文件提交 Git。


## 三规模重查询工作负载（2026-11-02）

完成用户指定的数据集工作负载生成任务：`query/heavy_scale/{small_scale,medium_scale,large_scale}` 每规模8个类型文件，每文件1000条不同查询，共24000条。生成器 `scripts/generate_heavy_scale_queries.py` 读取完整schema与全部CSV首尾记录；验证器 `scripts/validate_heavy_scale_queries.py` 独立检查文件与语句。未更改生产Agent、Registry、Validator或数据库。

- 生成命令：`PYTHONPATH=src /opt/anaconda3/envs/MANTO/bin/python scripts/generate_heavy_scale_queries.py`，exit 0。
- 验证命令：`PYTHONPATH=src /opt/anaconda3/envs/MANTO/bin/python scripts/validate_heavy_scale_queries.py --probe-explain`，完整性 PASS：24 文件、24000 条全局唯一查询；当前 IoTDB 2.0.6 tree 的每文件首条普通 EXPLAIN 共 24/24 SUCCESS，详见 validation.json。
- 回归命令：`PYTHONPATH=src /opt/anaconda3/envs/MANTO/bin/python -m pytest -q tests/unit/test_sql_guard.py tests/unit/test_sql_parser.py tests/unit/test_tree_adapter.py`，137 passed in 0.47s。
- EXPLAIN 仅覆盖每文件首条；未逐条执行24000条目标查询，未测得真实性能收益。CSV内部完整性、数据库完整导入情况和查询负载等级仍未认证。

证据见 `query/heavy_scale/manifest.json`、`query/heavy_scale/validation.json` 和工作负载 README。本项不解除阶段7既有阻塞，不据此进入下一阶段。下一步是有界执行并实测查询强度和失败路径。

最终工作树回归：**859 passed in 36.93s**（含13项真实IoTDB集成，无skipped）；`python -m compileall -q src scripts tests` exit 0。最终实验SQLite `integrity_check=ok`，10项运行源码摘要与实验启动时一致。摘要见 `docs/evidence/phase7_overhead_artifact_manifest_20261103.json`。

2026-11-03完整请求奖励最终证据回归：23 passed in114.45s；两份最终SQLite integrity_check=ok。摘要见`evidence/phase7_reward_artifact_manifest_20261103.json`；失败批未删除。
