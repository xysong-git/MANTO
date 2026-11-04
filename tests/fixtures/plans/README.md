# EXPLAIN Plan Fixtures（阶段 0）

本目录保存 IoTDB 2.0.6 树模型在真实查询上返回的文本执行计划，以及三类
“空计划”负例。它们是 Plan Validator 和“策略是否生效”测试的输入，
不是稳定的机器可读计划格式。

## 环境与来源

| 项目 | 值 |
|---|---|
| IoTDB server | 2.0.6，build `c6ecbde` |
| 客户端 | 2.0.6 复验；部分探测用 2.0.10 |
| 方言 | tree model |
| 计划格式 | 纯文本（ASCII art / `distribution plan` 列） |
| 生成日期 | 2026-11-01 |

`EXPLAIN` 返回单列 `distribution plan`，每行是一段文本。当前 server
**不支持** `EXPLAIN FORMAT JSON`、`EXPLAIN ANALYZE FORMAT JSON` 或
`EXPLAIN VERBOSE`，错误见 `docs/DB_CAPABILITY_AUDIT.md`。

## 文件

| 文件 | 查询/场景 | 用途 |
|---|---|---|
| `native_scan.txt` | 单序列时间范围 Native scan | 基线计划：`AlignedSeriesScan` |
| `aggregation_count.txt` | `COUNT(s_130)` | 聚合计划：`AlignedSeriesAggregationScan` |
| `last_query.txt` | `LAST s_130` | `LastQuery` → `LastQueryScan` |
| `native_desc_limit_1.txt` | `ORDER BY time DESC LIMIT 1` | 与 LAST 对比的 Native 索引顺序 |
| `full_outer_time_join_2devices.txt` | 两设备原始查询 | `FullOuterTimeJoin` |
| `device_view_2devices.txt` | 两设备 `ALIGN BY DEVICE` | `DeviceView` 形状变化 |
| `agg_max_time_last_value.txt` | `MAX_TIME + LAST_VALUE` | 聚合扫描与 `ScanOrder: DESC` |
| `limit_10.txt` | `LIMIT 10` | limit 下推到 scan |
| `desc_limit_10.txt` | `ORDER BY time DESC LIMIT 10` | `ScanOrder: DESC` |
| `explain_analyze_native.txt` | `EXPLAIN ANALYZE` 单序列查询 | 运行时算子统计样例；**已去敏** |
| `empty_plan_*.json` | 非法/未知路径或函数 | 0 计划行，不能当作合法计划 |
| `index.json` | 全部 fixture 的元数据 | 查询、来源 probe、行数、波动性警告 |

`index.json` 中每条记录包含 `file`、`query`、`source_probe`、`line_count`；
`explain_analyze_native` 额外标记 `volatile: true`。

## EXPLAIN 与 EXPLAIN ANALYZE 的区别

- `EXPLAIN <SELECT>` 返回计划文本，不执行查询。
- `EXPLAIN ANALYZE <SELECT>` 返回带运行时统计的文本，包括
  `Total Wall Time`、算子 CPU、输出行数等；它走真实执行路径，因此
  **不能当成无副作用的 EXPLAIN**。本目录只在只读测试查询上生成，
  并已把 fragment ID 和地址替换为占位符。
- `explain_analyze_native.txt` 中的成本、wall time、CPU、内存、文件数都是
  真实观测值，但高度波动；只能作为“ANALYZE 可用且确实执行”的证据，
  不能作为固定基准值。

## 计划文本的波动性

以下内容在相同查询重跑时也可能变化，禁止直接 hash 原始文本：

- 树形绘制字符、空格数量、行顺序中的排版；
- `PlanNodeId N`；
- 算子名后缀数字（如 `AlignedSeriesScan-1` 的 `-1`）；
- `Partition: N`（受数据分区/时间分区影响）；
- `EXPLAIN ANALYZE` 的 fragment ID、IP、端口、`DataRegion`；
- 所有 cost、CPU、wall time、内存和 `Seq File` 计数；
- server 版本升级后算子命名或字段顺序。

建议的稳定计划签名（Phase 1 实现时采用并测试）：

1. 解码为文本，去除 ASCII 边框字符和多余空白；
2. 按“算子节点”提取算子类型（去掉 `-<数字>` 后缀）和父子关系；
3. 只保留语义相关属性：`Series`、`QueryAllSensors`、`ScanOrder`、
   `Limit`、`Aggregator-*`、`Order`、`DeviceCount`、`Aligned`；
4. 忽略 `PlanNodeId`、`Partition`、fragment/地址、全部运行时成本；
5. 对规范化后的结构做确定性序列化再 hash，而不是对原始文本 hash；
6. 计划不可用时记录 `UNAVAILABLE`，空计划记录 `EMPTY`，二者都不能
   被当成“计划符合预期”。

阶段 3 已在 `src/tsdb_optimizer/monitoring/plans.py` 实现上述树模型
`EXPLAIN` / `EXPLAIN ANALYZE` 解析和稳定 hash，测试见
`tests/unit/test_plan_parser.py`。解析器只覆盖本目录 fixture 验证的文本
格式；格式变化会返回 `UNAVAILABLE`/`ERROR`，不会猜测。表模型没有真实
fixture，因此非空表计划 fail-closed 为 `UNAVAILABLE`。详见
[`docs/METRICS_DICTIONARY.md`](../../../docs/METRICS_DICTIONARY.md)。

## 空计划负例

三个 `empty_plan_*.json` 都记录同一危险行为：server 返回成功，但 EXPLAIN
得到 0 行、执行只返回 `Time` 列和 0 行。

| fixture | 原因 |
|---|---|
| `empty_plan_series_path.json` | `FROM` 使用了完整 series path 而非 device path |
| `empty_plan_wrong_device_path.json` | device path 不存在（`root.test.d_0`） |
| `empty_plan_mavg.json` | 未知函数 `MAVG` 被接受但未生成计划 |

因此：**空计划不是合法计划，空结果不是语义等价证明。** Validator 必须
结合 SQL 子集、schema 路径和函数白名单 fail closed。

## 复现

`index.json` 中每条记录都带有原始 SQL。使用树模型客户端执行：

```sql
EXPLAIN <query>;
-- 或仅在隔离的只读测试数据上：
EXPLAIN ANALYZE <query>;
```

不要在生产数据上运行 `EXPLAIN ANALYZE`；不要在未确认路径存在时把空计划
当作 baseline。Phase 1 的 adapter 应把计划状态记录为
`AVAILABLE` / `EMPTY` / `UNAVAILABLE`，并保留原始文本供诊断。
