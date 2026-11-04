# SQL Fixtures（阶段 0）

本目录保存阶段 0 在真实 IoTDB 实例上探测得到的 SQL 样本、执行结果摘要和负例。
这些文件是后续 Parser、Validator、Strategy Registry 和语义回归测试的输入，
不是 benchmark 结果，也不代表生产负载。

## 环境与去敏

| 项目 | 值 |
|---|---|
| IoTDB server | 2.0.6，build `c6ecbde` |
| 探测客户端 | `apache-iotdb` 2.0.10；关键行为用 2.0.6 客户端复验 |
| 方言 | tree model（`iotdb.Session`） |
| 连接 | 本机 `127.0.0.1:6667` |
| 认证 | 本地测试账号；**仓库内不保存用户名、密码或连接串** |

探测脚本位于仓库外的临时工作目录，且包含本地测试凭据，因此没有提交。
仓库只保留去敏后的 SQL、结果摘要和错误文本。

## 文件

```text
sql/
├── workload_cases.json      # 47 条可被 server 接受的树模型 workload
├── negative_cases.json      # 24 条负例/边界/语义警告
└── workload/
    └── <case_id>.sql        # 与 workload_cases.json 中 sql 字段对应的纯 SQL
```

`workload/*.sql` 与 JSON 中的 `sql` 字段按 `strip()` 后比较应完全一致。
单个 SQL 文件便于 Parser 测试按 case 读取，也便于人工审阅。

## `workload_cases.json`

顶层字段：

| 字段 | 含义 |
|---|---|
| `fixture_version` | fixture schema 版本 |
| `generated_at` | 生成日期 |
| `server_version` / `server_build_info` | 被探测 server 版本与 build |
| `client_version_used_for_probe` | 初次探测客户端版本 |
| `client_version_reverified` | 关键能力复验客户端版本 |
| `dialect` | `tree` |
| `authentication` | 去敏说明，不含凭据 |
| `dataset_snapshot` | 探测时数据快照的设备/序列/点数/时间范围 |
| `cases` | 47 条 workload |

单条 case 的主要字段：

| 字段 | 含义 |
|---|---|
| `case_id` | 唯一 ID，也是 `workload/<case_id>.sql` 的文件名 |
| `source_file` | 来源 workload 文件（相对本地 MAPO/MATAOF 工作区） |
| `database` | 目标数据库名 |
| `sql` | 实际发送给 IoTDB 的 SQL |
| `dialect` | `tree` |
| `classification` | 见下表 |
| `server_acceptance` | 是否被 server 接受（当前均为 `ACCEPTED`） |
| `execution_status` | `SUCCESS` / `SUCCESS_EMPTY` / `SUCCESS_LARGE` |
| `row_count` / `column_count` | 完整读取后的结果规模 |
| `columns` | 结果列名（保留 server 返回的原始命名） |
| `result_digest_sha256` | 对完整结果行做规范化序列化后的摘要 |
| `rows_preview` | 小结果的前几行；大结果可能为空或截断 |
| `elapsed_ms` | 单次客户端端到端观测，**不是**基准统计 |
| `explain_status` | `AVAILABLE` / `EMPTY_PLAN` |
| `explain_line_count` | EXPLAIN 文本行数 |
| `notes` | 观测到的特殊行为 |

`classification` 取值：

| 值 | 含义 |
|---|---|
| `SUPPORTED` | server 接受，结果形状与常规 SELECT 一致 |
| `SUPPORTED_WITH_SHAPE_WARNING` | 可执行，但结果形状与 Native 等价写法不同 |
| `SUPPORTED_HEAVY_WILDCARD` | 可执行，但结果宽/行数大，需要成本约束 |
| `SUPPORTED_SILENT_EMPTY_WARNING` | SQL 可执行但路径无数据，空结果不能证明查询有效 |
| `SUPPORTED_CORRECTED_VARIANT` | 已对照原始错误路径修正后的可执行版本 |

当前分布：40 条 `SUPPORTED`、4 条 `SUPPORTED_WITH_SHAPE_WARNING`、
1 条 `SUPPORTED_HEAVY_WILDCARD`、1 条 `SUPPORTED_SILENT_EMPTY_WARNING`、
1 条 `SUPPORTED_CORRECTED_VARIANT`。

### 来源与命名注意

- 46 条来自本地 `MAPO/MAPO/sample_queries_iotdb*/`，1 条来自
  `MATAOF/query/queries_dbm/q12.sql`。
- `dbm_q12_union` 的 case ID 带有历史名称，但其实际 SQL 是 MATAOF 的
  wildcard 查询，**不是** UNION。UNION 的真实负例见
  `negative_cases.json:union_q12`。
- 这些是本地生成的 benchmark 查询，不是用户生产查询，也不是 30–50 条
  真实线上 SQL 的替代品。

## `negative_cases.json`

每条 case 包含：

| 字段 | 含义 |
|---|---|
| `case_id` | 唯一 ID |
| `sql` | 被探测的 SQL；拒绝类语句记录原始文本 |
| `dialect` | `tree` 或 `table` |
| `classification` | 负例类型 |
| `observed` | 实际列、行数、错误类型、错误文本；或“未发送到数据库” |
| `expected_optimizer_behavior` | Optimizer 必须采取的行为 |
| `notes` | 补充说明 |

当前分布：

| classification | 数量 | 说明 |
|---|---:|---|
| `UNSUPPORTED_BY_SERVER` | 12 | server 拒绝，含 UNION、子查询、窗口函数、JSON/VERBOSE EXPLAIN、LAST 值过滤等 |
| `UNKNOWN_SILENT_EMPTY` | 2 | 未知函数被接受但只返回 `Time` 列和 0 行 |
| `INVALID_SILENT_EMPTY` | 2 | 非法 series path / 不存在 device path，静默返回空 |
| `SUPPORTED_WITH_SHAPE_WARNING` | 2 | LAST 多序列形状、`ALIGN BY DEVICE` 形状 |
| `SUPPORTED_WITH_SEMANTIC_WARNING` | 1 | `COUNT(*)` 逐序列展开 |
| `SUPPORTED_WITH_RESULT_SIZE_WARNING` | 1 | `SELECT *` 宽行展开 |
| `SUPPORTED_WITH_PRECISION_WARNING` | 1 | LAST 对 FLOAT 的显示精度与 Native 不同 |
| `SUPPORTED_TABLE_MODEL_ONLY` | 1 | table model 的 `information_schema` 系统表 |
| `REJECTED_BY_OPTIMIZER_POLICY` | 1 | DDL/DML/权限/系统管理语句，未发送 |
| `REWRITE_NOT_EQUIVALENT` | 1 | UNION 合并为单区间不保持语义 |

## 关键警告

1. **空结果不等于成功。** `SUCCESS_EMPTY` 只表示 SQL 执行未抛错。错误路径、
   未知函数、无数据时间范围都可能返回 `Time` 列 + 0 行。Optimizer 必须用
   schema/解析结果判定有效性，不能把 0 行当作策略有效性的证据。
2. **摘要绑定数据快照。** `result_digest_sha256` 和 `rows_preview` 只对探测时的
   数据有效。数据写入、删除或 TTL 变化后，摘要必须重新生成。
3. **`elapsed_ms` 不是性能证据。** 它是单次观测，受缓存、JIT、调度影响；
   不得用于策略收益声明。
4. **`dbm_q12_union` 是命名陷阱。** 其 SQL 为 wildcard，结果可达
   1200 行 × 101 列；不要按 UNION 语义解析。
5. **`test_benchmark_path_original` 是错误路径样本。** 它对不存在的
   `root.test.d_0` 返回空结果和空计划；正确路径是
   `root.test.g_0.d_0`，见 `test_benchmark_path_corrected`。
6. **q08 原始文件包含中文注释。** 该文件在到达子查询前就因注释 token 被拒绝；
   `negative_cases.json` 同时记录了原始文件和去注释版本，两个失败点不能混淆。
7. **通用 SQL Parser 的“解析成功”不等于 IoTDB 接受。** 当前环境中的
   SQLGlot 30.18.0 没有 IoTDB dialect；默认解析 47 条 workload 时 38 条成功、
   9 条失败，并会接受多个被 server 拒绝的负例。详见
   `docs/DB_CAPABILITY_AUDIT.md` §4.4。
8. **LAST 仍不是可执行策略。** 只读认证通过干净单序列数据；另在用户授权
   专用库中完成 79/79 边缘差分（缺失值、重复时间戳、乱序、缺口、空序列、
   SQL NULL），但 TTL/重启/并发/NaN、真实需求和生产回退仍未证；状态仍为
   `NOT_VERIFIED`。详见 `docs/LAST_CERTIFICATION.md`。

## 复现方式

这些 fixture 不包含凭据。复现时通过环境变量提供连接信息，且只执行只读查询：

```bash
export IOTDB_HOST=127.0.0.1
export IOTDB_PORT=6667
export IOTDB_USER=...
export IOTDB_PASSWORD=...
```

Phase 1 的 adapter 将负责从环境/配置读取连接信息、完整消费结果集并在
`finally` 中关闭结果句柄。阶段 0 未提交的探测脚本只用于生成这些 fixture，
不应作为生产代码复用。
