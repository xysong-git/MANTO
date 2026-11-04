# 阶段7：负载稳定性修复与独立验证

2026-11-03，预声明。仅阶段7，生产Bandit禁用、LLM关闭，Conda MANTO。

## 已知事实与修复假设

上轮v2整机CPU在MEDIUM准入达到83.183%，必须停止；两批BATCH_LOAD_CHANGED拒绝。具体峰值来源UNKNOWN。旧发生器48个50ms周期忙等进程，在64逻辑核主机上理论最高占用75%，周期未错峰。新显式staggered-v1限制忙等进程数为min(48,affinity_count,floor(cpu_count*0.60))，本机38个、理论占用上限59.375%（不含其他进程），并以共同20ms时钟均匀错开忙等相位。旧模式保留，未改IoTDB配置/CPU亲和性。

不能保证环境整体CPU不上升，仍按实际CPU>80%、内存>90%、磁盘>90%停止；worker30秒到期、监护60秒、正式准入250ms不变。先用真实VerifiedLocalDbHostSampler/后台100ms采样/200ms返回预算做无SQL预检，LOW/MEDIUM各3轮，预热3秒+观测8秒；每轮至少20个唯一DB快照、80%落入既定档位。所有拒绝快照保留；不平滑/删除峰值，不自动重试。

预检通过后，批次实验v3使用此显式profile；其余与delayed-batch-v2相同：训练SQL第10条、未做策略留出的第11条，240训练批次/1200请求，360留出批次/1800请求，计划/奖励/样本门槛/置信门槛不变，独立零初始化。输出新目录data/phase7_delayed_batch_v3_20261103。只做一次完整新实验，失败保留，不拼接旧数据。

测试计划：相位分布边界、进程预算/不可能目标拒绝、worker自行到期/收尾、原默认模式兼容、现有监控与批次测试；实测完成后独立重放模型、检查数据库完整性、全量含真实集成回归、更新README/状态。稳定性通过不等于策略收益通过；必须按实际留出结果判定。

## 共享实例独立性阻塞（等待期间记录）

首次错峰预检`data/phase7_staggered_pilot_20261103`完成5轮，第6轮MEDIUM在DB_HOST=81.260%、client_host=82.353%时停止，零SQL。LOW也出现53.883%/55.956%峰值，因此不能把干扰归因全部归给压测器。未获得完整预检PASS，不启动训练。

只读调查确认：MATAOF的`scripts/bench_configured_e2e.py`，PID1202893、create_time=1793677564.05，从另一项目目录运行，持有同一DataNode/6667的20个ESTABLISHED连接。服务端地址是`::ffff:127.0.0.1`，客户端是`127.0.0.1`。无加压采样捕获DataNode进程CPU增量872%/900%（按多核进程口径，不是整机百分比），15秒jstat期间YGC348/FGC0/CGC70计数未变；不能将这一窗口的峰值直接归因为GC，也不能证明所有历史峰值均来自另一项目。

用户明确选择等待MATAOF完成，不结束该进程。共享运行使当前性能验收不满足独立实例窗口，连接数不冒充active_queries：实际活跃查询数仍UNKNOWN。

新增`experiments/exclusive_instance.py`及测试：只读Linux TCP表，验证DataNode监听者存在，对IPv4映射IPv6规范化；任何外部PID或未知来源连接（即使空闲）均拒绝。批次v3在启动、每轮开始及每批前后检查并落盘，完整计划要求1325条检查。预检也先检查占用。拒绝发生在加压/查询前，不结束任何外部进程。检查是时间点观测，不能排除两次检查间的短暂连接，仍要求操作者提供独占实验窗口。

原始错误映射诊断`phase7_shared_instance_20261103.json`保留（20 unknown，仍拒绝）；修正后证据`phase7_shared_instance_verified_20261103.json`明确20连接属于PID1202893。不得把旧失败日志补成通过。

v3修订仅增加独立性守卫和错峰压测模式，模型、SQL、随机顺序、样本数和收益门槛不变。等待连接释放后，先在新目录重复完整6轮无SQL预检，再执行此前预声明的单次完整v3；不将被干扰预检拼接成合格结果。

MATAOF原PID1202893在04:57:23 UTC报告completed；04:57:52占用检查0连接。随后PID1237877启动p1-repeat-wide-i9.json，04:58:05占用检查命中，独占预检目录`data/phase7_staggered_exclusive_pilot_20261103`只完成LOW一轮即拒绝，未进入MEDIUM加压，也没有SQL。用户等待决定继续有效。新增只读等待脚本每15秒检查，连续60秒无外部/未知连接才返回READY；2小时未获得窗口显式超时，不加压、不杀进程、不自动重试查询。

当前已运行：`conda run --no-capture-output -n MANTO python -m pytest -q -m 'not integration'`，912 passed / 13 deselected in41.48s；`PYTHONPATH=src:scripts conda run --no-capture-output -n MANTO python -m pytest -q docs/evidence/phase7_delayed_batch_review_probes.py`，9 passed in14.61s；compileall exit0。真实占用拒绝测试`data/phase7_exclusive_preflight_20261103`为预期exit1，仅三条事件，无SQL/无加压。等待结束前不宣称修复已经通过完整真实验证。

批次守卫同时实测执行单连接预算，观察到自有连接数>1也停止；重放器校验观察者PID、时间顺序、startup/round/batch边界检查次数与连接预算。独占守卫属于实验基础设施，在批次服务计时之外执行，其成本不混称生产查询服务耗时。

## v4固定样本预算修订（训练开始之前）

MATAOF及后续任务退出后，等待记录`data/phase7_exclusive_wait_20261103.jsonl`满足60.3秒定期检查零外部连接。新的`data/phase7_staggered_quiet_pilot_20261103`六轮完整通过：LOW三轮目标比例均100%；MEDIUM为100%、98.765%、100%，每轮81个唯一快照，范围分别[52.739,66.354]、[48.287,66.144]、[51.485,65.023]%。全部低于80%停止线，仍有一次真实跨档。

尚未进行新训练、未执行新留出策略。针对预检显示的非零跨档概率，恰好30批且要求30批有效没有任何质量拒绝余量；现预先固定`delayed-batch-v4`为45块，而非临时补样：360训练批次/1800请求，540留出批次/2700请求；每目标单元预分配45批，有效最低门槛仍30。全部预定块必须执行，无提前停止获取收益结论，无事后剔除成本。批内跨档训练仍拒绝，留出成功批次即使跨档也保留在按预分配目标配对的成本比较中；实际正确档位/质量一致的有效单元计数须>=30，phase/load目标稳定率仍>=80%。

协议与分析器按45块计算所有成本、分层配对bootstrap4000次和三个连续15块均值，99%门槛不变。新目录`data/phase7_delayed_batch_v4_20261103`。SQL第10/11条、epoch、seed、奖励、模型参数、资源阈值均不变；新模型仍从零开始。v3除占用启动拒绝外未开展训练或收益比较，不存在用留出收益挑选样本量。完整占用检查数变为1985。SequenceRequest的block上限扩至45，仅扩展实验轮次域；旧30块分析继续严格核对自身完整计划。

## v4中止、进程继承风险与v5（待用户执行）

两次v4启动均完成4轮内置pilot及4个LOW训练批次（20请求），随后在首次MEDIUM启动后被占用守卫拒绝。目录分别为`data/phase7_delayed_batch_v4_20261103`和`data/phase7_staged_exclusive_20261103/study`；记录的PID分别1248508、1250977。进程随后退出，未取得当时cmdline/cwd，身份UNKNOWN；不得继续认定它们属于MATAOF。前一轮的87个实现/协议文件在后续修改前hash完全一致。

代码审查发现确定的隔离风险：此前`staggered-v1`仍使用fork，而训练前Adapter已建立连接；fork会保留父进程打开的文件描述符。实际安装psutil的`_pslinux.py`中`get_all_inodes`逐PID执行inodes.update，同一socket被多个进程持有时归属可能覆盖。该机制可导致守卫把继承连接的自有CPU worker当作外部PID；两次均在首次MEDIUM fork后中止与此一致，但退出PID的历史身份无法补证。

v5使用新的`staggered-spawn-v2`，明确`cpu_start_method=spawn`，避免worker继承Adapter/SQLite/审计文件描述符；保留旧profile供历史解释，不增加“允许所有子进程”的绕过。实际外部/未知连接仍拒绝。增加真实打开文件的描述符隔离回归（文件代替父DB句柄，spawn不得继承），以及新profile生命周期测试；**这些最新修复尚未运行测试**。需要用户运行入口中的专项测试和后续完整验证，不能沿用修复前PASS。

v5保持45块、360训练批次/1800请求、540留出批次/2700请求；原始SQL/奖励/参数/seed/80%停止线/250ms准入/每单元30有效样本不变。无测试收益可供调参。两次v4均无冻结、无留出，收益UNKNOWN。离线失败审计均重现4次更新，20条VALID，两份SQLite integrity_check均ok；见`evidence/phase7_delayed_batch_v4_interrupted_20261103.json`与`evidence/phase7_staged_v4_interrupted_20261103.json`。

用户明确要求MANTO后续采用“用户执行命令，助手完成后分析日志”，已写入根目录AGENTS.md。最后一个自动流程已exit1结束，没有继续后台查询/压测。此后只准备代码/命令及读取已完成日志，不自行启动新测试或持续轮询。

## 用户执行入口

运行前确保输出目录尚不存在，DataNode PID仍为3879777；若IoTDB重启须换成新的实际PID，不猜测。所有Python命令由脚本显式使用Conda MANTO。

```bash
cd /opt/program-code/MANTO
bash scripts/phase7_run_staged_validation.sh data/phase7_manual_v5_20261103 3879777
```

脚本顺序：专项回归（含spawn描述符隔离）→定期检查连续300秒无外部/未知连接→六轮无SQL预检→完整零初始化训练和冻结留出→独立重放分析→全量含真实集成回归。任何一步失败立即停止，不重试、不覆盖旧目录；正常exit0仅表示命令完成，收益是否通过仍看report.json。

输出：`console.log`、`occupancy-wait.jsonl`、`pilot/journal.jsonl`、`study/journal.jsonl`及SQLite/implementation.json、成功完成分析后才有`report.json`。失败时保留完整已有记录。用户运行完告知目录即可，由助手读取日志做离线分析。

本轮修改涉及cpu_load.py、exclusive_instance.py、sequences.py、batches.py、实验/分析/预检/等待/一键脚本、对应单元测试与AGENTS/README/状态/本文。最新spawn路径及整个v5尚待验证，阶段7NOT ACCEPTED、生产部署关闭，不进入阶段8。

## 用户完成v5后的状态更新

`data/phase7_manual_v5_20261103`已完整完成：专项45 passed、全量929 passed；360训练/540留出批次、4500条VALID/MATCHED；359次更新、1次跨档拒绝。独占、稳定性、覆盖与重放均通过。自适应优于Native的本会话门槛通过，但相对固定S1的额外收益门槛未通过，阶段7仍NOT ACCEPTED。以上“尚待执行”描述保留为历史；最新结论见[完整结果](PHASE7_V5_RESULTS.md)。助手未重新运行测试或压测。
