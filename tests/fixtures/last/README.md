# LAST 认证摘要 fixture

本目录保存 **`CAND_LAST_SINGLE_SERIES` 的认证摘要**，不是策略启用配置，
也不是生产测试结果。摘要由仓库外的完整认证结果生成，原始逐样本数据和脚本
**未提交**。

## 文件

### `last_certification_summary.json`

只读认证摘要，保留：

- 数据集快照和六种数据类型的归一化值；
- 1,200 个 `WHERE time <= t` 差分采样汇总；
- 8 个时间边界/空结果 case；
- 多序列、多设备、值过滤、错误路径、`LIMIT 2`、`ORDER BY ASC` 负例；
- Native 与 LAST 的 EXPLAIN 文本；
- 30 个配对块、两个 session 的性能汇总和 bootstrap CI；
- 注入式回退契约结果。

这份摘要的结论仍带历史上的 `EDGE_CASES_UNTESTED` 子标记，反映生成时的
覆盖范围；当前状态以边缘摘要和认证报告为准。

### `last_readonly_recheck_20261101_summary.json`

2026-11-01 的只读复核摘要，保留：

- 对 `query/queries_dbs` 全部 12 个文件、24,000 条语句的逐语句扫描：0 条
  符合条件的 Native 单序列 `ORDER BY time DESC LIMIT 1`，2,000 条 `SELECT LAST`
  全部是 q8 的多序列/全设备查询；
- 别名、`LIMIT`/`ORDER BY`、`OFFSET`、无 ORDER BY、wildcard、限定路径、
  空区间的服务端行为和 Parser 边界；
- LAST 四列结果契约、Native 两列契约、FLOAT 十进制字符串、`LAST_VALUE`
  固定 `Time=0` 的观测；
- 30 块初始计时、两次独立 120 块扩展计时及 bootstrap CI/胜率；
- 注入式回退契约。

该摘要不改变主状态：`CAND_LAST_SINGLE_SERIES` 仍是 `NOT_VERIFIED`；
需求、生产结果归一化/Executor 回退和 TTL/重启/并发/NaN 仍未认证。

### `last_edge_certification_summary.json`

2026-11-01 经用户授权生成的可写边缘认证摘要，保留：

- 专用库 `root.last_cert_20261101` 的 9 条 DDL 和 19 次 DML 尝试；
- 8 个对齐设备、六种数据类型；
- 79 个 Native vs LAST 差分 case，按场景：主数据 18、缺失测量值 32、
  重复时间戳 3、乱序 4、缺口 9、空序列 3、API 全 None 6、SQL NULL 4；
- 重复时间戳（单值覆盖，last-write-wins）、乱序后升序存储、缺口、
  显式 SQL NULL 和客户端全 None `ValueError` 的观测；
- `DROP DATABASE` 清理结果：目标库不存在，原有 7 个数据库未变化。

摘要不含密码、原始 IPv4 或原始连接信息。

## 状态

三份摘要的共同结论是：

```text
CAND_LAST_SINGLE_SERIES: NOT_VERIFIED
edge_case_coverage: PASS
variant_boundary_coverage: PASS
demand_scan: 24,000 statements; 0 eligible Native latest-value queries
submarkers: DEMAND_UNPROVEN, FALLBACK_CONTRACT_ONLY
```

干净数据只读证据、79/79 授权边缘差分和变体复核支持“候选在已测范围内语义
等价”；两次 120 块扩展计时支持受控热缓存下的方向性收益。但以下仍未认证：
TTL、重启/崩溃恢复、并发/背景负载、NaN/Infinity、生产结果归一化和 Executor
回退、真实 workload 需求、多进程/多日/冷缓存和 DB 主机指标。详见
[`docs/LAST_CERTIFICATION.md`](../../../docs/LAST_CERTIFICATION.md)。

不要编辑摘要中的数字来“提升”状态；任何更新都必须由新的真实认证运行生成，
并同步更新认证报告。

## 安全与清理边界

- 只读认证只执行 SELECT/EXPLAIN，没有 DDL/DML；2026-11-01 复核还只读取本地
  `query/queries_dbs` SQL 文件做 Parser 扫描；
- 边缘认证使用用户单独授权的专用库，不写入共享 benchmark database，
  结束后执行 `DROP DATABASE` 并核对原有数据库未变化；
- 回退部分注入了 LAST 失败结果，Native 侧使用真实只读连接；
- 普通在线查询默认不双执行；这里的差分比较是离线认证，不是运行时策略；
- 任何重跑都必须重新获得可写授权，使用新的专用库并完成清理核验。
