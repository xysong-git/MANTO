# Agent 1：SQL Parser 与查询特征

## 1. 范围与结论

阶段 2 只实现 **Agent 1（Query Analysis）** 和查询特征 schema。它把一条 SQL
解析为：

- `ParseResult`：解析状态、失败原因、`query_hash`、`normalized_template`、
  `template_hash`、parser 版本和 unsupported constructs；
- `QueryFeatures`：时间条件、值过滤、设备/序列、聚合、GROUP BY、ORDER BY、
  LIMIT 等特征，每个特征带 `source`，缺失值带 `missing_reason`。

本轮**没有**：

- 调用 LLM；
- 生成或改写 SQL；
- 访问 IoTDB 执行查询、获取统计信息或估计行数；
- 把 Parser 接入 `query` CLI、策略 Registry 或 Executor；
- 实现 Agent 2–4、Strategy Registry、Validator、学习模块；
- 进入阶段 3。

阶段 0 的研究阻塞
`BLOCKED: NO_VERIFIED_NON_NATIVE_STRATEGY` 在阶段 2 时尚未解除；后续阶段 4
已在固定 epoch 契约下认证 `S1_REPEAT_CACHE` 并解除该阻塞。Parser 能识别一条
SQL 是 Native 可执行语法，不等于它验证了任何非 Native 优化策略；本文档的
`SUPPORTED`/`UNSUPPORTED` 仍只描述解析兼容范围，不授予策略认证。

## 2. Parser 选择与 IoTDB 兼容边界

### 2.1 候选解析器

- 使用 **SQLGlot 30.18.0**，在 `pyproject.toml` 中精确锁定。
- SQLGlot **没有 `iotdb` dialect**；本项目使用其默认 dialect 作为词法/语法
  候选解析器，不能宣称它完整支持 IoTDB SQL。
- 真正的兼容范围来自阶段 0 的 47 条真实 IoTDB 2.0.6 workload SQL 和 24 条
  负例，不来自 SQLGlot 文档。
- SQLGlot 解析成功**不等于** IoTDB server 接受，也不等于语义等价。所有
  `SUPPORTED` 只表示“落在本文件声明的子集内”。

### 2.2 真实 fixture 上的兼容性证据

47 条 workload fixture 的原始 SQLGlot 行为：

| 行为 | 数量 | 例子 |
|---|---:|---|
| 原始 parse 成功 | 38 | Native 投影、COUNT、值过滤、ORDER BY、LIMIT |
| 原始 `ParseError` | 9 | 8 条 `GROUP BY ([start,end),interval)`；1 条 `root.dbm.g_0.*` 通配路径 |
| 解析成功但语义错误 | 4 | 裸 `LAST` 被解析成 `Alias(Column("last"), series)`，不是 IoTDB LAST 函数 |

加上严格 AST 白名单后，最终结果：

```text
34 SUPPORTED
13 UNSUPPORTED
```

13 条 unsupported workload：

```text
db800_q7, db800_q8, db800_q11
dbs_q7,   dbs_q8,   dbs_q11
dbl_q7,   dbl_q8,   dbl_q11
dbm_q7,   dbm_q8,   dbm_q11
dbm_q12_union
```

其中：

- `*_q7`、`*_q11`：SQLGlot `ParseError`；tokenizer 诊断 hint 为 `GROUP_BY`；
- `*_q8`：裸 `LAST` 被 SQLGlot 静默解析为 alias；本适配器专门识别为
  `LAST_SYNTAX`；
- `dbm_q12_union`：`root.dbm.g_0.*` 通配路径导致 `ParseError`；诊断 hint 为
  `WILDCARD_PATH`。

24 条负例中，SQLGlot 原始接受 17 条；严格白名单使本适配器只接受 4 条
（`wrong_device_path`、`count_star_semantics`、`select_star_result_size`、
`union_to_single_range_not_equivalent`），其余 20 条为 `UNSUPPORTED`。这 4 条
的语义限制见第 8 节。

### 2.3 已知 SQLGlot 差异

| SQL 构造 | SQLGlot 30.18.0 行为 | 本适配器处理 |
|---|---|---|
| `GROUP BY ([start,end),interval)` | `ParseError` | `UNSUPPORTED`，`PARSER_CANNOT_PARSE`，附加 tokenizer hint `GROUP_BY` |
| `root.db.g_0.*` | `ParseError`（缺 Dot expression） | `UNSUPPORTED`，附加 `WILDCARD_PATH` |
| `ALIGN BY DEVICE` | `ParseError` | `UNSUPPORTED`，附加 `ALIGN_BY_DEVICE` |
| `SELECT LAST s` | 解析为 `Alias(Column("last"), Identifier("s"))` | 专门检测为 `LAST_SYNTAX`，绝不当作投影 |
| `SELECT LAST_VALUE(s)` | 解析为 `LastValue` | 支持；它是普通函数，不是裸 `LAST` |
| `SELECT MAX_TIME(s)` | 解析为 `Anonymous("MAX_TIME")` | 只接受 `MAX_TIME` 这一种 anonymous 函数 |
| `LIMIT 10 OFFSET 5` | offset 放在 `Select.args["offset"]` | 拒绝 `OFFSET` |
| `ORDER BY time ASC` | `nulls_first=True`（默认值） | 接受；显式 `NULLS FIRST/LAST` 由 token 层拒绝 |
| `SELECT DISTINCT` | `Distinct` 节点 | 拒绝 |
| `SHOW`/`EXPLAIN` | 退化为 `Command` 并打印 warning | 返回 `NOT_A_SELECT` |

tokenizer hint 只用于让失败报告更可诊断；它不会把 `ParseError` 转成
`SUPPORTED`。安全性来自 AST 白名单和结构校验，不来自 hint。

## 3. 解析流水线

`SQLParserAdapter.parse()`：

1. 计算 `query_hash = SHA-256(raw SQL UTF-8)`；
2. 拒绝空白输入；
3. 用 `sqlglot.parse()` 取得**语句列表**，不是 `parse_one()`；
4. 语句数必须恰为 1；
5. 根节点必须是 `exp.Select`；set operation、`Command`、DDL 等拒绝；
6. 对根节点及所有后代节点做类型白名单；
7. 对每个已知节点做参数键白名单；
8. 检查 SELECT/FROM/WHERE/ORDER BY/LIMIT 的结构；
9. 构造特征、规范化 template 和 `template_hash`；
10. 任何未知节点、未知函数、未知路径或未知子句都 fail-closed。

`SQLParserAdapter.extract_features()`：

- 非 `SUCCESS` 的 `ParseResult` 返回全 `None` 特征；
- `SUCCESS` 时重新解析同一 SQL，避免信任可能被外部修改的 AST；
- 重解析结果与原 `normalized_template` 不一致时抛 `ParserInternalError`；
- Phase 2 不接受 `metadata`；传入非 `None` 会显式抛
  `NotImplementedError`，不会被静默忽略。

Agent 1 包装：

```python
from tsdb_optimizer.agents import QueryAnalysisAgent

result = QueryAnalysisAgent().analyze(
    "SELECT s_130 FROM root.test.g_0.d_0 WHERE time >= 1 AND time < 2 LIMIT 10"
)
print(result.model_dump_json(indent=2))
```

## 4. 特征字典

### 4.1 顶层标识与模板

| 字段 | 类型 | 语义 | 来源/缺失规则 |
|---|---|---|---|
| `query_id` | `str` | 调用方提供；未提供时生成 UUID hex | 调用方 |
| `query_hash` | 64 位 hex `str` | `SHA-256(raw SQL UTF-8)`；原始文本、注释和空白都会影响它 | 总是可计算 |
| `template_hash` | 64 位 hex `str \| None` | `SHA-256(normalized_template)`；仅 `SUCCESS` 有值 | `SUCCESS` 必有 |
| `normalized_template` | `str \| None` | 由已验证 AST 重建的规范化、**不可执行** SQL-like 字符串；字面量替换为 `?` | 仅 `SUCCESS` |
| `query_types` | `list[QueryType]` | 多标签结构分类，按固定顺序去重 | `SUCCESS` 非空；失败为空 |
| `warnings` | `list[str]` | 语义或结果形状警告；不是错误 | 可为空 |

`normalized_template` 的规则：

- 保留路径和裸序列名；
- 聚合函数名大写；
- 时间/值过滤字面量变成 `?`；
- `LIMIT <n>` 变成 `LIMIT ?`；
- `ORDER BY time` 补成 `ORDER BY time ASC`；
- 不输出 GROUP BY（因为 Phase 2 不支持）；
- 它是结构模板，**不是可执行 SQL**，不得直接执行。

`query_hash` 是“原始查询文本”的哈希；`template_hash` 是“规范化结构”的哈希。
同一模板不同字面量会得到不同 `query_hash`、相同 `template_hash`。

### 4.2 `Feature[T]` 通用契约

每个特征字段都是：

```text
value: T | None
source: SQL_AST | SERVER_METADATA | CATALOG | NOT_AVAILABLE
missing_reason: <reason> | None
```

不变量：

- `value is None` ⇒ `missing_reason` 必填；
- `value is not None` ⇒ `missing_reason is None`，且 `source != NOT_AVAILABLE`；
- Phase 2 的 `source` 只会是 `SQL_AST`（从 SQL 结构得到）或
  `NOT_AVAILABLE`（需要但尚未获取）；
- `SERVER_METADATA` 和 `CATALOG` 是预留来源，本轮不产生。

`QueryFeatures.feature_sources` 是计算字段，返回每个特征名到 `source` 值的
字典；`missing_features` 返回所有 `value is None` 的特征名。两者都进入 JSON。

### 4.3 时间条件

| 字段 | 类型 | 语义 |
|---|---|---|
| `time_predicates` | `list[TimePredicate]` | 每个时间比较的 `operator` 和整数 `value_ms`，按 SQL 中出现顺序 |
| `time_range` | `TimeRange` | 从时间谓词推导的静态边界；只来自 SQL，不来自统计 |
| `time_range_ms` | `int` | 结束边界减开始边界；闭/开区间语义见下 |

`TimeRange`：

```text
start_ms: int | None
end_ms: int | None
start_inclusive: bool | None
end_inclusive: bool | None
```

推导规则：

| SQL 形态 | `time_range` | `time_range_ms` |
|---|---|---|
| 无时间谓词 | `None`，`NOT_PRESENT_IN_SQL` | `None`，`NOT_PRESENT_IN_SQL` |
| 恰好一个 `time = v` | `[v, v]`，两端 inclusive | `0` |
| 一个下界 + 一个上界 | 对应边界与开闭性 | `end - start`（合法时） |
| 只有下界或只有上界 | 该边界 | `None`，`INCOMPLETE_TIME_RANGE` |
| 多个同类边界或 `=` 混其他谓词 | `None`，`AMBIGUOUS_TIME_RANGE` | `None`，`AMBIGUOUS_TIME_RANGE` |
| `end < start` | 仍保留静态边界 | `None`，`INVALID_TIME_RANGE` |
| `end == start` 且任一端为开区间 | 仍保留静态边界 | `None`，`INVALID_TIME_RANGE` |

`end == start` 且两端都 inclusive 时是合法的时间点，`time_range_ms = 0`。
`time_range_ms` 为 `None` 时，查询不会被标记 `TIME_RANGE`（仍可能标记
`TIME_FILTER`）。

时间边界不检查 IoTDB 时间戳范围、时区或 TTL，也不证明服务端会返回数据。

### 4.4 值过滤

| 字段 | 类型 | 语义 |
|---|---|---|
| `value_filters` | `list[SeriesFilter]` | 完全限定的五段序列路径过滤 |
| `filter_count` | `int` | `time_predicates` 数量 + `value_filters` 数量 |

`SeriesFilter`：

```text
path: "root.<db>.<group>.<device>.<series>"
device: "root.<db>.<group>.<device>"
series: "<series>"
operator: = | > | >= | < | <=
value: int
```

- 只接受五段、以 `root` 开头的路径；
- 裸序列名（如 `s_130 > 0`）拒绝，因为 IoTDB 可能跨设备解释，无法安全归属；
- 字符串、浮点、布尔、NULL、算术表达式、`BETWEEN`、`IN`、`LIKE`、`IS`、
  `OR`、`NOT`、括号均拒绝；
- 不检查序列是否存在、类型是否数值、值是否超出类型范围。

### 4.5 设备与序列

| 字段 | 类型 | 语义 |
|---|---|---|
| `devices` | `list[DeviceRef]` | FROM 中的设备路径，含 `parts` |
| `device_count` | `int` | 设备语法数量 |
| `device_paths_verified` | `bool` | Phase 2 **恒为 `None`**，`NEEDS_METADATA` |
| `selected_series` | `list[SeriesSelector] \| None` | SELECT 中显式选择的序列；无法安全展开时为 `None` |
| `series_count` | `int \| None` | 显式选择的序列表达式数量；无法安全展开时为 `None` |

`DeviceRef`：

```text
path: "root.<level>.<device>" 或 "root.<db>.<group>.<device>"
parts: list[str]，长度 3 或 4
```

- 只做语法检查，**不查 catalog**；
- `root.test.d_0`（三段）语法上接受，但它可能是数据库层级而不是真实设备；
  因此 `device_paths_verified=None` 并带 warning；
- 存在性必须在后续阶段用 `SHOW TIMESERIES`/metadata 验证；
- 空结果不是路径有效的证据。

`SeriesSelector`：

```text
name: str
qualified_path: str | None
device: str | None
```

- 裸名 `s_130`：由 IoTDB 在 FROM 的每个设备上解释；Parser 不展开；
- 五段限定路径：带 `qualified_path` 和 `device`；
- `SELECT *`：`selected_series=None`，`series_count=None`，因为展开宽度需要
  metadata；
- `COUNT(*)`：`selected_series=None`，`series_count=None`，因为 IoTDB 会按
  series 展开；不是单行计数。

### 4.6 聚合

| 字段 | 类型 | 语义 |
|---|---|---|
| `aggregations` | `list[AggregationSpec]` | 每个聚合投影的函数、序列、是否 star |
| `aggregation_types` | `list[str]` | 去重后的函数名，按首次出现顺序 |

`AggregationSpec`：

```text
function: "COUNT" | "LAST_VALUE" | "MAX_TIME"
series: SeriesSelector | None
is_star: bool
```

- 仅支持 `COUNT(series)`、`COUNT(*)`、`LAST_VALUE(series)`、
  `MAX_TIME(series)`；
- `AVG`、`SUM`、`MIN`、`MAX`、`MAVG`、`TIME_FLOOR` 等不在子集；
- `LAST_VALUE` 是函数；裸 `LAST` 是另一套语法且被拒绝；
- `COUNT(*)` 只允许 `is_star=True` 且没有 series；
- 非 star 聚合必须有 series；
- 聚合的语义、结果形状和 LAST 认证状态不由 Parser 改变。

### 4.7 GROUP BY

| 字段 | 类型 | 语义 |
|---|---|---|
| `group_by` | `GroupBySpec \| None` | 预留结构；Phase 2 支持查询中恒为 `None` |
| `has_group_by` | `bool` | 支持查询中恒为 `False` |

Phase 2 **不支持任何 GROUP BY**：

- IoTDB interval 语法（`GROUP BY ([start,end),interval)`）SQLGlot 直接
  `ParseError`；
- `GROUP BY TIME(...)` 同样是 `ParseError`；
- 其他 `GROUP BY` 被 `_check_select_args` 拒绝。

`GroupBySpec` 和 `QueryType.GROUP_BY` 仅为 schema 兼容和未来阶段预留；当前
不会产生 `GROUP_BY` 类型。**不得把 `GroupBySpec` 的存在理解为 interval 语义
已被实现。**

### 4.8 ORDER BY 与 LIMIT

| 字段 | 类型 | 语义 |
|---|---|---|
| `order_by` | `list[OrderBySpec]` | 只允许单个 `time` 列 |
| `has_order_by` | `bool` | 语法上是否有 ORDER BY |
| `limit` | `int` | 非负整数 LIMIT 字面量 |
| `has_limit` | `bool` | 语法上是否有 LIMIT |

`OrderBySpec.column` 恒为 `"time"`，`direction` 为 `"ASC"` 或 `"DESC"`。

- 不接受 `ORDER BY` 其他列、多个排序键、表达式、`WITH FILL`；
- 显式 `NULLS FIRST/LAST` 通过 token 序列检测并拒绝；SQLGlot 的默认
  `nulls_first` 值不作为用户语法；
- 不接受 `OFFSET`；
- `LIMIT -1`、`LIMIT 1.5`、`LIMIT 1, 2` 拒绝；
- `LIMIT 0` 是合法整数，保留；
- LIMIT 不影响 `estimated_rows`；Phase 2 不估计行数。

### 4.9 估计行数

`estimated_rows` 在 Phase 2 **恒为 `None`**：

```text
value = None
source = NOT_AVAILABLE
missing_reason = NEEDS_STATISTICS
```

Parser 不从 LIMIT、时间范围、设备数量或 fixture 行数推断行数。阶段 0 的
实测行数不是估计器输入，也没有被硬编码。

## 5. Query Type 多标签

标签按以下固定顺序产生：

| 标签 | 条件 |
|---|---|
| `PROJECTION` | 每个成功查询都有 |
| `AGGREGATION` | 至少一个支持聚合 |
| `TIME_FILTER` | 至少一个时间谓词 |
| `TIME_POINT` | 恰好一个时间谓词且为 `=` |
| `TIME_RANGE` | 有合法双边界，且 `time_range_ms` 可计算 |
| `VALUE_FILTER` | 至少一个值过滤 |
| `MULTI_DEVICE` | FROM 中设备数 > 1 |
| `MULTI_SERIES` | 显式序列表达式数 > 1 |
| `GROUP_BY` | Phase 2 不产生 |
| `ORDER_BY` | 有 ORDER BY |
| `DESCENDING_ORDER` | 任一排序方向为 DESC（Phase 2 只有一个） |
| `LIMIT` | 有 LIMIT |
| `COUNT_ALL` | 有 `COUNT(*)` |
| `WILDCARD_PROJECTION` | 投影为 `SELECT *` |

时间点查询不会同时得到 `TIME_RANGE`；只有下界或只有上界的查询只有
`TIME_FILTER`。

## 6. Source 与 Missing Reason

### 6.1 Source

| 值 | 含义 | Phase 2 |
|---|---|---|
| `SQL_AST` | 从已校验的 SQL 结构直接得到 | 使用 |
| `SERVER_METADATA` | 需要 server 元数据探测 | 预留，不使用 |
| `CATALOG` | 需要 catalog/schema 查询 | 预留，不使用 |
| `NOT_AVAILABLE` | 当前无法获得 | 用于 `estimated_rows`、`device_paths_verified`、失败解析的全部特征 |

### 6.2 Missing Reason

| 值 | 含义 |
|---|---|
| `NOT_PRESENT_IN_SQL` | 该子句在 SQL 中不存在 |
| `INCOMPLETE_TIME_RANGE` | 只有单侧时间边界 |
| `INVALID_TIME_RANGE` | 边界矛盾或空开区间 |
| `AMBIGUOUS_TIME_RANGE` | 多个边界/等值条件，无法静态合并 |
| `UNSUPPORTED_SYNTAX` | SQL 不在支持子集，特征不提取 |
| `NEEDS_METADATA` | 需要 catalog/设备/序列元数据 |
| `NEEDS_STATISTICS` | 需要统计信息；Phase 2 不猜测 |
| `PARSER_LIMITATION` | Parser 内部/重解析限制 |
| `NOT_APPLICABLE` | 预留 |

## 7. 支持的语法子集

```text
SELECT <projection> [, <projection> ...]
FROM <device_path> [, <device_path> ...]
[WHERE <predicate> [AND <predicate> ...]]
[ORDER BY time [ASC | DESC]]
[LIMIT <non-negative-integer>]
```

只允许**一条** `SELECT` 语句，不允许 CTE、UNION、子查询、显式 JOIN、表别名。

### 7.1 FROM

- `root.<level>.<device>`（3 段）；
- `root.<db>.<group>.<device>`（4 段）；
- 多个设备用逗号分隔；
- 不验证路径存在性；
- 不接受完整序列路径、通配路径、表别名、显式 JOIN、子查询。

### 7.2 投影

- 裸序列名（不能是 `time`）；
- 五段限定序列路径；
- `COUNT(series)`、`COUNT(*)`、`MAX_TIME(series)`、`LAST_VALUE(series)`；
- 单独的 `SELECT *`。

不接受：投影别名、`DISTINCT`、算术、CASE、窗口函数、其他函数、`time` 列
投影、`SELECT *` 与其他投影混用。

### 7.3 WHERE

- 仅 `AND` 连接；
- 时间：`time <op> <integer>`，`op ∈ {=, >, >=, <, <=}`；
- 值过滤：`root.<db>.<group>.<device>.<series> <op> <integer>`；
- 负整数允许；整数按 Python `int` 保存，不做类型宽度截断。

不接受：`OR`、`NOT`、括号、`!=`/`<>`、`BETWEEN`、`IN`、`LIKE`、`IS`、
字符串、浮点、布尔、NULL、算术、裸序列名过滤、函数调用。

### 7.4 ORDER BY / LIMIT

- 单个 `time` 列；
- `ASC` 或 `DESC`；缺省规范化为 `ASC`；
- 不接受 `NULLS FIRST/LAST`、`WITH FILL`、多排序键、非 time 列；
- `LIMIT` 只接受非负整数字面量；
- 不接受 `OFFSET`。

### 7.5 明确拒绝的子句

`GROUP BY`、`HAVING`、`ALIGN BY DEVICE`、`DISTINCT`、`OFFSET`、CTE、集合
运算、子查询、窗口函数、投影/表别名、显式 JOIN、其他函数。

## 8. Unsupported Constructs

| 枚举值 | 典型触发 | 结果 |
|---|---|---|
| `EMPTY_INPUT` | 空白 SQL | `UNSUPPORTED` / `EMPTY_INPUT` |
| `MULTIPLE_STATEMENTS` | 两条 SELECT 或分号分隔多语句 | `UNSUPPORTED` / `MULTIPLE_STATEMENTS` |
| `NOT_A_SELECT` | DDL/DML/SHOW/EXPLAIN/Command | `UNSUPPORTED` / `NOT_SELECT` |
| `SET_OPERATION` | UNION/INTERSECT/EXCEPT | `UNSUPPORTED` / `SET_OPERATION` |
| `MISSING_FROM` | `SELECT 1` | `UNSUPPORTED` / `UNSUPPORTED_SYNTAX` |
| `SUBQUERY` | FROM/投影中的子查询 | `UNSUPPORTED` / `UNSUPPORTED_SYNTAX` |
| `EXPLICIT_JOIN` | `JOIN ... ON ...` | `UNSUPPORTED` / `UNSUPPORTED_SYNTAX` |
| `TABLE_ALIAS` | `FROM ... AS x` | 同上 |
| `SELECT_ALIAS` | `SELECT s AS x` | 同上 |
| `LAST_SYNTAX` | 裸 `SELECT LAST s` | 同上 |
| `DISTINCT` | `SELECT DISTINCT` | 同上 |
| `GROUP_BY` | 任意 GROUP BY | 同上；interval 形式为 `PARSER_CANNOT_PARSE` + hint |
| `HAVING` | HAVING | 同上 |
| `ORDER_BY` | 非 time/多键/NULLS/WITH FILL | 同上 |
| `LIMIT` | 非整数/负 LIMIT 等 | 同上 |
| `OFFSET` | OFFSET | 同上 |
| `UNKNOWN_SELECT_ITEM` | `time` 投影、星号混用、表达式 | 同上 |
| `UNKNOWN_FUNCTION` | MAVG、TIME_FLOOR、LAST()、其他函数 | 同上 |
| `UNKNOWN_WHERE_EXPRESSION` | OR、NOT、BETWEEN、IN、LIKE、IS、NEQ、裸序列过滤 | 同上 |
| `UNKNOWN_AST_NODE` | 白名单外的 AST 节点/参数 | `UNSUPPORTED` / `UNKNOWN_AST_NODE` |
| `INVALID_PATH` | 非 root、段数错误、FROM 为完整序列路径 | `UNSUPPORTED` / `UNSUPPORTED_SYNTAX` |
| `WILDCARD_PATH` | `root.db.g_0.*` | `PARSER_CANNOT_PARSE` + hint |
| `QUOTED_IDENTIFIER` | 双引号/反引号标识符 | `UNSUPPORTED` / `UNSUPPORTED_SYNTAX` |
| `UNSUPPORTED_LITERAL` | 字符串、浮点、非整数字面量 | 同上 |
| `UNSUPPORTED_CLAUSE` | CTE、QUALIFY、TABLESAMPLE、未知参数键等 | 同上 |
| `ALIGN_BY_DEVICE` | `ALIGN BY DEVICE` | `PARSER_CANNOT_PARSE` + hint |
| `SQLGLOT_PARSE_ERROR` | SQLGlot `ParseError` | `PARSER_CANNOT_PARSE` |
| `PARSER_INTERNAL_ERROR` | Parser/分析器内部异常 | `ERROR` / `INTERNAL_PARSER_ERROR` |

失败时：

- `normalized_template = None`；
- `template_hash = None`；
- `features.query_types = []`；
- 所有特征值 `None`；
- 每个特征有 reason：解析失败用 `UNSUPPORTED_SYNTAX` 或
  `PARSER_LIMITATION`；`estimated_rows` 保持 `NEEDS_STATISTICS`，
  `device_paths_verified` 保持 `NEEDS_METADATA`；
- 不生成候选 SQL，不自动改写。

内部异常映射为 `ERROR` / `INTERNAL_PARSER_ERROR`，不会伪装成 `SUCCESS`。

## 9. 测试证据

阶段 2 只使用仓库内 fixture，不连接 IoTDB。实际执行：

```bash
cd /opt/program-code/MANTO
python -m pytest -q tests/unit/test_query_features_schema.py \
  tests/unit/test_sql_parser.py tests/test_phase2_sql_features.py
```

结果：

```text
146 passed in 0.62s
```

完整测试：

```bash
python -m pytest -q
```

结果：

```text
206 passed, 2 skipped in 0.83s
```

`2 skipped` 是需要显式启用的真实 IoTDB 集成测试，不是本次 Parser 测试。
测试内容：

- 47 条 workload fixture 参数化测试；断言实际为 34 `SUCCESS` / 13
  `UNSUPPORTED`；
- 24 条 negative fixture 参数化测试；逐条断言状态和关键 construct；
- 12 条 `tests/fixtures/plans/index.json` 中的真实 SELECT（去掉 EXPLAIN
  前缀）测试；
- 特征 schema 不变量、JSON 序列化、hash/template 确定性、缺失原因；
- 未知 AST 节点、内部异常、重解析不一致的 fail-closed 测试；
- 普通单元测试与阶段 0/1 回归测试。

测试断言是硬编码的预期，不是从被测 Parser 输出反推的。

## 10. 已知限制

1. **SQLGlot 没有 IoTDB dialect。** 支持范围只覆盖 47 条 fixture 验证过的
   子集；其他合法 IoTDB SQL 可能被拒绝，这是有意的 fail-closed。
2. **不验证 catalog。** `wrong_device_path` 在语法上 `SUCCESS`，但
   `device_paths_verified=None`。非空/空结果都不能作为路径有效证据。
3. **不估计行数。** `estimated_rows` 恒 `None`，不读统计、不猜。
4. **不支持 GROUP BY。** 所有 interval/GROUP BY 形式均为 `UNSUPPORTED`；
   `GroupBySpec` 只是预留结构。
5. **不支持裸 LAST。** 裸 `LAST` 会被 SQLGlot 静默误解析，本适配器专门拒绝；
   `LAST_VALUE()` 是另一条语法，支持它不等于 LAST 策略已认证。
6. **不展开 `SELECT *` / `COUNT(*)`。** 需要 metadata；当前标记
   `NEEDS_METADATA`。
7. **裸序列名不展开。** IoTDB 可能在 FROM 的每个设备上解释裸名；Parser 只
   记录名字，不声称唯一归属。
8. **时间范围只来自 SQL。** 不做时区、TTL、统计或 server 语义推断；
   模糊/不完整/非法边界显式标记。
9. **`normalized_template` 不可执行。** 它只用于结构比较；字面量已替换为
   `?`，不能直接送去执行。
10. **未接入执行路径。** `query` CLI、Adapter、Registry、Executor 仍执行
    用户原始 Native SQL；Phase 2 不改写、不双执行、不选策略。
11. **Parser 版本锁定。** SQLGlot 升级可能改变 AST 形状；升级必须重新跑
    fixture 并更新本文件的兼容性证据。
12. **测试不等于 server 验收。** 34 条 `SUPPORTED` 来自阶段 0 真实 SQL 的
    语法子集，不代表任意新 SQL 会被 IoTDB 接受；正式执行前仍需 server
    校验/错误处理。

## 11. 下一步边界

阶段 2 已实现 Agent 1 和 schema，但**不进入阶段 3**。后续如果用户明确要求：

- 阶段 3 才实现 Optimization Decision Agent 和策略选择；
- 所有策略仍必须进 Registry、经 Validator；
- Native 仍是 S0 基线；
- 在阶段 0 研究阻塞解除前，不得启用任何 `CAND_*`；
- 任何改写都必须有独立等价性测试，不能由 Parser 自动重写。
