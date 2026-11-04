# IoTDB 数据库能力审计（阶段 0，含阶段 1 补充）

- **审计对象：** Apache IoTDB 2.0.6（build `c6ecbde`），树模型，单机
  ConfigNode + DataNode
- **审计方式：** 通过 Python Native API 执行只读 SQL 和 EXPLAIN；
  未修改源码、未执行 DDL/DML、未修改 server 配置
- **证据：** `tests/fixtures/sql/`、`tests/fixtures/plans/`；
  原始 probe 输出位于仓库外临时目录，关键结论已在本文件记录
- **结论：** 树模型 SELECT 子集、文本 EXPLAIN、EXPLAIN ANALYZE 可用；
  但**没有找到经过完整认证的非 Native 优化策略**。详见
  [`STRATEGY_CATALOG.md`](STRATEGY_CATALOG.md)。

## 1. 审计边界

本阶段只回答“数据库实际支持什么”，不设计四个 Agent，也不实现优化器。
本阶段没有收到单独粘贴的用户 SQL；审计使用本地 MAPO/MATAOF benchmark 查询
和针对能力边界构造的探测语句作为真实样例。没有用户生产 workload，因此不能
把本审计当作生产 SQL 覆盖率证明。

审计使用树模型 `iotdb.Session`；表模型只做了 `TableSession` 的最小连接和
`SHOW TABLES` 探测。两种方言的 SQL、结果列和 EXPLAIN 规则不能混用。

## 2. 版本、部署与方言

```text
SHOW VERSION
Version  BuildInfo
2.0.6    c6ecbde
```

`SHOW CLUSTER`：

| NodeID | NodeType | Status | InternalAddress | InternalPort | Version |
|---:|---|---|---|---:|---|
| 0 | ConfigNode | Running | 127.0.0.1 | 10710 | 2.0.6 |
| 1 | DataNode | Running | 127.0.0.1 | 10730 | 2.0.6 |

- RPC 端口：`0.0.0.0:6667`；
- MPP：10740；schema consensus：10750；data consensus：10760；
- 单机、单副本（replication factor = 1）；
- 用户数据为树模型；表模型只有 `information_schema` 系统表。

## 3. 元数据命令

以下命令均在树模型方言下实际执行成功：

| 命令 | 结果列 | 观测 |
|---|---|---|
| `SHOW VERSION` | `Version`, `BuildInfo` | 2.0.6 / c6ecbde |
| `SHOW DATABASES` | `Database`, `SchemaReplicationFactor`, `DataReplicationFactor`, `TimePartitionOrigin`, `TimePartitionInterval` | 7 个 database（含 `root.__system`） |
| `SHOW DATABASES DETAILS` | 上述 5 列 + `SchemaRegionGroupNum`, `MinSchemaRegionGroupNum`, `MaxSchemaRegionGroupNum`, `DataRegionGroupNum`, `MinDataRegionGroupNum`, `MaxDataRegionGroupNum` | 单副本；时间分区 604800000 ms |
| `SHOW CLUSTER` | `NodeID`, `NodeType`, `Status`, `InternalAddress`, `InternalPort`, `Version`, `BuildInfo` | ConfigNode + DataNode Running |
| `SHOW CLUSTER DETAILS` | 13 列，含 `ConfigConsensusPort`, `RpcAddress`, `RpcPort`, `MppPort`, `SchemaConsensusPort`, `DataConsensusPort` | 端口见上 |
| `SHOW DEVICES` | `Device`, `IsAligned`, `Template`, `TTL(ms)` | 全量 1,001 个；1,000 个用户 device 均 aligned，另 1 个系统 device |
| `SHOW TIMESERIES <path>` | `Timeseries`, `Alias`, `Database`, `DataType`, `Encoding`, `Compression`, `Tags`, `Attributes`, `Deadband`, `DeadbandParameters`, `ViewType` | 支持通配路径；可看到 FLOAT/DOUBLE/INT32/INT64/BOOLEAN/TEXT |
| `SHOW CHILD PATHS <path>` | `ChildPaths`, `NodeTypes` | 可返回 `DEVICE`/`TIMESERIES` 节点 |
| `SHOW CHILD NODES <path>` | `ChildNodes` | 只返回子节点名 |
| `COUNT TIMESERIES <path>` | `count(timeseries)` | 例如 `root.test.**` = 100000 |
| `COUNT DEVICES <path>` | `count(devices)` | 例如 `root.test.**` = 500 |

`SHOW TABLES` 在树模型方言被拒绝：

```text
700: Error occurred while parsing SQL to physical plan:
line 1:5 no viable alternative at input 'SHOW TABLES'
```

表模型 `TableSession` 下 `SHOW TABLES` 成功，返回 17 张 `information_schema`
系统表（`columns`、`config_nodes`、`configurations` 等），没有用户表。

## 4. 已验证的 SELECT 子集

以下语法在 fixture 或探测中被 server 接受并实际执行。这个列表是**白名单
的起点，不是完整 SQL 语法证明**；Parser 仍需按 fixture 实现 fail-closed。

### 4.1 查询结构

- `SELECT <series list> FROM <device path list>`
- `SELECT * FROM <device path> LIMIT ...`
- 时间过滤：`time >= ...`、`time < ...`、`time = ...`、`time > ...`
- 值过滤：`<series> > ...` 等（实测用于多设备查询）
- 聚合函数：`COUNT`、`AVG`、`MIN_TIME`、`MAX_TIME`、`MIN_VALUE`、
  `MAX_VALUE`、`FIRST_VALUE`、`LAST_VALUE`
- 最新值语法：`LAST <series>`
- 分组：`GROUP BY ([start, end), interval)`，例如
  `GROUP BY ([1640966400000, 1640966650000), 20000ms)`
- 排序：`ORDER BY time DESC`
- 限制：`LIMIT n`
- 多设备对齐：`ALIGN BY DEVICE`
- 多设备 `FROM`：逗号分隔的 device path
- 通配路径：如 `root.dbm.g_0.**`

### 4.2 被拒绝的语法

| SQL 特征 | 实际错误 | 结论 |
|---|---|---|
| `UNION` | `700 ... line 1:108 no viable alternative at input '... UNION'` | 不支持；不能把 UNION 重写为单区间（见 §7.3） |
| 子查询在 `FROM` 中 | `700 ... line 5:5 no viable alternative at input 'SELECT ... FROM ('` | 不在验证子集 |
| 窗口函数 `LAG(...) OVER` | `700 ... line 1:26 no viable alternative at input 'SELECT LAG(AVG(s_130), 1) OVER'` | 不支持 |
| `SELECT 1`（树） | `700 ... line 1:8 no viable alternative at input 'SELECT 1'` | 树方言必须有 `FROM` |
| `SELECT 1`（表） | `701: From clause must not be empty` | 表方言也必须有 `FROM` |
| `SHOW TABLES`（树） | `700 ... line 1:5 no viable alternative at input 'SHOW TABLES'` | 树/表方言不能混用 |
| `EXPLAIN FORMAT JSON` | `700 ... line 1:8 mismatched input 'FORMAT' expecting {<EOF>, ';'}` | 无 JSON 计划 |
| `EXPLAIN ANALYZE FORMAT JSON` | `700 ... line 1:16 mismatched input 'FORMAT' expecting {<EOF>, ';'}` | 无 JSON analyze |
| `EXPLAIN VERBOSE` | `700 ... line 1:8 mismatched input 'VERBOSE' expecting {<EOF>, ';'}` | 无 verbose |
| `LAST` + 值过滤 | `701: Only time filters are supported in LAST query` | LAST 只能带时间过滤 |
| `GROUP BY TIME(1h)` 无显式范围 | `701: The query time range should be specified in the GROUP BY TIME clause.` | 必须提供 `[start,end)` |
| 原始 q08 文件（中文行内注释） | `700 ... line 4:60 token recognition error at: '：'` | 注释 token 不可安全解析 |
| q08 去注释版本（子查询 + LAG） | 先命中子查询 `FROM (` 错误 | 仍不支持 |

### 4.3 被接受但危险/非等价的语法

| SQL 特征 | 观测 | 风险 |
|---|---|---|
| `COUNT(*)` | 返回 **每个序列一个 COUNT 列**；200-series device 返回 200 个值列 | 不是“行数”单值；成本/结果形状都被低估 |
| `SELECT *` | 200-series device 返回 `Time + 200` 列 | 宽行、传输成本高 |
| `LAST` 多序列/多设备 | 每个序列/设备一行（4 列：`Time, Timeseries, Value, DataType`） | 与 Native 单行结果形状不同 |
| `LAST` 单序列 FLOAT | `LAST` 显示 `34579.86`；Native 返回 `34579.859375` | 浮点显示精度不同；不能无容差比较 |
| `ALIGN BY DEVICE` | 结果列变为 `Time, Device, ...`；2 设备时 100 行 vs Native 50 行 | 形状变化，不是 drop-in rewrite |
| 完整 series path 放在 `FROM` | EXPLAIN 0 行；执行只返回 `Time` 列 + 0 行 | 非法路径被静默接受 |
| 不存在的 device path | EXPLAIN 0 行；执行只返回 `Time` 列 + 0 行 | 静默空结果，不是成功 |
| 未知函数 `MAVG`/`TIME_FLOOR` | 执行成功但只有 `Time` 列、0 行；EXPLAIN 0 行 | 未知函数必须 fail closed |

### 4.4 当前项目 Parser 兼容性

**阶段 0 仓库中没有 Parser 实现**，因此不存在可对本项目真实 SQL 做端到端
验证的“当前项目 Parser”。当前环境安装了 `sqlglot 30.18.0`，仅作为候选做了
只读兼容性测试：

- SQLGlot 没有 `iotdb` dialect；`Dialect.get_or_raise("iotdb")` 抛出
  `ValueError: Unknown dialect 'iotdb'`。
- 对 47 条 workload 的 SQL 文本运行默认 `sqlglot.parse_one`：**38 条可解析，
  9 条 `ParseError`**。9 条中 8 条是 4 个库的 `GROUP BY ([start,end),interval)`
  变体（q7/q11），1 条是 `root.dbm.g_0.*` 通配路径
  （`dbm_q12_union`）。
- 对 24 条负例运行同一 API：**17 条被 SQLGlot 接受，7 条解析失败**。
  被接受的包括 server 明确拒绝的 `SELECT 1`、树模型 `SHOW TABLES`、
  `EXPLAIN FORMAT JSON`、`EXPLAIN VERBOSE`、LAST 值过滤；对不认识的
  结构还会降级为 `Command`。

因此，**SQLGlot 解析成功不等于 IoTDB 接受，也不等于语义正确**。阶段 2 必须
实现 Parser Adapter + 已验证语法子集白名单 + 函数/路径白名单；未知语法返回
`UNSUPPORTED`，禁止自动改写，不能把 SQLGlot 作为唯一验收依据。

测试命令（只读、不连接 IoTDB）：

```bash
python - <<'PY'
import json, sqlglot
from pathlib import Path
w = json.loads(Path("tests/fixtures/sql/workload_cases.json").read_text())
for c in w["cases"]:
    try:
        sqlglot.parse_one(c["sql"])
        print(c["case_id"], "OK")
    except Exception as e:
        print(c["case_id"], "FAIL", type(e).__name__)
PY
```

## 5. EXPLAIN

### 5.1 输出格式

`EXPLAIN <SELECT>` 返回单列 `distribution plan`，每行一段 ASCII 文本。
`EXPLAIN` 的列名和文本格式都是版本相关的，不是稳定 JSON schema。

示例（`native_scan.txt`）：

```text
┌────────────────────────────────┐
│AlignedSeriesScan-1             │
│Series: root.test.g_0.d_0[s_130]│
│QueryAllSensors: false          │
│ScanOrder: ASC                  │
│Partition: 100                  │
└────────────────────────────────┘
```

注意：该查询本身带时间范围，但计划文本没有打印时间谓词。**计划中缺少某行
不代表该谓词不存在**；不能用“文本里没看到”来证明计划未生效。

### 5.2 观测到的算子

| 算子/结构 | 场景 |
|---|---|
| `AlignedSeriesScan` | 普通单/多序列扫描；属性含 `Series`、`QueryAllSensors`、`ScanOrder`、`Limit`、`Partition` |
| `AlignedSeriesAggregationScan` | 聚合；属性含 `Aggregator-N: <func>, SINGLE` |
| `LastQuery` → `LastQueryScan` | `SELECT LAST ...`；`Aligned: true` |
| `FullOuterTimeJoin` | 多设备普通查询的时间对齐 |
| `InnerTimeJoin` | 带值过滤/降序的多设备查询（探测中观测到） |
| `DeviceView` | `ALIGN BY DEVICE`；属性 `DeviceCount` |
| `IdentitySink` | `EXPLAIN ANALYZE` 的根输出算子 |
| `Limit` | 下推到扫描节点的属性（如 `Limit: 10`） |
| `ScanOrder` | `ASC` / `DESC` |

这些是文本中的算子名，不是公开的枚举 API。版本升级必须重新生成 fixture，
不能假设算子名永久稳定。

### 5.3 计划波动性

- `PlanNodeId N`、算子名后缀数字、`Partition`、fragment ID、IP/端口；
- 树形绘制字符和空白；
- `EXPLAIN ANALYZE` 的全部成本/CPU/wall time/内存/文件计数。

**不要对原始计划文本做 hash。** 阶段 1 应实现规范化签名：只保留算子类型、
父子关系和语义属性（`Series`、`ScanOrder`、`Limit`、聚合函数、`Order`、
`DeviceCount`、`Aligned`），忽略 ID、分区、地址和运行时数值。该方案尚未实现，
见 `tests/fixtures/plans/README.md`。

## 6. EXPLAIN ANALYZE

### 6.1 可用性与执行证据

`EXPLAIN ANALYZE <SELECT>` 返回单列 `Explain Analyze`，本次探测共 27 行。
与 `EXPLAIN` 不同，它输出运行时统计和实际输出行数，说明它走真实执行路径：

```text
Analyze Cost: 0.481 ms
Fetch Partition Cost: 0.126 ms
Fetch Schema Cost: 0.038 ms
Logical Plan Cost: 0.351 ms
Logical Optimization Cost: 0.156 ms
Distribution Plan Cost: 4.096 ms
Dispatch Cost: 13.950 ms
Fragment Instances Count: 1

FRAGMENT-INSTANCE[...][State: FINISHED]
  Total Wall Time: 12 ms
  ...
    [PlanNodeId 5]: IdentitySinkNode(IdentitySinkOperator)
        CPU Time: 10.965 ms
        output: 11 rows
      [PlanNodeId 1]: AlignedSeriesScanNode(AlignedSeriesScanOperator)
          CPU Time: 10.934 ms
          output: 11 rows
```

（完整去敏样例见 `tests/fixtures/plans/explain_analyze_native.txt`。）

`Fragment Instances Count: 1`、`State: FINISHED`、算子 CPU 和 `output: 11 rows`
是运行时观测；不能把 `EXPLAIN ANALYZE` 当作“只看计划、不执行”的命令。
本阶段只在只读 SELECT 上使用它，没有验证写语句或副作用，也没有在生产路径
默认执行。

### 6.2 限制

- 只有文本输出；JSON/VERBOSE 均被拒绝；
- 成本、wall time、CPU、内存数值高度波动，不能作为固定 benchmark 阈值；
- fragment ID 和地址必须去敏；
- 正式查询前后默认执行 ANALYZE 会改变缓存和负载，禁止作为常规验证手段；
- 计划不可用时必须记录 `UNAVAILABLE`，空计划记录 `EMPTY`，不能伪造字段。

## 7. 结果语义与策略影响

### 7.1 LAST 与 Native 最新值

在现有干净单序列数据上，单序列、仅时间过滤时，LAST 与 Native 的**时间戳
一致**；六种类型均通过归一化差分：

| 类型 | 结果 |
|---|---|
| INT32 / INT64 | 精确整数一致 |
| BOOLEAN / TEXT | 归一化后一致 |
| FLOAT | LAST `34579.86` vs Native `34579.859375`；1e-6 容差内一致 |
| DOUBLE | 12 位相对/绝对容差内一致；200 个时间采样通过 |

补充了每类型 200 个 `WHERE time <= t`（含 min/max 边界），合计 1,200 对，
0 失败；8 个时间边界/空结果 case 全部通过。2026-11-01 又在用户授权的专用库
`root.last_cert_20261101` 中完成 79 个可写边缘差分（缺失测量值、重复时间戳、
乱序写入、时间缺口、空序列、显式 SQL NULL），79/79 通过；目标库随后已删除。
重复时间戳本次观测为单值覆盖，乱序写入后按时间升序存储，显式 SQL NULL 不产生
该序列的数据点。完整数据见
[`LAST_CERTIFICATION.md`](LAST_CERTIFICATION.md)。

形状差异：

```text
SELECT LAST s_130 FROM root.test.g_0.d_0
Time, Timeseries, Value, DataType
```

而 Native `SELECT s_130 ... ORDER BY time DESC LIMIT 1` 返回：

```text
Time, root.test.g_0.d_0.s_130
```

多序列 LAST 返回每个序列一行；Native 返回一行多列。`LAST` 不接受值过滤
（`701: Only time filters are supported in LAST query`）。因此 LAST 不是
可直接替换的 Native 等价查询；需要显式的 single-series 限制、类型白名单、
数值容差和结果归一化层。当前项目还没有这层。

**仍未覆盖：** TTL、重启/崩溃恢复、并发/背景负载、NaN/Infinity、别名/
`OFFSET`/wildcard 等变体；错误路径仍需 schema 校验，空结果不能证明路径合法。
2.0.6 客户端对全 None 的 `insert_aligned_record` 会抛 `ValueError`，adapter
必须显式处理。LAST 仍为 `NOT_VERIFIED`。

### 7.2 MAX_TIME + LAST_VALUE

`SELECT MAX_TIME(s_x), LAST_VALUE(s_x) ...` 可执行，计划为
`AlignedSeriesAggregationScan`，`ScanOrder: DESC`。INT 和 FLOAT 的最新值
与 Native 精确一致；但结果形状是：

```text
Time=0, MAX_TIME(...), LAST_VALUE(...)
```

列名、Time 列语义都与 Native 不同；单次计时没有稳定收益
（见策略目录）。它不是可直接替换的等价查询。

### 7.3 UNION 与区间合并

`UNION` 被 server 拒绝。把两个重叠区间合并成一个连续区间可以执行
（单区间返回 62 行），但这不保持 UNION 的重复行和边界语义，不能作为自动
重写。负例见 `negative_cases.json:union_to_single_range_not_equivalent`。

### 7.4 ALIGN BY DEVICE

`ALIGN BY DEVICE` 的计划从 `FullOuterTimeJoin` 变为 `DeviceView`，结果列变为
`Time, Device, <series...>`；两设备探测中 Native 50 行，by-device 100 行。
它改变行形状且本次探测更慢，不是 drop-in 优化。

### 7.5 静默空结果

以下情况 server 不报错，但结果不可用：

- `FROM` 使用完整 series path（`root.test.g_0.d_0.s_130`）；
- device path 拼写错误或不存在；
- 未知函数 `MAVG`、`TIME_FLOOR`；
- 时间范围完全落在数据之外（这是合法空结果，但必须与非法路径区分）。

前三种必须由 Parser/Validator 结合 schema 和函数白名单 fail closed。空结果
本身永远不能证明 SQL 合法或策略有效。

### 7.6 Server 配置入口（阶段 1 只读补充）

2026-11-01 用 2.0.6 客户端补充探测动态配置语法：

| 语句 | 实际结果 |
|---|---|
| `SHOW CONFIG` | 拒绝：`700: Error occurred while parsing SQL to physical plan: line 1:5 no viable alternative at input 'SHOW CONFIG'` |
| `SHOW CONFIGURATION` | 拒绝：同类语法错误 |
| `SHOW VARIABLES` | 可用；列 `Variable, Value` |

`SHOW VARIABLES` 只返回 5 个集群复制/共识配置：

```text
ClusterName=defaultCluster
DataReplicationFactor=1
SchemaReplicationFactor=1
DataRegionConsensusProtocolClass=org.apache.iotdb.consensus.iot.IoTConsensus
SchemaRegionConsensusProtocolClass=org.apache.iotdb.consensus.ratis.RatisConsensus
```

这些是集群级配置，不是查询级 tuning 参数；本轮也没有验证修改语法、热生效、
重启要求或回滚。因此只能把 `SHOW VARIABLES` 记为“可读”，不能把
`CAND_DB_PARAMETER` 升级为可执行策略。

## 8. Workload 分类

`tests/fixtures/sql/workload_cases.json` 共 47 条，全部被 server 接受：

| 分类 | 数量 |
|---|---:|
| `SUPPORTED` | 40 |
| `SUPPORTED_WITH_SHAPE_WARNING`（LAST q8） | 4 |
| `SUPPORTED_HEAVY_WILDCARD`（MATAOF wildcard q12） | 1 |
| `SUPPORTED_SILENT_EMPTY_WARNING`（错误路径） | 1 |
| `SUPPORTED_CORRECTED_VARIANT`（修正路径） | 1 |

执行状态：

| 状态 | 数量 | 说明 |
|---|---:|---|
| `SUCCESS` | 39 | 有结果 |
| `SUCCESS_EMPTY` | 7 | SQL 成功但 0 行；含值过滤无匹配/错误路径 |
| `SUCCESS_LARGE` | 1 | wildcard 结果 1200 行 × 101 列 |

EXPLAIN 状态：46 条 `AVAILABLE`，1 条 `EMPTY_PLAN`
（`test_benchmark_path_original`）。

来源：46 条 MAPO、1 条 MATAOF。`dbm_q12_union` 是历史命名，实际 SQL 是
wildcard，不是 UNION。该 workload 是本地 benchmark 生成的查询，不是生产
查询；也没有覆盖 `ORDER BY time DESC LIMIT 1` 这种 Native 最新值模式。
2026-11-01 对用户授权的 `query/queries_dbs` 全量 12 个文件、24,000 条语句做了
补充扫描，仍然没有符合条件的 Native 单序列最新值语句；2,000 条 `SELECT LAST`
全部是 q8 的多序列/全设备查询。因此不能用它证明 LAST 重写有实际 workload
需求。

## 9. 负例与错误分类

`tests/fixtures/sql/negative_cases.json` 共 24 条，覆盖：

| 类型 | 数量 | 代表 |
|---|---:|---|
| `UNSUPPORTED_BY_SERVER` | 12 | UNION、子查询、LAG、`SELECT 1`、JSON/VERBOSE EXPLAIN、LAST 值过滤 |
| `UNKNOWN_SILENT_EMPTY` | 2 | MAVG、TIME_FLOOR |
| `INVALID_SILENT_EMPTY` | 2 | 完整 series path、错误 device path |
| `SUPPORTED_WITH_SHAPE_WARNING` | 2 | LAST 多序列、ALIGN BY DEVICE |
| `SUPPORTED_WITH_SEMANTIC_WARNING` | 1 | COUNT(*) 逐序列展开 |
| `SUPPORTED_WITH_RESULT_SIZE_WARNING` | 1 | SELECT * 宽行 |
| `SUPPORTED_WITH_PRECISION_WARNING` | 1 | LAST FLOAT 精度 |
| `SUPPORTED_TABLE_MODEL_ONLY` | 1 | table `SHOW TABLES` |
| `REJECTED_BY_OPTIMIZER_POLICY` | 1 | DDL/DML/权限/系统管理语句未发送 |
| `REWRITE_NOT_EQUIVALENT` | 1 | UNION 单区间合并不等价 |

错误文本保留在 fixture 的 `observed.error_message` 中，便于 Parser/Validator
回归测试。不要把 `UNKNOWN_SILENT_EMPTY` 当作 `UNSUPPORTED_BY_SERVER`：
前者 server 接受但结果不可用，后者明确拒绝；两者对 fallback 和日志的语义不同。

## 10. 优化入口审计摘要

| 入口 | 观测 | 状态 |
|---|---|---|
| SQL rewrite：LAST | `SELECT LAST` 有独立 `LastQuery` 计划；六类型/1,200 采样/8 边界 + 79 授权边缘差分（缺失值/重复时间戳/乱序/缺口/空序列/SQL NULL）通过；2026-11-01 变体边界/结果契约复核和两次 120 块计时完成；TTL/重启/并发/NaN 未测 | `NOT_VERIFIED`（子标记 `DEMAND_UNPROVEN`、`FALLBACK_CONTRACT_ONLY`） |
| SQL rewrite：MAX_TIME + LAST_VALUE | 聚合计划可执行；结果形状/列名不同；单次计时无稳定收益 | `NOT_VERIFIED` |
| SQL rewrite：ALIGN BY DEVICE | `DeviceView` 计划；行形状变化；探测更慢 | `NOT_VERIFIED` |
| SQL rewrite：谓词简化/重复谓词 | 计划相同，无收益 | `NOT_VERIFIED` |
| SQL rewrite：series path | 静默空结果 | `UNSUPPORTED`（子标记 `INVALID`） |
| SQL rewrite：UNION 合并 | UNION 被拒绝；单区间不等价 | `UNSUPPORTED`（子标记 `NOT_EQUIVALENT`） |
| 连接级 `fetch_size` | 可设置；10,000 行结果上均值差异约 3.6%，方差重叠 | `NOT_VERIFIED` |
| 客户端并行按设备拆分 | 行数一致但更慢（2/10 设备均更慢） | `NOT_VERIFIED` |
| server 参数/Hint | `SHOW CONFIG`/`SHOW CONFIGURATION` 被拒绝；`SHOW VARIABLES` 只有 5 个集群变量；无查询级入口 | `NOT_VERIFIED`（子标记 `UNKNOWN`） |
| table model | 无用户表 | `UNSUPPORTED`（子标记 `NO_USER_TABLE`） |
| 客户端缓存 | 未定义一致性/新鲜度 | `NOT_VERIFIED`（子标记 `UNKNOWN`） |

这些状态的完整审计卡、计时数据和阻塞结论见
[`STRATEGY_CATALOG.md`](STRATEGY_CATALOG.md)。

## 11. 未验证/未知项

以下项目**没有**在阶段 0/1 得到可依赖的结论，不能写成已支持：

1. `SHOW CONFIG`/`SHOW CONFIGURATION` 语法被 server 拒绝；`SHOW VARIABLES`
   可读但只返回 5 个集群复制/共识变量；动态参数修改语法、热生效、重启要求
   和回滚仍未验证；
2. SQL Hint、Session 级优化开关、查询超时配置；
3. 服务端并行度、fragment 调度、plan cache、存储层 compaction 调优；
4. 客户端压缩、连接池复用对查询的实际影响（`SessionPool` 存在但未做性能验证）；
5. 冷缓存、并发负载、远程网络、跨主机时钟差异；
6. NULL、重复时间戳、乱序写入、数据缺口已有授权专用库的 79/79 边缘差分；
   TTL、重启/崩溃恢复、并发和 NaN/Infinity 仍未验证；
7. 表模型用户表、表模型 EXPLAIN、表模型聚合语义；
8. 权限模型和只读账号；阶段 0 没有执行 GRANT/REVOKE；
9. IoTDB server 自身 CPU/内存/IO 指标；没有 DB 主机采集代理；
10. `EXPLAIN ANALYZE` 在写语句或长查询上的副作用；本阶段只测只读 SELECT。

这些未知项必须在后续阶段用独立测试/隔离实例验证，不能靠理论推断补全。

## 12. 证据索引

| 内容 | 文件 |
|---|---|
| 47 条 workload、执行摘要、结果 digest | `tests/fixtures/sql/workload_cases.json` |
| 47 个纯 SQL 文件 | `tests/fixtures/sql/workload/*.sql` |
| 24 条负例、错误文本、预期行为 | `tests/fixtures/sql/negative_cases.json` |
| 10 个文本计划 + 3 个空计划 | `tests/fixtures/plans/` |
| 计划来源、查询、行数、波动性 | `tests/fixtures/plans/index.json` |
| 环境、版本、数据集 | `docs/ENVIRONMENT_REPORT.md` |
| 策略状态与阻塞 | `docs/STRATEGY_CATALOG.md` |
| 阶段状态与测试结果 | `docs/IMPLEMENTATION_STATUS.md` |

Fixture 完整性测试：

```bash
python -m pytest -q tests/test_phase0_fixtures.py
```

该测试只校验 fixture 的结构、文件一致性、空计划标记和凭据泄漏，不连接
IoTDB，也不证明 SQL 语义正确。
