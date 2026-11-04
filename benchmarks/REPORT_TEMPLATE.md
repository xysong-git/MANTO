# Benchmark 报告模板

## 实验身份与资格

- 运行目录、config/plan/source SHA-256：
- 时间、Python/客户端/服务端版本、模型、数据集 epoch、导入证书：
- 原始日志、失败/中止批次、reference 与 correctness：
- 阶段 7 前置限制：NOT ACCEPTED；阶段 8 真实验收：待核验。
- 样本门槛、计划覆盖、实际负载、模型冻结/重放结果：

## 协议

- 固定 SQL/参数、训练与留出划分、行数/FULL、排序/容差：
- warmup、应用缓存条件、IoTDB/OS 缓存 UNKNOWN 范围：
- repetitions、seed、random/ABBA、并发、负载驱动与实际档位：
- E0/E1/E2/E3/E4/A1/A2 定义及模型参数/奖励版本：
- 安全停止、失败与 fallback 情况：

## 结果

按 workload/cache/load/concurrency 分区附原始 CSV、JSON 和图表。
报告样本数、均值、std、P50/P95/P99、block CI、吞吐、两类主机 CPU/memory、
decision overhead、完整 E2E、correctness、选择分布、实际命中及回退。
服务器纯执行时间无可靠来源时填写 UNKNOWN，不能用 client_wall 替代。

| 对照 | 配对块数 | E2E 差值 ms | CI 与方法 | 结论/限制 |
|---|---:|---:|---|---|
| E4−E0 | | | | |
| E4−E1 | | | | |
| E4−E2/E3 | | | | |
| A1−E4 | | | | |
| A2−E4 | | | | |

## 成本与局限

- 连接/准备/预热/关闭、缓存填充、离线拟合和日志成本：
- 未控制的缓存层、外部负载、系统调度、频率/温度、资源归因：
- 多重比较、单会话时间依赖、独立重复、负面/不确定结论：
- 冻结推断与持续在线训练的边界、动态写入适用性：
- 未执行策略的反事实性能/regret：UNKNOWN。

## 验收和下一步

逐项 PASS / FAIL / BLOCKED；不以单次最优结果或结构测试通过宣称实际收益。
列明可复制命令、所有未完成项及需要新预声明实验的问题。
