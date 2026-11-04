# 三规模数据导入修复（2026-11-02）

本轮仅处理数据导入/路径映射阻塞；不修改IoTDB源码、不进入下一阶段。用户明确要求核实并修复导入，按维护操作补齐缺失CSV数据；与此前只读负载实验分开执行。

## 根因与证据

IoT-Benchmark `RealMetaDataSchema.java:61`使用`Map<String,String>`存储文件，`:135`以设备名作为key执行`put`；`:107`每设备只取一个文件。同设备三个CSV路径相互覆盖。实际使用的core-1.0.0.jar经javap检查包含同样的Map.put逻辑，JAR SHA-256记录于evidence/heavy_import_plan_20261102.json。

原导入日志分别只识别100/500/1000个文件，成功点数为2亿/10亿/20亿。真实只读抽查三个设备均仅1000行，时间1640976405000至1640981400000，与BigBatch_2一致。目录遍历顺序并不保证永远选择最后编号；本次结论以实测时间戳为准。

映射`root.small_scale.g_0`、`root.medium_scale.g_0`、`root.large_scale.g_0`与真实设备/测点匹配。缺失来自文件枚举，不通过修改SQL路径掩盖。原始数据和之前的失败实验记录保留。

## 客户端版本边界

现有导入器捆绑`iotdb-session-2.0.5-250801-20250801.020248-1.jar`，不是2.0.6；优化器独立读取端是Python apache-iotdb 2.0.6，服务端2.0.6 tree。这里没有把Java版本写成2.0.6，也没有自行替换未验证的JAR。本次复用用户已有的Tablet导入路径，兼容性证据仅限实际成功写入及Python2.0.6读回比较，不能推广到其他接口。小规模修复后已独立比较三批3000行、6种类型，全部相符（evidence/heavy_import_small_values_20261102.json）。

## 修复设计

新增`scripts/repair_heavy_import.py`，仅针对本次已导入第三批、前两批为空的数据集：

1. 对全部4800个CSV计算SHA-256，检查480万数据行时间戳、5000ms步长、每100行的重复表头、每文件1000行。
2. 为两个缺失批次分别建立软链接目录，每设备每轮仅一个CSV，避免Map覆盖。总共3200个CSV链接，无额外完整数据拷贝。
3. 为六轮生成独立私有配置：verificationWriteMode、2个数据客户端、100行写入批、禁止删库/建schema/双写、单写操作超时30000ms、单轮子进程最长900秒。缺失范围与批次在Pydantic计划中固定，不能扩大到已有第三批。
4. 写前全量核验1600设备/320万序列在[1640966405000,1640976405000)范围内全部COUNT=0。
5. 按小/中/大规模依次补写BigBatch_0、BigBatch_1。要求准确成功批数/点数、失败批数和点数均0、无未识别警告或ERROR。已知TSFileDescriptor默认客户端配置提示单独记录，不当作数据库写入失败。
6. 写后全量核验每序列3000点，并对三规模代表设备的6种类型、全部3批进行源值比对。FLOAT容差1e-6，DOUBLE容差1e-12；时间戳、布尔、整数和文本精确比较。

该工具是显式维护工具，不接入线上查询、Registry或学习执行链。密钥仅保存在gitignore下私有工作目录的0600配置文件，工作目录0700，不写报告。正常查询仍只读。

## 原始记录

工作目录`data/heavy_import_repair_20261102/`：plan.json、source_inventory.jsonl、verify_missing.jsonl、verify_complete.jsonl、import.jsonl及六轮benchmark/console日志。不要仅保留汇总后删除原始记录。

只读COUNT(*)在真实IoTDB2.0.6 tree上确认返回完整2000测点，逐个核对列名；证据evidence/heavy_count_star_probe_20261102.json。全量核验仍逐设备检查所有列，不是用一列推断全库。

## 实际中间失败与处理

- 首轮逐列COUNT只读核验的解析开销过高，主动终止自有进程（exit143），保留partial记录。等待30秒后，使用实测COUNT(*)重新完整核验并通过。
- 首次维护启动配置遗漏function.xml。IoT-Benchmark初始化失败却System.exit(0)，但没有写入结果矩阵；脚本正确判FAIL并停止。失败日志归档startup_failure_1，补齐配置资源并再次完整核验320万序列缺失范围仍为0后才重新执行。
- 没有把中断/零退出码视为成功，没有删除失败证据或自动重试部分写入。

## 可复现命令

解释器为/opt/anaconda3/envs/MANTO/bin/python，从项目根目录执行。prepare拒绝已有目录；import拒绝已有导入journal。当前修复一旦完成，不能再以原缺失前提重放，必须重新评估目标数据状态。

```bash
PYTHONPATH=src /opt/anaconda3/envs/MANTO/bin/python scripts/repair_heavy_import.py prepare
PYTHONPATH=src /opt/anaconda3/envs/MANTO/bin/python scripts/repair_heavy_import.py verify-missing
PYTHONPATH=src /opt/anaconda3/envs/MANTO/bin/python scripts/repair_heavy_import.py import
PYTHONPATH=src /opt/anaconda3/envs/MANTO/bin/python scripts/repair_heavy_import.py verify-complete
PYTHONPATH=src /opt/anaconda3/envs/MANTO/bin/python scripts/verify_heavy_values.py
PYTHONPATH=src /opt/anaconda3/envs/MANTO/bin/python scripts/phase7_check_heavy_import.py --live --output docs/evidence/heavy_import_live_after_20261102.json
```

旧的非live审计仍重放历史390点参考，它不是当前数据库状态；不可覆盖旧证据伪装历史已正确。

最终验收结果见下表。新的数据epoch为heavy-complete-20261102-v2，旧epoch下的缓存和模型不得与新数据混用；原始历史经验保留，不伪造更新。


## 后续实验的证据约束

`workload_audit.verify_import_certificate`验证本地维护报告的SHA-256、endpoint、data_epoch以及三规模全序列计数覆盖和抽样值检查状态。phase7_exploration在源/库参考一致后、第一次随机分配前记录dataset_import_certification；分析器重新验证该证据。缺失凭证仍BLOCKED，哈希、实例或epoch不匹配则拒绝。

历史journal没有本次证书，不会因数据库后来补齐就被追溯认证。证书是受信本地审计的来源核验，不是IoTDB签名；后续外部写入仍要求作废epoch并重新核验。


## 最终实际结果

六批补写均得到准确成功矩阵，新增64亿点。独立写后COUNT遍历全部1600个设备、320万序列，全部每序列3000点。三规模原工作负载窗口COUNT由390恢复1931；代表设备六类型共9000行/54000测点值与源CSV一致。数值核验是抽样，不声称逐值比较了全部96亿点。

| 规模 | 设备 | 序列 | 每序列点数 | 完整窗口实际点数 |
|---|---:|---:|---:|---:|
| small_scale | 100 | 200000 | 3000 | 600000000 |
| medium_scale | 500 | 1000000 | 3000 | 3000000000 |
| large_scale | 1000 | 2000000 | 3000 | 6000000000 |

导入修复证书：[heavy_import_repair_20261102.json](evidence/heavy_import_repair_20261102.json)，包含原始清单/写入日志/全量计数/抽样值/新鲜COUNT报告的SHA-256。源数据、数据库路径和24000条SQL均未改写；重新执行离线工作负载完整性检查仍PASS 24000。

## 测试与入口复审

```bash
PYTHONPATH=src /opt/anaconda3/envs/MANTO/bin/python -m pytest -q tests/unit/test_heavy_import_repair.py tests/unit/test_heavy_workload_audit.py tests/unit/test_exploration.py tests/unit/test_exploration_model.py
# 30 passed in 0.58s

IOTDB_INTEGRATION=1 IOTDB_TEST_SQL='SELECT s_355 FROM root.small_scale.g_0.d_1 WHERE time >= 1640968700000 AND time < 1640978355000 LIMIT 5' /opt/anaconda3/envs/MANTO/bin/python -m pytest -q
# 786 passed in 31.25s，包含13项真实只读集成，无skipped。

PYTHONPATH=src /opt/anaconda3/envs/MANTO/bin/python -m compileall -q src scripts tests
# exit 0

PYTHONPATH=src /opt/anaconda3/envs/MANTO/bin/python scripts/phase7_exploration.py --output data/phase7_heavy_import_smoke_20261102 --epoch heavy-complete-20261102-v2 --immutable-epoch-confirmed --sql-file query/heavy_scale/small_scale/q03_count_aggregation.sql --local-db-pid 3879777 --trials 2
# exit 0；8条真实只读冒烟测量，4轮均低于1秒；PID复跑前必须重新核验。

PYTHONPATH=src /opt/anaconda3/envs/MANTO/bin/python scripts/phase7_analyze_exploration.py data/phase7_heavy_import_smoke_20261102/journal.jsonl --output docs/evidence/heavy_import_smoke_20261102.json
# exit 0；随机分配重放、当前DB主机来源、新epoch导入证书均通过。
```

本轮新增10项回归，包括源时间戳/表头损坏、配置去重及禁删保护、缺失全量前置证据、六类型值差异、零退出码但无成功矩阵、禁止扩大到已有第三批、证书哈希/目标/epoch/计数覆盖，以及禁止在随机分配之后补造导入证书。既有测试未删除或弱化。

**本任务：PASS，导入/路径阻塞已解除。** 新冒烟报告不再含HEAVY_SOURCE_IMPORT_NOT_FULLY_VERIFIED或DB_HOST_METRICS_UNAVAILABLE。8条冒烟不是样本量/收益验收：多负载覆盖、新epoch充分训练与留出、可重复自适应收益仍BLOCKED；阶段7整体仍NOT ACCEPTED / NOT READY，不进入阶段8。

改动包括三个维护/核验脚本、导入证书汇总脚本、已有实时源库审计和实验入口/分析器、workload_audit证据校验、对应回归以及README/阶段状态。IoTDB源码和原benchmark安装目录均未修改。工作区既有业务目录为未跟踪状态，本轮未将整仓既有改动混入Git提交。
