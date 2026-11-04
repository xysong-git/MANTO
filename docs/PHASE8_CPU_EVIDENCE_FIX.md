# 2026-11-04 09:50 CPU停止与取证缺口修复

## 根因边界

本次原始目录 `benchmarks/results/phase8_small_count_20261104_094956_840227951`，journal事件227：北京时间09:50:09.977182，TRAIN block2 / APPLICATION_EMPTY / TRAIN_S1，第2条测量请求（index1）派发前被拒。

client CPU99.711%、DB_HOST99.645%，分别超过80%；快照年龄72.102ms、107.259ms，memory67.2%、disk22.1%，有效且新鲜。91条已保存请求，64次训练更新；无完整留出实验。拒绝本身符合资源协议，并非目录冲突或Ctrl+C。

模型数值实现是纯Python Cholesky，此时也未进入E4/A1留出推断，没有发现BLAS线程池证据。读取DataNode运行目录、进程GC选项及09:50前后IoTDB日志，只发现登录/查询记录；当前JVM无GC日志文件。不能据此确认或排除GC。历史sysstat在09:45–09:55没有返回区间数据。

**峰值来源仍UNKNOWN。** 两路整机计数相近支持同一主机活动，但不证明特定进程占满CPU。缺少原始累计计数器、差分窗口与进程CPU，历史记录无法进一步归因；这是本轮修复的可观测性缺口，不宣称修复了高CPU根因。

## 最小修改

- 新增 `benchmark/cpu_evidence.py`：代理现有psutil接口，原样传递现有采样器实际使用的cpu_times；保存前后原始计数、UTC/单调时钟以及benchmark/DataNode进程的user/system累计秒数、PID与创建时间。进程读取失败明确记录错误和null。
- 原始证据使用独立Pydantic模型；按最终SystemSnapshot.snapshot_id关联，保留最近32份，不用“最新证据”代替被拒快照，不更改原SystemSnapshot或历史经验协议。
- `benchmark/runner.py`仅为LiveEnvironment包装采样器，并在resource_rejected增加cpu_counter_evidence。缺失/被淘汰证据为null，不能填造。
- 不改变80%停止线、快照有效期、CPU计算公式、SQL、学习算法、自动重试规则或IoTDB代码/配置。

进程采样与整机读取不是原子操作，必须使用各自时间窗；PID创建时间变化时不能相减。user/system进程CPU不能包含/解释所有内核活动。只观察MANTO与DataNode，其他进程来源仍UNKNOWN，不将“剩余差额”标成某个外部程序。未保存完整命令行或凭证。

额外进程读操作会增加监控开销；32条有界内存，不在每次快照写磁盘。失败事件才序列化原始证据。所有组使用相同取证方式，实际计时不扣除成本；新源码摘要会区别于旧实验。未运行真实性能测试，不能声称开销可忽略。

## 验证

原事件提取到 `tests/fixtures/monitoring/phase8_resource_rejection_20261104_095009.json`，未修改其值。测试覆盖：原始拒绝保持、原始CPU计数不变、快照ID关联、基线/读取失败、权限拒绝为UNKNOWN、有界缓存，以及拒绝事件携带对应证据且不派发下一次SQL。

- `conda run -n MANTO python -m pytest -q tests/unit/test_phase8_cpu_evidence.py tests/unit/test_phase8_admission.py tests/unit/test_cpu_counter_isolation.py tests/unit/test_local_db_sampler.py`：28 passed in0.61s（当时新增模块5项测试）。
- 补充runner证据关联测试后 `conda run -n MANTO python -m pytest -q tests/unit/test_phase8_cpu_evidence.py`：6 passed in0.59s。
- 完整非IoTDB回归 `conda run -n MANTO python -m pytest -q tests/unit tests/test_phase0_fixtures.py tests/test_phase2_sql_features.py`：1015 passed in124.01s（采集测试集时尚未加入最后1项runner关联测试，该项随后单独通过如上）。
- `conda run -n MANTO python -m compileall -q src/tsdb_optimizer/benchmark/cpu_evidence.py src/tsdb_optimizer/benchmark/runner.py`：exit0。
- 离线调用analyze：91条记录，report与原保存报告逐字段一致、所有结果文件摘要未变，execution_complete=false、analysis_qualified=false。

原journal SHA-256：`6a138d5ff570257ea009a688a193bfb0bfb50792a4ca0acf62addf2ca0f0c2d2`。

## 下一步

用户下一次按原命令在新输出目录执行时自动携带取证；如果再发生拒绝，读取resource_rejected.cpu_counter_evidence，先验证host差分计算，再检查相同PID/创建时间下的进程CPU增量。无需再次安排与故障不同时段的短采样来猜历史来源。原实验保留，不合并失败前缀或选择性忽略高负载样本。真实运行仍由用户执行，本轮没有数据库查询。
