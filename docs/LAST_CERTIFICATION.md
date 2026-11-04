# LAST 单序列策略认证报告（只读 + 授权可写边缘数据）

- **策略：** `CAND_LAST_SINGLE_SERIES`
- **认证日期：** 2026-11-01
- **server：** Apache IoTDB 2.0.6，build `c6ecbde`
- **客户端：** `apache-iotdb 2.0.6`（临时 venv；不是仓库依赖）
- **方言：** tree
- **性质：** 只读认证 + 用户授权的专用库可写边缘认证；2026-11-01 追加只读复核
  （变体边界、结果契约、全量 workload 需求扫描和扩展计时）；未修改 IoTDB
  源码、未修改 server 配置
- **状态：** **`NOT_VERIFIED`**（主状态不变；边缘数据子项已补测）
- **结论：** 干净单序列数据、授权专用库的边缘数据、时间边界、生效、受控收益和
  回退契约证据通过；**真实 workload 需求和生产 Executor 回退仍未认证**，因此
  不能升级为 `VERIFIED`，也不能启用该策略。全量扫描 24,000 条授权 workload
  仍未找到符合条件的 Native 最新值语句。

本报告是 `docs/STRATEGY_CATALOG.md` 中 LAST 候选的专项补充。它不改变 LAST 自身
的 `NOT_VERIFIED` 状态；阶段 0 的总阻塞
`BLOCKED: NO_VERIFIED_NON_NATIVE_STRATEGY` 已由后续固定 epoch 契约下的
`S1_REPEAT_CACHE` 认证解除，但不构成对 LAST 的认证。

## 1. 认证范围与五项证明

`IMPLEMENTATION_GUIDE.md` 要求入口、正确性、生效、收益、回退五项证明。本轮
只读认证的结果：

| 证明 | 本轮结果 | 说明 |
|---|---|---|
| 入口 | **通过** | server 接受 `SELECT LAST`；独立 `LastQuery` 计划 |
| 正确性 | **通过（授权范围内）** | 六类型、1,200 个时间采样、8 个边界、79 个可写边缘差分全部通过；2026-11-01 变体复核覆盖别名、`LIMIT`/`ORDER BY`、`OFFSET`、无 ORDER BY、wildcard、限定路径和空区间；NULL/缺失、重复、乱序、缺口、空序列已测；TTL/重启/并发/NaN 未测 |
| 生效 | **通过** | `AlignedSeriesScan/Limit/ScanOrder: DESC` → `LastQuery` → `LastQueryScan` |
| 收益 | **通过（受控热缓存实验）** | 30 块初始运行的 TEXT 区间在本轮复核中跨 0；两次独立 120 块复测六类型均值差 95% bootstrap CI 均不跨 0，平均相对降低约 11.7%–42.6%；仍不能当生产收益 |
| 回退 | **仅契约层通过** | 注入 LAST 失败/错形时执行 Native 一次；无生产 Executor、无 timeout/连接中断测试 |

因此主状态仍为 **`NOT_VERIFIED`**，剩余子标记：

- `DEMAND_UNPROVEN`：47 条 fixture 以及 `query/queries_dbs` 全量 24,000 条
  语句中都没有符合条件的 Native 单序列 `ORDER BY time DESC LIMIT 1`；
  2,000 条 `SELECT LAST` 全部是 q8 的多序列/全设备查询，已经使用 LAST；
- `FALLBACK_CONTRACT_ONLY`：回退是注入式契约测试，不是生产执行器。

2026-11-01 已按用户授权在专用库完成边缘数据补测，`EDGE_CASES_UNTESTED`
所缺的 NULL/缺失、重复时间戳、乱序、缺口和空序列子项已有实测证据；残余未测
类别见 §7.3。

## 2. 数据集与测试方法

### 2.1 数据快照

从现有只读测试数据中选择六条不同数据类型的单序列：

| 类型 | 序列 | 点数 | 最小时间 | 最大时间 |
|---|---|---:|---:|---:|
| FLOAT | `root.test.g_0.d_0.s_130` | 10,000 | 1640966405000 | 1641016400000 |
| DOUBLE | `root.test.g_0.d_0.s_133` | 10,000 | 1640966405000 | 1641016400000 |
| INT64 | `root.test.g_0.d_0.s_70` | 10,000 | 1640966405000 | 1641016400000 |
| INT32 | `root.db800.g_0.d_0.s_1666` | 3,000 | 1640966405000 | 1640981400000 |
| TEXT | `root.dbl.g_0.d_0.s_9807` | 3,000 | 1640966405000 | 1640981400000 |
| BOOLEAN | `root.dbl.g_0.d_0.s_1079` | 3,000 | 1640966405000 | 1640981400000 |

补充抽样了 **60** 条序列的 `COUNT`，计数与数据生成快照的期望值一致；**抽样中
没有观察到缺口或重复样本**。这不证明整个 3.2M 序列集合无异常，只是说明本轮
可用于“干净数据”差分测试。

### 2.2 对照写法

Native 基准：

```sql
SELECT <single_series> FROM <single_device> [WHERE time ...]
ORDER BY time DESC LIMIT 1
```

LAST 候选：

```sql
SELECT LAST <single_series> FROM <single_device> [WHERE time ...]
```

两条查询都通过 `Session.execute_query_statement` 执行，逐行完整读取结果，并在
`finally` 中调用 `close_operation_handle()`。

### 2.3 归一化与比较规则

LAST 返回四列：

```text
Time, Timeseries, Value, DataType
```

Native 返回两列：

```text
Time, <full series path>
```

比较前执行显式归一化：

1. 非空 LAST 必须恰好一行、四列；只有两侧都为空才允许 0 行；
2. `Timeseries` 必须等于请求的完整序列路径；
3. `DataType` 必须等于期望类型；
4. 取 `Time` 和 `Value`，映射为 Native 的 `Time` 和第二列；
5. `INT32`/`INT64` 精确整数比较；
6. `TEXT` 精确字符串比较；
7. `BOOLEAN` 归一化为布尔值后比较；
8. `FLOAT` 使用 `rel_tol=1e-6, abs_tol=1e-6`；例如 LAST `34579.86` 与 Native
   `34579.859375` 通过；
9. `DOUBLE` 使用 `rel_tol=1e-12, abs_tol=1e-12`；
10. 只有两侧都为空才判定空结果通过；一侧为空、另一侧非空一律失败。

这套归一化只存在于认证脚本中，**项目还没有结果归一化层**。

## 3. 正确性证据

### 3.1 六类型基础差分

| 类型 | 条件 | LAST 归一化值 | Native 归一化值 | 结果 |
|---|---|---|---|---|
| FLOAT | 无过滤 | `(1641016400000, 34579.86)` | `(1641016400000, 34579.859375)` | 通过（容差） |
| DOUBLE | 无过滤 | `(1641016400000, 4.81)` | `(1641016400000, 4.81)` | 通过 |
| INT64 | 无过滤 | `(1641016400000, 4)` | `(1641016400000, 4)` | 通过 |
| INT32 | 无过滤 | `(1640981400000, -12)` | `(1640981400000, -12)` | 通过 |
| TEXT | 无过滤 | `(1640981400000, "Ry")` | `(1640981400000, "Ry")` | 通过 |
| BOOLEAN | 无过滤 | `(1640981400000, false)` | `(1640981400000, false)` | 通过 |

`WHERE time < <max_time>` 的六类型差分也全部通过；例如 FLOAT 在
`1641016395000` 上 LAST `34579.29` 对 Native `34579.2890625`，通过容差。
完整的归一化值和原始行见
`tests/fixtures/last/last_certification_summary.json`。

### 3.2 时间/值扫描

对每个类型执行 **200** 个 `WHERE time <= t` 过滤，其中包含：

- `min_time - 1`、`min_time`、`min_time + 1`；
- `max_time - 1`、`max_time`、`max_time + 1`；
- 194 个在 `[min_time - 1, max_time + 1]` 内的固定种子随机时间。

结果：

| 类型 | 采样数 | 通过 | 失败 | 两侧都空 |
|---|---:|---:|---:|---:|
| FLOAT | 200 | 200 | 0 | 1 |
| DOUBLE | 200 | 200 | 0 | 1 |
| INT64 | 200 | 200 | 0 | 1 |
| INT32 | 200 | 200 | 0 | 1 |
| TEXT | 200 | 200 | 0 | 1 |
| BOOLEAN | 200 | 200 | 0 | 1 |

合计 1,200 个差分对，0 个失败；每个类型的空对都是 `t = min_time - 1`。
这扩大了 FLOAT/DOUBLE 的值样本，但仍不等于全量或极端值（NaN/Infinity）测试；
边缘数据覆盖见 §3.5。

### 3.3 时间边界与空结果

在一个 FLOAT 单序列上执行 8 个边界 case：

| case | 过滤 | 结果 |
|---|---|---|
| `eq_min` | `time = 1640966405000` | 两侧同一行，通过 |
| `le_min` | `time <= 1640966405000` | 两侧同一行，通过 |
| `lt_min` | `time < 1640966405000` | 两侧都空，通过 |
| `gt_max` | `time > 1641016400000` | 两侧都空，通过 |
| `eq_non_timestamp` | `time = 1640966405001` | 两侧都空，通过 |
| `range_one_point` | `time >= 1640966405000 AND time <= 1640966405000` | 两侧同一行，通过 |
| `range_empty` | `time > 1641016400000 AND time < 1641016405000` | 两侧都空，通过 |
| `range_mid` | `time >= 1640990000000 AND time <= 1640991000000` | 两侧同一行，通过 |

注意：`LAST` 对**不存在的 device path** 也返回空结果，且不报错；空结果本身
不能证明路径合法。必须由 Validator 结合 schema 拒绝错误路径。

### 3.4 不适用形状负例

| case | 实际观测 | 决策 |
|---|---|---|
| 多序列 | LAST 每个序列一行（2 行）；Native 一行、2 个值列 | 拒绝改写 |
| 多设备 | LAST 每个设备一行（2 行）；Native 一行、2 个值列 | 拒绝改写 |
| 值过滤 | LAST 报错 `701: Only time filters are supported in LAST query`；Native 成功 | 拒绝改写 |
| 错误 device path | LAST 和 Native 都返回空，但不证明路径合法 | 必须由 schema 校验拒绝 |
| `LIMIT 2` | Native 返回 2 行；LAST 只返回最新一行 | 拒绝改写 |
| `ORDER BY time ASC LIMIT 1` | Native 返回最早一行；LAST 永远返回最新一行 | 拒绝改写 |
| `LIMIT 1 OFFSET 0` | Native 与 LAST 值相同；但 OFFSET 不在已验证语法子集 | 拒绝改写（Parser 报 `OFFSET`） |
| `LIMIT 1 OFFSET 1` | Native 返回倒数第二行；LAST 返回最新行 | 拒绝改写 |
| 无 `ORDER BY` 的 `LIMIT 1` | Native 返回最早存储行；LAST 返回最新行 | 拒绝改写 |
| `ORDER BY time DESC` 无 `LIMIT` | Native 返回 10,000 行；LAST 只返回一行 | 拒绝改写 |
| wildcard `SELECT *` | LAST 返回 200 行（每序列一行）；Native 一行 200 个值列 | 拒绝改写 |
| 限定序列路径 `SELECT LAST root...s_130` | server 报 `701: Path can not start with root in select clause` | 只能使用 FROM 中设备下的裸序列名 |
| `LAST` + 值过滤 | server 报 `701: Only time filters are supported in LAST query` | 拒绝改写 |
| 别名 `SELECT LAST s_130 AS v` | LAST 接受但忽略别名，仍返回四列；Native 列名是 `v` | 拒绝改写（结果形状不等价） |
| `SELECT LAST ... LIMIT 1` | server 接受，结果与不带 LIMIT 的 LAST 相同；候选规则仍只接受固定形态 | 不作为适用形状 |
| `SELECT LAST ... ORDER BY time DESC` | server 接受，结果与不带 ORDER BY 的 LAST 相同；候选规则仍只接受固定形态 | 不作为适用形状 |

2026-11-01 变体复核的 Parser 边界：

- `SELECT LAST <series> FROM <device>`：`UNSUPPORTED`，显式原因
  `LAST_SYNTAX`；因此当前 Parser 不能直接接受 LAST 候选；
- `SELECT ... AS v`：`UNSUPPORTED`，`SELECT_ALIAS`；
- `LIMIT 1 OFFSET 0/1`：`UNSUPPORTED`，`OFFSET`；
- `SELECT ... LIMIT 1`（无 ORDER BY）和 `SELECT ... ORDER BY time DESC`
  （无 LIMIT）：**Parser 接受**，所以 LAST 适用规则必须同时显式要求
  `order_by == [time DESC]` 且 `limit == 1`，不能只看 `LIMIT` 或只看
  `ORDER BY`；
- wildcard、多序列、多设备：Parser 接受，必须由 LAST 专用规则拒绝；
- 值过滤：Parser 拒绝（`UNKNOWN_WHERE_EXPRESSION`），且 server LAST 也拒绝。

这些负例说明：正确性不仅依赖结果比较，还必须在 Parser/Validator 中限制
**单序列、单设备、仅时间过滤、`ORDER BY time DESC LIMIT 1`、裸序列名**。
通用 Parser/Validator 已在阶段 4 实现，但 LAST 候选会被 Parser 的
`LAST_SYNTAX` 规则拒绝；LAST 专用适用规则和结果归一化尚未实现。

### 3.5 授权可写边缘数据差分（2026-11-01）

用户授权后，在专用库 `root.last_cert_20261101` 中创建 8 个 aligned device，
执行 19 次 DML 尝试（SQL 与 Python Native API；其中 1 次为客户端全 None
异常，未发送），完成 79 个单序列差分 case，
**79/79 通过**。测试后 `DROP DATABASE` 成功，目标库消失；原有 7 个 database
（含 `root.__system`）未变化。完整的脱敏摘要见
[`tests/fixtures/last/last_edge_certification_summary.json`](../tests/fixtures/last/last_edge_certification_summary.json)。

| 场景 | case | 通过 | 实际观测 |
|---|---:|---:|---|
| 干净基线 | 18 | 18 | 六类型完整行；无过滤、`time <= 2500`、`time > 2500` 均一致 |
| 缺失测量值 | 32 | 32 | SQL 部分列写入；每个序列按自己的最后有效点返回；精确时间戳缺席时 LAST/Native 都为空 |
| 重复时间戳 | 3 | 3 | 同一时间戳写两次均接受；`COUNT=1`，最终值 2.0（单值覆盖，last-write-wins） |
| 乱序写入 | 4 | 4 | 按 3000、1000、2000 写入均接受；存储按时间升序为 1000、2000、3000 |
| 时间缺口 | 9 | 9 | 1000/3000/5000；缺口时间、空区间、范围过滤均一致 |
| 空序列 | 3 | 3 | 从未写入的序列两侧都为空 |
| API None 过滤 | 6 | 6 | 2.0.6 客户端丢弃 None 测量值；精确时间戳缺席时两侧都为空 |
| 显式 SQL NULL | 4 | 4 | `INSERT ... VALUES(1000,NULL,2.5)` 接受；`s_float` 无数据点，`s_double=2.5`；两侧一致 |

补充观测：

- **重复时间戳**：两次 `insert_aligned_record` 同一时间戳均返回成功，查询只看到
  一行，值由第二次写入覆盖；这是本 server/配置的实测行为，不能外推到其他版本。
- **乱序写入**：三次乱序 API 写入均成功，`ORDER BY time ASC` 返回排序后的
  1000/2000/3000；LAST 与 Native 都返回 3000。
- **显式 SQL NULL**：server 接受 `NULL` 字面量，但该序列不产生数据点；
  `COUNT(s_float)=0`，`COUNT(s_double)=1`。不能据此声称“IoTDB 存储 NULL 并让
  LAST 返回 NULL”。
- **all-None API 调用**：2.0.6 客户端 `insert_aligned_record` 传入全 None 时，
  在过滤后触发客户端异常
  `ValueError: not enough values to unpack (expected 3, got 0)`，没有发送写入；
  写入前后 `COUNT` 不变。这是客户端行为，不是 LAST 语义，也不能当作 server
  已支持 NULL 的证据。
- **清理**：`DROP DATABASE root.last_cert_20261101` 返回成功；`SHOW DATABASES`
  中目标库不存在，前后 database 列表一致。原始结果在仓库外，不包含密码。

## 4. 生效证明

同一 FLOAT 查询的 EXPLAIN：

Native：

```text
AlignedSeriesScan-1
Series: root.test.g_0.d_0[s_130]
Limit: 1
QueryAllSensors: false
ScanOrder: DESC
Partition: 100
```

LAST：

```text
LastQuery-1
  └─ LastQueryScan-3
     Aligned: true
     Series: root.test.g_0.d_0[[s_130,FLOAT,GORILLA,,LZ4]]
     Partition: 100
```

计划从普通降序扫描变成独立的 `LastQuery` → `LastQueryScan`，说明不是只改
SQL 文本。该证据只说明计划改变，不说明结果在所有数据上等价。

## 5. 收益证据

### 5.1 协议

- 客户端 2.0.6；server 2.0.6；同一主机；单进程；
- 静态数据，热共享缓存，没有清缓存；
- 初始运行：每个类型每个 session 先做 5 次 warmup，30 个配对块；
- 扩展复核：每个类型每个 session 先做 10 次 warmup，120 个配对块，并用不同
  随机种子独立重复一次；
- 两个 `Session` 交替使用；
- 每块按正反交替，并在部分块随机打乱 Native/LAST 顺序；
- 完整读取结果；记录 `execute_call_ms`、`result_fetch_ms`、
  `client_wall_ms`；
- 配对差 = Native `client_wall_ms` − LAST `client_wall_ms`；
- 20,000 次 bootstrap 重采样，报告均值差的 95% 区间；另报告符号检验和
  胜率；
- p95 使用 nearest-rank 近似；即使 `n=120`，仍不能当作稳定 SLA。

### 5.2 结果

扩展复核第一次运行（seed `20261101`），单位 ms，`n=120`，两种写法错误数均为
0：

| 类型 | Native mean | LAST mean | 配对差 mean | 95% CI | 相对降低 | 胜率 |
|---|---:|---:|---:|---:|---:|---:|
| FLOAT | 3.841 | 2.475 | 1.365 | [1.332, 1.402] | 35.456% | 100.0% |
| DOUBLE | 4.363 | 2.533 | 1.830 | [1.734, 1.897] | 41.922% | 99.2% |
| INT64 | 3.704 | 2.478 | 1.226 | [1.200, 1.253] | 33.049% | 100.0% |
| INT32 | 2.865 | 2.477 | 0.388 | [0.353, 0.416] | 13.463% | 99.2% |
| TEXT | 3.032 | 2.512 | 0.520 | [0.427, 0.576] | 17.105% | 99.2% |
| BOOLEAN | 2.825 | 2.491 | 0.334 | [0.305, 0.362] | 11.720% | 97.5% |

第二次独立 120 块复测（seed `20261102`）六类型均值差 CI 同样不跨 0：

```text
FLOAT   [1.256, 1.352]   DOUBLE [1.728, 1.874]
INT64   [1.154, 1.360]   INT32  [0.380, 0.449]
TEXT    [0.244, 0.691]   BOOLEAN[0.332, 0.392]
```

观察与限制：

- 本轮 30 块复核中 TEXT 的均值差 CI 为 `[-0.351, 0.733]`，跨 0；扩展 120 块
  两次复测分离出正效应，因此不能只引用单次 30 块结果；
- 两次 120 块运行的相对降低约为 **11.7%–42.6%**，但绝对毫秒数随运行时段明显
  变化（例如 Native mean 从约 6.8 ms 变到约 3.0 ms），不能写成固定收益；
- 总差异主要来自 `execute_call_ms` 降低；`result_fetch_ms` 反而略高
  （LAST 四列、需要归一化），两者相抵后净收益仍为正；
- 符号检验在本样本量下均给出 `p < 1e-6` 量级；但样本来自同一主机、同一
  静态数据集、热缓存，不是独立生产负载；
- 相对降低幅度不能外推到其他数据集、远程部署或并发负载。

### 5.3 不能声称的内容

- 不能声称生产端到端收益；没有决策开销、结果归一化开销、连接池/并发和冷缓存；
- 不能把约 11.7%–42.6% 写成所有查询的固定收益；
- 不能忽略结果形状和归一化成本；
- 没有多进程/多日重复和 DB 主机指标。

## 6. 回退契约证据

认证脚本注入 LAST 结果，Native 侧使用真实只读连接并计数。方法要点：

- 成功路径只执行 LAST 一次，不执行 Native；预录的 Native 基线仅用于离线差分；
- LAST 报错、空结果、错误 `Timeseries`、错误 `DataType`、与基线不一致时，
  执行 Native 一次；
- 这不是生产 Executor 的双执行设计；生产路径仍必须默认
  `runtime_result_check=NOT_CHECKED`，不得为学习而双执行。

| case | 注入结果 | 选中路径 | Native 执行次数 |
|---|---|---|---:|
| `valid` | 归一化值与基线一致 | LAST | 0 |
| `wrong_timestamp` | 时间戳差 5000 ms | NATIVE_FALLBACK | 1 |
| `wrong_timeseries` | 返回了其他序列路径 | NATIVE_FALLBACK | 1 |
| `wrong_datatype` | `FLOAT` 被标成 `DOUBLE` | NATIVE_FALLBACK | 1 |
| `wrong_value` | 值被改成 100.0 | NATIVE_FALLBACK | 1 |
| `empty` | 0 行 | NATIVE_FALLBACK | 1 |
| `exception` | 注入 server 异常 | NATIVE_FALLBACK | 1 |

限制：没有 timeout、连接中断、请求是否已完成的确认、Server 端副作用检查；
没有生产 Executor、没有 Registry、没有真正的“只执行一次 + 失败后安全重试”
集成。因此只能称“契约层通过”。

## 7. 仍然缺失的认证项

### 7.1 专用可写测试数据集（2026-11-01 已授权并完成）

用户明确授权后，已创建并删除独立测试库 `root.last_cert_20261101`，没有写入
共享 benchmark database。实测回答：

- 创建/删除 database、aligned timeseries 以及 INSERT 均可执行；
- 重复时间戳被接受，最终只保留一个值（本次观测为第二次写入覆盖）；
- 乱序写入被接受，查询按时间升序返回；
- 缺失测量值表现为该序列在对应时间戳“无数据点”，不是可读取的 NULL 值；
- 对齐设备中某序列缺失时，LAST 与 Native 都按该序列自己的最后有效点返回；
- 显式 SQL `NULL` 被接受但不产生该序列的数据点；
- 清理后目标库不存在，原有 database 列表不变。

完整证据见 §3.5 和
[`tests/fixtures/last/last_edge_certification_summary.json`](../tests/fixtures/last/last_edge_certification_summary.json)。
重跑必须继续使用独立库、确认前置不存在并在 `finally` 中删除；不能在共享
benchmark database 上执行这些 DDL/DML。

### 7.2 真实需求证明

2026-11-01 对用户授权的 `query/queries_dbs` 全量文件做了逐语句扫描：

| 项目 | 数量 |
|---|---:|
| SQL 文件 | 12 |
| 语句总数 | 24,000 |
| Parser `SUPPORTED` / `UNSUPPORTED` | 12,000 / 12,000 |
| `ORDER BY time DESC LIMIT 1`（Parser 可解析） | **0** |
| 符合单序列、单设备、仅时间过滤、无 wildcard 的 Native 最新值 | **0** |
| `SELECT LAST` 语句 | 2,000 |
| 其中 q8 多序列/全设备、已经使用 LAST | 2,000 |

`tests/fixtures/sql/workload_cases.json` 的 47 条 fixture 同样没有该模式；
q8 本身已经是 `SELECT last ...`，不是待改写的 Native 写法。因此当前没有证据
表明真实 workload 需要这条改写。需要用户提供或确认一个代表性的“取单序列
最新值”workload；不能为了启用策略而虚构需求。

### 7.3 其他

- TTL、重启/崩溃恢复、压缩/合并、并发写入和背景负载；
- 多进程、多日、冷缓存和 DB 主机 CPU/IO/内存指标；
- 生产 Executor 的回退、超时和连接状态处理；
- FLOAT/DOUBLE 的 NaN/Infinity（当前未知，不能假设 server 支持）；
- 2.0.6 客户端对全 None 的 `insert_aligned_record` 会抛
  `ValueError`，adapter 必须显式处理这种输入，不能假定“无写入”；
- 结果归一化层尚未进入项目代码，当前只在认证脚本中存在。LAST 返回
  `Time, Timeseries, Value, DataType` 四列，Native 返回 `Time, <series>` 两列；
  FLOAT 的 `Value` 是十进制字符串（`34579.86`），Native 是
  `34579.859375`，需要明确的容差策略或改用其他结果获取方式；
- `SELECT LAST_VALUE(...)` 虽然返回更精确的值，但固定 `Time=0`；配合
  `MAX_TIME(...)` 会得到两列而不是 Native 的单值列，不能直接替代；
- Parser 仍显式拒绝 `SELECT LAST`（`LAST_SYNTAX`）；LAST 专用适用规则、
  候选构造、Validator plan-effect 处理和结果归一化都未进入生产代码；
- `LIMIT 1 OFFSET 0` 语义上等价，但 Parser 仍按 `OFFSET` 拒绝；不能为了方便
  而放宽到未验证子集。

## 8. 结论与决策

**`CAND_LAST_SINGLE_SERIES` 保持 `NOT_VERIFIED`。**

- 不能在 Registry 中启用；
- 不能把本报告约 11.7%–42.6% 的受控实验收益作为生产收益或阶段 0 通过证据；
- 授权范围内的边缘数据已通过，但不能用“边缘数据通过”替代真实需求和
  生产 Executor 证明；
- 本次授权已完成；任何重跑仍必须在独立测试库中进行并确认清理，不能写入共享
  benchmark database；
- 下一步应先获得代表性 `ORDER BY time DESC LIMIT 1` workload，再实现并测试
  结果归一化、LAST 专用 Parser/Validator 规则和生产 Executor 回退，最后才
  讨论状态升级；TTL/重启/并发/NaN 等残余类别仍需单独实验。

## 9. 证据文件与复现

仓库内摘要（无凭据）：

- [`tests/fixtures/last/last_certification_summary.json`](../tests/fixtures/last/last_certification_summary.json)
- [`tests/fixtures/last/last_edge_certification_summary.json`](../tests/fixtures/last/last_edge_certification_summary.json)
- [`tests/fixtures/last/last_readonly_recheck_20261101_summary.json`](../tests/fixtures/last/last_readonly_recheck_20261101_summary.json)
  —— 全量需求扫描、变体边界、结果契约、120 块扩展计时和回退契约的脱敏摘要

本轮原始脚本和完整 JSON 位于仓库外的临时工作目录，**未提交**：

```text
last_certify.py
last_certify_result.json
last_sweep.py
last_sweep_result.json
last_fallback_contract.py
last_fallback_contract_result.json
last_discovery.py
last_discovery_result.json
last_edge_ddl_probe.py
last_edge_ddl_probe_result.json
last_edge_certify.py
last_edge_certify_result.json
last_benchmark_120.py
last_benchmark_120_result.json
last_benchmark_replication_result.json
last_variant_probe.py
last_variant_probe_result.json
last_shape_probe.py
last_shape_probe_result.json
build_last_recheck_summary.py
```

本轮执行命令（临时目录，可能随 job 清理而消失）：

```bash
cd /home/xysong/.claude/jobs/205df8f3/tmp
./venv206/bin/python last_certify.py
./venv206/bin/python last_sweep.py
./venv206/bin/python last_fallback_contract.py
./venv206/bin/python last_edge_certify.py
./venv206/bin/python last_benchmark_120.py
./venv206/bin/python last_variant_probe.py
./venv206/bin/python last_shape_probe.py
```

> **历史路径说明：** 上面的 `venv206/` 是 2026-11-01 LAST 认证时的临时工作
> 目录，不是仓库路径，可能随 job 清理而消失。当前阶段 4 的可复现 2.0.6 环境由
> `scripts/setup_iotdb206_env.sh` 创建（默认 `.venv-iotdb206/`，自检
> `apache-iotdb==2.0.6`）；本节命令仅作为历史证据记录，不用于新的验收运行。

边缘认证脚本在 `finally` 中执行 `DROP DATABASE root.last_cert_20261101` 并验证
目标库消失；原始结果仍保留在仓库外。连接信息不写入结果文件；这些脚本不是项目
依赖，也不是生产代码。仓库内摘要由完整结果生成，保留六类型归一化值、边界、
负例、EXPLAIN、性能汇总、回退契约、全量需求扫描和 79 个边缘 case，省略了
逐样本 timing 明细。

## 10. 相关文档

- [`STRATEGY_CATALOG.md`](STRATEGY_CATALOG.md)：策略总表与阻塞结论
- [`DB_CAPABILITY_AUDIT.md`](DB_CAPABILITY_AUDIT.md)：LAST/EXPLAIN 能力审计
- [`IMPLEMENTATION_STATUS.md`](IMPLEMENTATION_STATUS.md)：阶段状态与下一步
- [`tests/fixtures/last/README.md`](../tests/fixtures/last/README.md)：摘要 fixture 说明
