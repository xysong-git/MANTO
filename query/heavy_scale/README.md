# 三规模 IoTDB 重查询工作负载

每个规模8个.sql文件，每文件1000条不同的同类型SELECT，共24个SQL文件、24000条查询。
一条SQL占一行，以分号结束，文件内不混入注释或其他语句。源目录实际名为small_scale，未使用消息中的拼写samll_scale。

## 文件类型

| 文件 | 类型 | 每设备测点数 |
|---|---|---:|
| q01_range_scan.sql | 宽时间范围原始值扫描 | 16 |
| q02_value_filter_scan.sql | 宽时间范围 + 两个数值过滤条件 | 16 |
| q03_count_aggregation.sql | 多设备多测点COUNT聚合 | 16 |
| q04_sum_aggregation.sql | 多设备多数值测点SUM聚合 | 16 |
| q05_group_by_time.sql | 时间分组COUNT聚合 | 8 |
| q06_group_by_time_value_filter.sql | 数值过滤 + 时间分组COUNT聚合 | 8 |
| q07_range_descending.sql | 原始值扫描按time倒序 | 16 |
| q08_group_by_time_descending.sql | 时间分组COUNT聚合按time倒序 | 8 |

数值过滤条件作用于所选首/尾设备的指定测点，是IoTDB时间对齐查询的全局WHERE条件；不宣称每个设备分别独立过滤。阈值为-10/-5/0/5/10，实际选择率UNKNOWN，需要执行时测量。

## 数据与规模

| 子目录 | 数据库前缀 | 总设备数 | 每条查询设备数 | 总时间序列数 |
|---|---|---:|---:|---:|
| small_scale | root.small_scale.g_0 | 100 | 10 | 200000 |
| medium_scale | root.medium_scale.g_0 | 500 | 50 | 1000000 |
| large_scale | root.large_scale.g_0 | 1000 | 100 | 2000000 |

- 读取真实info.txt、完整schema.txt以及每个设备全部CSV文件的表头/首尾时间；不是按配置猜测测点路径。
- 三组每设备2000测点，数值测点为s_333至s_1665（INT32/INT64/FLOAT/DOUBLE）；未对BOOLEAN/TEXT使用SUM或数值比较。
- CSV观测首尾时间1640966405000–1640981400000，步长5000ms，元数据预期每序列3000点。没有全量扫描CSV内部所有值，内部完整性仍UNKNOWN；未声称验证全部数据库导入点数。
- 每条查询时间窗口1800–3000个采样间隔（约2.5–4.167小时），采用半开区间 time >= lower AND time < upper；upper最大为max_time+5000。
- GROUP BY间隔为20000ms/30000ms/60000ms。没有LIMIT，可能产生较大结果集，执行时应完整消费并计时。
- 同类型、同一行跨三组使用相同测点与时间参数，设备数量随规模增加。这是按比例扩大查询工作量的对照，不是固定查询范围的纯数据库规模对照。
- “重”指设计上的宽时间窗口、多设备、多测点；实际扫描量、响应时间和资源消耗UNKNOWN，未伪造Benchmark结果。

## 验证与重建

manifest.json记录种子、实际数据边界、schema/file校验和、路径和文件数量。
validation.json记录独立完整性检查，以及每文件第一条查询的真实EXPLAIN结果（若已运行）。EXPLAIN不等于EXPLAIN ANALYZE；本次不执行整批目标SELECT。

从项目根目录运行：

```bash
PYTHONPATH=src /opt/anaconda3/envs/MANTO/bin/python scripts/generate_heavy_scale_queries.py --output query/heavy_scale_regenerated
PYTHONPATH=src /opt/anaconda3/envs/MANTO/bin/python scripts/validate_heavy_scale_queries.py --root query/heavy_scale --probe-explain
```

生成器拒绝覆盖已有目录。重建种子默认20261102。数据库路径来自本次已确认的root.<scale>.g_0；若以后导入到其他数据库，需要改生成映射并重新验证，不能只改目录名。

这些是显式工作负载文件，不改变Parser支持范围、策略Registry或生产执行路径。全部语句经过项目只读守卫，不代表Agent 1一定能自动优化所有类型；未知特征仍须Native或安全拒绝。


## 2026-11-02 实际验证

- 生成命令：`PYTHONPATH=src /opt/anaconda3/envs/MANTO/bin/python scripts/generate_heavy_scale_queries.py`，exit 0。
- 验证命令：`PYTHONPATH=src /opt/anaconda3/envs/MANTO/bin/python scripts/validate_heavy_scale_queries.py --probe-explain`，完整性 PASS：24 文件、24000 条全局唯一查询；当前 IoTDB 2.0.6 tree 的每文件首条普通 EXPLAIN 共 24/24 SUCCESS，详见 validation.json。
- 回归命令：`PYTHONPATH=src /opt/anaconda3/envs/MANTO/bin/python -m pytest -q tests/unit/test_sql_guard.py tests/unit/test_sql_parser.py tests/unit/test_tree_adapter.py`，137 passed in 0.47s。
- EXPLAIN 仅覆盖每文件首条；未逐条执行24000条目标查询，未测得真实性能收益。CSV内部完整性、数据库完整导入情况和查询负载等级仍未认证。


## 后续实际执行发现（2026-11-02）

三规模COUNT首查询的首列与源CSV不一致：窗口内源文件1931个非空点，数据库实际COUNT仅390。源CSV抽查各设备3000个唯一时间戳；完整导入仍未通过验收，原因UNKNOWN。详见同目录import_audit.json。此前24条EXPLAIN成功只证明代表查询可规划，不证明导入完整或实际扫描规模。SQL文件未改写，执行前应先完成源/库核验。


## 导入已修复（2026-11-02）

前述390点不一致已修复，历史报告保存在import_audit_before_repair.json。补齐两批后，全量COUNT确认三个规模的320万序列均3000点；原窗口COUNT均1931，六类型54000个抽样值一致。当前报告为import_audit.json，完整计数认证为import_verification.json。SQL文件不变。

新数据epoch为`heavy-complete-20261102-v2`。manifest.json绑定仓库docs/evidence下的认证报告与SHA-256；包内import_verification.json为同一报告的副本。后续任何写入需作废此epoch，旧heavy_scale缓存/模型不可复用。完整实验仍未通过阶段7收益和多负载门槛。
