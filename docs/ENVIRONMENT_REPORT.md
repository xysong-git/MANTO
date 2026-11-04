# 环境报告（阶段 0，含阶段 1 与 2026-11-02 环境复核）

**后续默认环境约定（用户指定，2026-11-03）：使用 Conda 环境 `MANTO`。**
命令使用 `conda run -n MANTO python ...`，或先 `conda activate MANTO`。
Python 版本仅作为该环境的实测事实，不将 `3.12.13` 视为环境名或另选解释器的依据。
本轮 `conda run -n MANTO python -c "import sys; print(sys.executable); print(sys.version)"`
返回 `/opt/anaconda3/envs/MANTO/bin/python`、3.12.13。宿主只读探测确认 DataNode PID 3879777，6667 端口可连接；沙箱内 socket 权限失败不等于数据库停机。

- **阶段：** 0 — 仓库、环境与 IoTDB 能力审计；2026-11-01 补充阶段 1 工程环境；
  2026-11-02 复核当前 Conda 环境
- **报告日期：** 初版 2026-11-01；2026-11-02 更新
- **探测性质：** 阶段 0 主体探测为只读；未修改 IoTDB 源码、未修改 server 配置。
  2026-11-01 经用户授权的 LAST 可写边缘测试只发生在专用库
  `root.last_cert_20261101`，目标库随后已删除，见
  [`LAST_CERTIFICATION.md`](LAST_CERTIFICATION.md)。
- **结论：** 默认连接、完整结果读取、EXPLAIN 和 EXPLAIN ANALYZE 均可用；
  当前部署是 **IoTDB 2.0.6 树模型单机双节点（ConfigNode + DataNode）**。
  阶段 1 已在 `pyproject.toml` 锁定 `apache-iotdb==2.0.6`。

**2026-11-02 复核更新：** 用户重新安装后，当前 Conda 环境 `MANTO`
（Python 3.12.13）中的客户端已是 `apache-iotdb 2.0.6`。`doctor` 实测
`ok=true`、`connected=true`、server `2.0.6`（build `c6ecbde`）、model `tree`、
client `2.0.6`。因此当前默认运行环境直接满足版本锁定，不需要额外 venv。
下文中“基础环境是 2.0.10”的表述是阶段 0 的历史记录，仅用于解释当时为何引入
项目本地 venv，不代表 2026-11-02 的当前环境。

## 1. 结论摘要

| 检查项 | 结果 | 证据/说明 |
|---|---|---|
| server 版本 | 2.0.6，build `c6ecbde` | `SHOW VERSION` |
| 部署模式 | 单机；ConfigNode + DataNode 均 Running | `SHOW CLUSTER`、`SHOW CLUSTER DETAILS` |
| 用户数据模型 | **树模型** | 6 个用户 database 均为 `root.<name>.g_0` 设备树 |
| 表模型 | server 支持，但只有 `information_schema`，无用户表 | TableSession 的 `SHOW TABLES` 返回 17 张系统表 |
| Python | 3.12.13（Conda 环境 `MANTO`） | `/opt/anaconda3/envs/MANTO/bin/python` |
| 已安装客户端 | **`apache-iotdb 2.0.6`（当前 Conda `MANTO`，2026-11-02 复核）** | `importlib.metadata.version("apache-iotdb")`；阶段 0 历史值为 2.0.10 |
| 项目锁定客户端 | **`apache-iotdb==2.0.6`** | `pyproject.toml`；当前 Conda 已满足，项目本地 `.venv-iotdb206/` 仅为可选隔离环境 |
| 服务端匹配客户端 | **2.0.6 已实测可用** | 2026-11-02 `doctor`：client 2.0.6、server 2.0.6；连接、SELECT、LAST、EXPLAIN 均通过 |
| 默认连接 | 成功 | 本机 `127.0.0.1:6667`，只读探测 |
| 结果完整读取/关闭 | 成功 | 逐行迭代并在 `finally` 中 `close_operation_handle()` |
| EXPLAIN | 支持文本计划 | 单列 `distribution plan` |
| EXPLAIN ANALYZE | 支持，且走真实执行路径 | 输出运行时算子统计、输出行数、wall time |
| 写操作 | 阶段 0 主体探测未执行 | 无 DDL/DML/权限/系统管理语句发送到 server；2026-11-01 授权的 LAST 可写边缘测试见 §7.2，目标库已删除 |

**版本风险（历史与当前）：** 阶段 0 时基础 Conda 环境安装的客户端是 2.0.10，
而 server 是 2.0.6；当时用 2.0.6 客户端复验关键路径，并在阶段 1 于
`pyproject.toml` 固定 `apache-iotdb==2.0.6`。2026-11-02 用户重新安装后，当前
Conda `MANTO` 已是 2.0.6，`doctor` 实测与 server 匹配，因此该历史版本差异已
消除。若未来环境再次出现 2.0.10，不得用它生成认证证据；使用
`scripts/setup_iotdb206_env.sh` 创建可选隔离 venv 或重新安装 2.0.6。

## 2. 操作系统与 Python 运行时

| 项目 | 值 |
|---|---|
| OS | Ubuntu 24.04.3 LTS (Noble Numbat) |
| Kernel | `7.0.0-38-generic` |
| 架构 | x86_64 |
| glibc | 2.39 |
| CPU | 64 vCPU（`nproc`） |
| 内存 | 125 GiB total；探测时约 36 GiB free + 30 GiB buff/cache |
| Java | OpenJDK 17.0.20.1 |
| Python | 3.12.13，`/opt/anaconda3/envs/MANTO/bin/python` |
| 依赖管理 | 阶段 0 时仅 Conda 环境；阶段 1 起使用 `pyproject.toml`（`apache-iotdb==2.0.6`） |
| 候选 SQL Parser | `sqlglot 30.18.0` 已安装，但仅用于兼容性审计，不是已锁定依赖 |

阶段 0 的探测脚本运行在仓库外的临时工作目录，避免把本地凭据写入仓库。
脚本使用的结果集读取模式为：

```python
ds = session.execute_query_statement(sql)
columns = list(ds.get_column_names())
while ds.has_next():
    row = ds.next()
    values = [row.get_timestamp()] + [
        getattr(field, "value", field) for field in row.get_fields()
    ]
ds.close_operation_handle()
```

注意：`RowRecord` 通过 `get_fields()` 取字段，没有 `fields` 属性；
`TableSession` 在构造函数中完成打开，不能调用不存在的 `.open()`。
这些是阶段 1 adapter 必须按实际版本实现的细节。

## 3. IoTDB 部署

### 3.1 版本与集群

```text
SHOW VERSION
Version  BuildInfo
2.0.6    c6ecbde
```

`SHOW CLUSTER` 观测到：

| NodeID | NodeType | Status | InternalAddress | InternalPort | Version | BuildInfo |
|---:|---|---|---|---:|---|---|
| 0 | ConfigNode | Running | 127.0.0.1 | 10710 | 2.0.6 | c6ecbde |
| 1 | DataNode | Running | 127.0.0.1 | 10730 | 2.0.6 | c6ecbde |

`SHOW CLUSTER DETAILS` 补充：

| 节点 | ConfigConsensusPort | RpcAddress | RpcPort | MppPort | SchemaConsensusPort | DataConsensusPort |
|---|---:|---|---:|---:|---:|---:|
| ConfigNode | 10720 | — | — | — | — | — |
| DataNode | — | 0.0.0.0 | 6667 | 10740 | 10750 | 10760 |

`SHOW CLUSTER` 中的 `InternalAddress` 是 `127.0.0.1`，而 DataNode 的
`RpcAddress` 是 `0.0.0.0:6667`。前者是节点内部注册地址，后者是 RPC
监听地址；不能据此推断该实例可从外部网络安全访问。阶段 0 只从本机连接。

### 3.2 安装路径与数据

| 项目 | 值 |
|---|---|
| 安装目录 | `/opt/program-code/iotdb/distribution/target/apache-iotdb-2.0.6-all-bin/apache-iotdb-2.0.6-all-bin` |
| 数据目录大小 | 约 1.6 GB（`du -sh .../data`） |
| 观测到的进程 | ConfigNode PID 3879519；DataNode PID 3879777（2026-11-01，可能变化） |
| 启动方式 | Java 前台进程；未通过 Docker 探测 |

阶段 0 没有读取或修改 `conf/` 下的 server 配置，也没有执行重启。阶段 1
补充了只读语法探测：`SHOW CONFIG` 和 `SHOW CONFIGURATION` 均被 server 拒绝
（`700 ... no viable alternative at input ...`）；`SHOW VARIABLES` 可用，但只
返回 5 个集群复制/共识变量（`ClusterName`、`DataReplicationFactor`、
`SchemaReplicationFactor` 和两种 Region consensus class）。没有观察到查询级
tuning 参数、修改语法、生效方式或重启/回滚方案，因此 `CAND_DB_PARAMETER`
仍为 `NOT_VERIFIED`（子标记 `UNKNOWN`），不能假设存在可调参数。

### 3.3 资源隔离

客户端和 server 在同一台主机（`127.0.0.1`），因此本阶段的 `client_host`
与 `db_host` 实际相同。即使如此，监控设计仍必须分别记录来源：

- `client_host`：运行 Python 客户端的机器；
- `db_host`：IoTDB 服务所在机器；
- `db_metrics`：IoTDB 自身暴露的指标，当前**未采集，状态 UNKNOWN**。

本阶段没有采集 server CPU/内存/IO，也没有部署 DB 主机监控代理。后续不能把
Python 进程所在主机的 `psutil` 数值直接标成“IoTDB 服务器指标”。
本机同时运行其他进程，且 64 vCPU/125 GiB 的资源条件不代表目标部署环境。

## 4. 数据模型与数据集

### 4.1 树模型

用户数据全部是树模型。`SHOW DATABASES` 返回 7 个 database，其中
`root.__system` 是系统库，6 个是用户数据：

| Database | Devices | Series | Points/series | Min time (ms) | Max time (ms) | 说明 |
|---|---:|---:|---:|---:|---:|---|
| `root.db200.g_0` | 100 | 200,000 | 3,000 | 1640966405000 | 1640981400000 | 200 series/device |
| `root.db800.g_0` | 100 | 800,000 | 3,000 | 1640966405000 | 1640981400000 | 800 series/device |
| `root.dbl.g_0` | 100 | 1,000,000 | 3,000 | 1640966405000 | 1640981400000 | 1,000 series/device |
| `root.dbm.g_0` | 100 | 1,000,000 | 3,000 | 1640966405000 | 1640981400000 | 1,000 series/device |
| `root.dbs.g_0` | 100 | 100,000 | 3,000 | 1640966405000 | 1640981400000 | 100 series/device |
| `root.test.g_0` | 500 | 100,000 | 10,000 | 1640966405000 | 1641016400000 | 200 series/device，时间跨度更长 |

合计约 1,000 个设备、3,200,000 条序列。上表点数/时间范围来自对代表性序列的
`MIN_TIME/MAX_TIME/COUNT` 探测，是**探测快照**，不是全库精确统计；数据写入或
TTL 变化会使快照失效。

`SHOW DATABASES DETAILS` 显示所有用户 database 的 schema/data replication
factor 均为 1，`TimePartitionInterval=604800000` ms（7 天），
`TimePartitionOrigin=0`。这是单机单副本部署，不具备高可用/多副本实验条件。

### 4.2 序列与编码

- 所有用户设备 `IsAligned=true`（`SHOW DEVICES` 的 `IsAligned` 列）。
- `SHOW DEVICES` 全量返回 1,001 个 device：1,000 个用户 device 全部 aligned；
  第 1,001 个是系统 device `root.__system.password_history._root`，其
  `IsAligned=false`。后续统计用户设备时必须排除 `root.__system`。
- 观测到的数据类型：`FLOAT`、`DOUBLE`、`INT32`、`INT64`、`BOOLEAN`、`TEXT`。
- 观测到的编码：`GORILLA`、`TS_2DIFF`、`RLE`、`DICTIONARY`。
- 压缩：`LZ4`。
- 这些是当前数据集的观测结果，不能推断任意 IoTDB 部署都使用相同编码。

### 4.3 表模型

TableSession 可以连接，但当前部署没有用户表：

```text
SHOW TABLES   -- table dialect
TableName           TTL(ms)
columns             INF
config_nodes        INF
configurations      INF
...                 ...
```

共 17 张系统表，位于 `information_schema`。`SHOW TABLES` 在树模型方言中
被拒绝（`700 ... no viable alternative at input 'SHOW TABLES'`），说明树/表
方言不能混用。表模型的 `SELECT 1` 也失败（`701: From clause must not be
empty`），不能把表模型当作通用 SQL 引擎。

**结论：** 当前部署的用户数据只能走树模型 adapter；表模型 adapter 在本环境
没有用户数据可验证，主状态为 `UNSUPPORTED`（子标记 `NO_USER_TABLE`），不是
“未实现”的借口，而是没有可审计的入口。

## 5. 连接、认证与查询路径

阶段 0 使用本机测试账号，凭据不写入仓库、文档或日志。**认证方式：** Python
Native `Session` 的用户名/密码参数；连接本机 `127.0.0.1:6667`，
`use_ssl=False`，未使用 TLS、Kerberos 或其他认证机制。权限模型未审计；阶段 1
集成测试仍使用本地测试账号，生产应改用最小权限只读账号。

探测使用的连接 timeout 为 5000 ms；客户端 `Session` 还观察到默认
`fetch_size=5000`、
`zone_id='Asia/Shanghai'`、`enable_redirection=True`、`use_ssl=False`。
这些是当前客户端构造参数的观测值。阶段 1 只显式配置 host/port/user/password/
connection timeout；`fetch_size`、`zone_id`、`enable_redirection` 仍使用客户端
默认值，未作为策略参数，后续不得假设它们已被固定。

关键过程：

1. `Session.open(False)` 成功；
2. `SHOW VERSION`、`SHOW DATABASES`、`SHOW CLUSTER` 成功；
3. 单条 SELECT 成功，完整迭代结果集并关闭句柄；
4. `Session.close()` 成功；
5. 未发送 DDL/DML/GRANT/SET 等语句。

树模型方言下 `SELECT 1` 不是合法健康检查，会报：

```text
700: Error occurred while parsing SQL to physical plan:
line 1:8 no viable alternative at input 'SELECT 1'
```

健康检查应使用 `SHOW VERSION`，或使用已知存在的 device path 做
`SELECT ... LIMIT 1`。

## 6. EXPLAIN 能力

- `EXPLAIN <SELECT>`：支持，结果单列 `distribution plan`，纯文本。
- `EXPLAIN ANALYZE <SELECT>`：支持，结果单列 `Explain Analyze`，包含运行时
  算子统计；**会真实执行查询**，不能当作无副作用 EXPLAIN。
- `EXPLAIN FORMAT JSON`、`EXPLAIN ANALYZE FORMAT JSON`、`EXPLAIN VERBOSE`：
  均被拒绝，错误分别为：

```text
700: Error occurred while parsing SQL to physical plan:
line 1:8 mismatched input 'FORMAT' expecting {<EOF>, ';'}

700: ... line 1:16 mismatched input 'FORMAT' expecting {<EOF>, ';'}

700: ... line 1:8 mismatched input 'VERBOSE' expecting {<EOF>, ';'}
```

因此不存在可依赖的 JSON 计划字段；Phase 1 只能解析文本，且必须保留
`UNAVAILABLE`/`EMPTY` 状态。详细的算子、计划和波动性见
[`DB_CAPABILITY_AUDIT.md`](DB_CAPABILITY_AUDIT.md)。

## 7. 客户端版本复验

### 7.1 历史记录：阶段 0 曾安装 2.0.10

```text
apache-iotdb 2.0.10
import iotdb  # 包名不是 apache_iotdb
```

这是阶段 0（2026-11-01）的历史观测。2.0.10 客户端可以完成当时的连接、查询、
LAST 和 EXPLAIN 探测，但这不是长期兼容性证明；2026-11-02 当前 Conda 环境
已改为 2.0.6，见 §7.4。

### 7.2 可选的 2.0.6 客户端环境

阶段 0 最初在仓库外临时 venv 中安装 2.0.6。阶段 4 已把该过程固化为仓库脚本，
不再依赖临时 job 路径：

```bash
scripts/setup_iotdb206_env.sh            # 默认 .venv-iotdb206/
scripts/setup_iotdb206_env.sh /path/to/venv
```

脚本从 `pyproject.toml` 安装 `.[dev]`（其中 `apache-iotdb==2.0.6` 为固定依赖），
并自检 `apache-iotdb` 版本必须严格等于 `2.0.6`，否则以非零退出。默认目标
`.venv-iotdb206/` 已加入 `.gitignore`；脚本不修改共享 Conda 环境，不读取或
打印 IoTDB 密码。2026-11-01 已从零执行该脚本并通过自检。**该 venv 是可选的**
隔离 Python 环境，不是 `.env`；`.env` 只保存连接配置/密码（gitignore，权限
600）。2026-11-02 当前 Conda 已直接安装 2.0.6，日常测试不再需要该 venv。

复验结果：

- 连接成功；
- `SHOW VERSION` 返回 2.0.6；
- 单条 SELECT 与完整结果集读取成功；
- `SELECT LAST ...` 成功；
- `EXPLAIN` / `EXPLAIN ANALYZE` 成功；
- 行为与 2.0.10 在已测路径上一致；
- 2.0.6 还用于 LAST 只读认证（六类型、时间边界、配对计时、回退契约）；
- 2026-11-01 经用户授权，2.0.6 还用于在专用库 `root.last_cert_20261101`
  中创建 8 个对齐设备并执行 79 个可写边缘差分；目标库随后已删除。详见
  [`LAST_CERTIFICATION.md`](LAST_CERTIFICATION.md)。

项目本地 venv 不是交付物；阶段 1 已把 `apache-iotdb==2.0.6` 写入
`pyproject.toml`，并由 `doctor` 报告实际安装版本；如果要升级 server，必须
单独做兼容性审计，不能只改客户端版本。

### 7.3 本轮只读复核（2026-11-01）

用 2.0.6 客户端对 `db800_q1` 重新执行了一条受控只读查询：

```sql
SELECT s_1666, s_1766 FROM root.db800.g_0.d_0, root.db800.g_0.d_1
WHERE time = 1640966450000;
```

结果：

| 项目 | 实际结果 |
|---|---|
| `SHOW VERSION` | `2.0.6 / c6ecbde` |
| 连接打开 | 6.652 ms |
| SELECT | 1 行、5 列；完整读取并关闭 |
| `EXPLAIN` | 13 行文本计划，列名 `distribution plan` |
| `EXPLAIN ANALYZE` | 39 行运行时输出；该查询被真实执行 |
| 关闭 | 成功 |

该复核只使用只读 SELECT；探测脚本及结果在仓库外，结果文件不含密码。单次
计时不是 benchmark 证据。

### 7.4 当前 Conda 环境复核（2026-11-02）

当前直接使用 Conda `MANTO`，不再依赖项目本地 venv：

```bash
python -c "import importlib.metadata as m; print(m.version('apache-iotdb'))"
# 2.0.6

PYTHONPATH=src python -m tsdb_optimizer doctor
```

实际结果（关键字段）：

```text
ok=true, connected=true
server: 2.0.6 / c6ecbde, model=tree
client: 2.0.6
databases: 7
explain_text=true
explain_analyze=available_but_executes_query_not_run
```

同一环境下的测试结果：默认 `598 passed, 11 skipped`，单元 `502 passed`，
真实集成 `11 passed`，启用真实集成的完整套件 `609 passed`，`compileall`
退出码 0。`.env` 仍只保存本地连接配置/密码，已 gitignore 且权限 600；
`.venv-iotdb206/` 是可选的隔离 venv，与 `.env` 无关。

## 8. 可复现的只读检查

以下命令只读，且不在仓库中保存密码。先导出连接信息：

```bash
export IOTDB_HOST=127.0.0.1
export IOTDB_PORT=6667
export IOTDB_USER=...
export IOTDB_PASSWORD=...
```

最小检查（阶段 1 已封装为 `tsdb-optimizer doctor` 和
`IoTDBTreeAdapter`；下面是最小原生示例，不是项目入口）：

```python
import os
from iotdb.Session import Session

session = Session(
    os.environ["IOTDB_HOST"],
    int(os.environ["IOTDB_PORT"]),
    os.environ["IOTDB_USER"],
    os.environ["IOTDB_PASSWORD"],
    connection_timeout_in_ms=5000,
)
ds = None
try:
    session.open(False)
    ds = session.execute_query_statement("SHOW VERSION")
    while ds.has_next():
        row = ds.next()
        print([getattr(f, "value", f) for f in row.get_fields()])
finally:
    if ds is not None:
        ds.close_operation_handle()
    session.close()
```

本报告不提供 `EXPLAIN ANALYZE` 的自动执行入口；它是诊断/Benchmark 专用，
不能放在正式查询路径前后默认执行。

## 9. 已知风险与未知项

| 项目 | 状态 | 影响 |
|---|---|---|
| 客户端版本 | **已消除（2026-11-02）** | 当前 Conda `MANTO` 为 `apache-iotdb 2.0.6`；阶段 0 的 2.0.10 仅作历史记录 |
| server 动态参数/配置项 | `NOT_VERIFIED` / `UNKNOWN` | `SHOW CONFIG`/`SHOW CONFIGURATION` 被拒绝；`SHOW VARIABLES` 只有 5 个集群变量 |
| DB 主机 CPU/内存/IO 指标 | `UNKNOWN` | 未部署 server 端采集；不能把客户端 `psutil` 当 DB 指标 |
| 冷缓存实验 | 未控制 | 阶段 0/1 计时均为热缓存、单机、无隔离负载 |
| 并发/背景负载 | 未测试 | 不能外推到生产并发场景 |
| 空值/重复时间戳/数据缺口 | 部分验证 | 已在授权专用库验证重复/乱序/缺口/全 None/SQL NULL；TTL、重启、NaN 未测 |
| 表模型用户数据 | 不存在 | table adapter 无用户数据入口 |
| 生产 workload | 未提供 | 当前只有本地 MAPO/MATAOF benchmark 查询 |
| 数据快照 | 静态 | 任何写入/TTL 变化会使 digest 和计时失效 |
| 权限模型 | 未审计 | 未测试 GRANT/只读账号；阶段 1 应使用最小权限账号 |
| 进程/PID/路径 | 可能变化 | 文档中的 PIDs 只代表探测时状态 |

## 10. 阶段 0 环境验收

| 验收条件 | 结果 |
|---|---|
| 确认 server/client 兼容 | **通过（2026-11-02）**：当前 Conda `MANTO` 客户端 2.0.6，`doctor` 实测 server 2.0.6；阶段 0 的 2.0.10 仅作历史 |
| 确认数据库模型 | **通过**：用户数据为树模型；表模型仅有系统表 |
| 能连接并完整读取结果 | **通过** |
| 有可复现 SQL/EXPLAIN 样本 | **通过**：见 `tests/fixtures/sql/`、`tests/fixtures/plans/` |
| 策略状态可分类 | **通过**：见 `docs/STRATEGY_CATALOG.md`；阶段 4 已认证 `S1_REPEAT_CACHE`，其余非 Native 候选仍禁用 |
| 无策略时明确阻塞 | **已解除**：原 `BLOCKED: NO_VERIFIED_NON_NATIVE_STRATEGY` 由固定 epoch 契约下的 `S1_REPEAT_CACHE` 五项证明解除；环境本身仍只认证 2.0.6 树模型 |

环境层面的约束只有版本锁定（server/client 2.0.6、树模型）；S1 认证依赖调用方
声明不可变 dataset epoch，不等于通用 freshness 或生产 benchmark 认证，详见策略
目录和实现状态。

## 11. 阶段 1 工程环境更新（2026-11-01，2026-11-02 复核）

阶段 1 只增加基础工程、树模型 Adapter、SQLite、JSON 日志和 CLI；没有新增
server 侧安装、DDL/DML 或配置修改。

### 11.1 依赖与运行方式

| 项目 | 值 |
|---|---|
| Python | 3.12.13 |
| 项目客户端 | `apache-iotdb==2.0.6`（`pyproject.toml`） |
| Pydantic | `>=2.7,<3`（实测 2.13.5） |
| PyYAML | 6.0.3（阶段 0 环境已有；阶段 1 配置为环境变量，不依赖 YAML） |
| pytest | 9.1.1 |
| 配置入口 | 环境变量优先，其次本地 `.env`；`.env` 被 `.gitignore` 忽略 |
| 运行入口 | `tsdb-optimizer doctor` / `tsdb-optimizer query` |

安装/运行方式（2026-11-02 当前环境）：

```bash
cd /opt/program-code/MANTO
python -c "import importlib.metadata as m; print(m.version('apache-iotdb'))"  # 2.0.6
cp .env.example .env
chmod 600 .env
# 编辑 .env，填入本地测试账号；不要提交
```

当前 Conda `MANTO` 已满足 `apache-iotdb==2.0.6`，可直接运行测试和 CLI。若需要
隔离 Python 环境，可运行 `scripts/setup_iotdb206_env.sh` 创建可选的项目本地
venv `.venv-iotdb206/`（已 gitignore），再激活或使用其 `bin/python`。
`.venv-iotdb206/` 不是 `.env`：前者是 Python 解释器环境，后者是连接配置/
凭据文件（gitignore，权限 600）。项目本地 venv 的路径和本地凭据不属于交付物。

### 11.2 真实探测结果

```text
doctor: ok=true, connected=true
server: 2.0.6 / c6ecbde, model=tree
client: 2.0.6
databases: 7
capabilities: show_version=true, show_databases=true,
              show_timeseries=true, explain_text=true,
              explain_last_query=true
explain_analyze: available_but_executes_query_not_run
integration tests: 2 passed in 0.57s
```

`doctor` 的 `elapsed_ms` 是单次客户端墙钟时间，不是 benchmark。`query` 真实
执行一条只读 SELECT 时得到 `SUCCESS`、1 行、5 列；三段计时为
`execute_call_ms=5.989`、`result_fetch_ms=1.133`、`client_wall_ms=7.396`
（单次运行，不是性能结论）。

### 11.3 配置入口探测

| 语句 | 实际结果 |
|---|---|
| `SHOW CONFIG` | 拒绝：`700 ... no viable alternative at input 'SHOW CONFIG'` |
| `SHOW CONFIGURATION` | 拒绝：同类语法错误 |
| `SHOW VARIABLES` | 可用；只返回 `ClusterName`、`DataReplicationFactor`、`SchemaReplicationFactor` 和两个 consensus protocol class |

这些结果只证明语法入口，不证明可修改参数、热生效、重启要求或回滚路径。
`CAND_DB_PARAMETER` 仍为 `NOT_VERIFIED`（子标记 `UNKNOWN`）。

### 11.4 阶段 1 环境限制

- 表模型未由 `doctor` 探测；`model=tree` 来自树模型 `SHOW TIMESERIES` 探测成功；
- 没有 DB 主机 CPU/内存/IO 采集；只有客户端计时；
- 没有 CI；真实集成测试依赖本机测试库和显式环境变量；
- 没有查询 timeout、取消、重试或连接池；
- 没有跨进程/并发/冷缓存实验，所有计时不能外推为生产收益。
