# 阶段 7 阻塞解除工作：真实随机探索与隔离实例前置条件

后续 heavy_scale 实验和最新阻塞状态见 [PHASE7_HEAVY_EXPLORATION.md](PHASE7_HEAVY_EXPLORATION.md)；下文保留当时结果。

日期：2026-11-02。**部分证据缺口已解决；整体 NOT ACCEPTED / NOT READY。**
用户确认现有 query/ 数据在本次实验期间不可变，并授权最多4连接、单查询30秒、单轮60秒的只读负载。未修改IoTDB源码/配置，未扩大策略认证或启用线上模型。

## 实施范围

- `src/tsdb_optimizer/bandit/exploration.py`：Pydantic Assignment/Trial，执行前缓存状态、随机种子/索引/条件概率、真实Experience关联、来源与背景计数一致性；独立训练/留出时间及ID检查。
- `scripts/phase7_exploration.py`：受控只读入口；一个主连接，最多3个背景连接。随机选择现有S0/S1，经过现有service/Registry/Validator/Executor；每个测量只执行选定动作。
- `scripts/phase7_analyze_exploration.py`：逐次重演PRNG，核对先前assignment，拒绝缺失/篡改/中断日志；汇总原始样本、门槛和固定动作对照。
- `src/tsdb_optimizer/bandit/randomized_model.py`：仅离线的随机试验LinUCB。复用原始真实Experience准入后，仅更新被执行arm；使用执行前缓存状态分区，不用执行后的SERVER/CACHE_HIT决定分区。此新实验协议不替换旧Experience分区，也不输出生产AdaptiveAdvice。
- `tests/unit/test_exploration.py`、`test_exploration_model.py`：随机分配重放、概率、错误身份、时间泄漏、重复ID、日志中断、背景计数、真实/低质量准入、冻结模型、无反事实标签、隔离目标防串库测试。

显式独立环境文件使用 `--env-file`，不继承旧进程数据库环境变量；必须提供 `--expect-endpoint host:port`，不匹配则在创建输出或连接前停止。

## 实验设计与安全范围

使用已有 `query/queries_dbm/q4.sql` 第一条SQL，不生成或改写SQL。S0/S1独立等概率分配，每次p=0.5；在发送测量请求前写入并flush assignment日志。日志flush不声明断电fsync保证；中断日志不能用于认证。

执行前条件：结果缓存EMPTY或READY；背景请求线程数0或3；先完整采集train，再采集holdout。READY条件下，对两个可能的随机动作都先用真实S1执行填充缓存；该准备执行单独记录，不混入测量奖励。EMPTY仅清客户端结果缓存，不称为OS或IoTDB冷缓存。

前30秒允许发起请求；每个已发查询设置30秒SDK超时，之后停止新增并等待自有线程收尾。本次实测每轮25.79–30.34秒。IoTDB服务器取消仍UNKNOWN；如超时/未完成，不自动重试或假称已取消。没有提高资源阈值或人为篡改load bucket。

正式请求路径不变。参考/缓存准备属于显式实验，不是普通查询隐含双执行。背景连接各有独立Adapter并在finally关闭。

## 实际采集结果

先运行每单元4次pilot：32条测量，8轮正常结束。随后正式实验：

- **770条真实测量**：train=402、holdout=368；全部VALID、结果与完整Native参考一致。
- 770条先于执行保存的随机分配记录；seed/index/action逐次重放全部匹配，概率均0.5。
- 另有1次参考查询、387次缓存准备；doctor探测与背景请求不计作测量奖励。
- 4个训练单元每arm均≥30，解决当前实验协议的训练数量不足；并非所有留出单元均达30。
- 实测负载桶 **770条全部LOW**；背景在途计数为0的758条，1的10条，2和3各1条。故不能把requested_workers=3宣称为持续高负载或数据库总并发3。

| 单元 | 训练S0/S1 | 留出S0/S1 |
|---|---:|---:|
| EMPTY / 背景0 | 58 / 70 | 56 / 72 |
| READY / 背景0 | 63 / 56 | 66 / 62 |
| EMPTY / 背景3 | 34 / 40 | 20 / 34 |
| READY / 背景3 | 41 / 40 | 22 / 36 |

缓存命中改善客户端查询耗时，不等于改善整体service耗时。大部分固定动作service均值差的bootstrap区间跨零；READY/背景3训练单元S1反而较慢。区间使用2000次独立arm percentile bootstrap，假设IID；串行自相关/单会话漂移可能使假设失效，不用其证明生产显著收益。

离线随机试验模型用402条训练记录更新4个分区；368条留出不更新模型。已知p=0.5的IPS平均reward点估计：adaptive=-0.0022171318760600416、Native=-0.003958883667908926、fixedS1=-0.003010709896844021。reward越大越好，单位是无量纲 `-log1p(client_wall_ms/1000)`；不是毫秒节省或百分比加速。这里只评估冻结策略的离线点估计，未实际部署自适应策略，没有跨会话复现或端到端自适应收益证明。未执行arm的chosen_reward仍为null，IPS贡献为零不是伪造零耗时标签。

## 原始数据与命令

原始日志及经验库保存在本地 `data/phase7_exploration_20261102/`（data被gitignore，约7.6MiB日志，不会随Git自动分发）。不得删除它们后仅保留聚合结果。日志摘要保存在审计报告randomization_audit.journal_sha256。
可分享审计报告：[phase7_exploration_20261102.json](evidence/phase7_exploration_20261102.json)，包括逐条留出、模型快照、拒绝/门槛与结果来源分布。

```bash
# 用户已明确确认本epoch不可变；复跑须使用新输出目录和当次真实epoch声明
PYTHONPATH=src /opt/anaconda3/envs/MANTO/bin/python scripts/phase7_exploration.py --output data/phase7_exploration_20261102 --epoch phase7-immutable-20261102 --immutable-epoch-confirmed --background --trials 128
# exit 0，770条测量，8轮结束；5次按发起请求窗口提前结束，未伪造补齐

PYTHONPATH=src /opt/anaconda3/envs/MANTO/bin/python scripts/phase7_analyze_exploration.py data/phase7_exploration_20261102/journal.jsonl --output docs/evidence/phase7_exploration_20261102.json
# exit 0；随机化逐次验证通过；online_ready=false

PYTHONPATH=src /opt/anaconda3/envs/MANTO/bin/python -m pytest -q tests/unit/test_exploration.py tests/unit/test_exploration_model.py
# 13 passed in 0.49s
```

本轮分析脚本初次接入新模型时发现缺少registry实参，已修正并通过真实日志重放；未把失败运行记为通过。既有测试未删除。旧历史证据未覆盖。

## 当前阻塞逐项状态

| 阻塞 | 当前状态 |
|---|---|
| 每arm真实训练样本不足 | 新受控协议4个训练单元已解决；旧历史报告仍是4条，不重写历史 |
| 无逐次探索概率凭证 | 新770条记录已解决；旧Experience本身没有新增propensity字段，不能追溯补造 |
| SERVER/CACHE_HIT后验分区问题 | 新离线协议按执行前条件处理并真实训练；生产接口仍未认证，不混入旧分区 |
| 多个实测负载桶 | 未解决；全部LOW，不能用背景配置标签替代测量 |
| 留出各单元样本充分 | 部分未解决：背景3留出S0分别20/22条 |
| DB主机监控 | 未解决；保持UNKNOWN，不复制client值 |
| 可重复自适应端到端收益 | 未解决；目前只有单次会话、离线IPS点估计，未证明部署效果 |
| 实际LLM对照 | 未接入provider；这是可选增强，默认无LLM运行，不为了消除标记虚构结果 |

用户已答复“可提供专用隔离实例”，正在等待主机/端口/版本/模型、数据集不变条件、CPU/内存隔离与DB主机监控方式，以及独立凭据环境文件路径。收到后先只读doctor/结果复核，再建立新实例专属epoch和实验分区；不与本实例样本混合。不自动进入阶段8、不打开部署锁。

## 最终回归

```bash
IOTDB_INTEGRATION=1 /opt/anaconda3/envs/MANTO/bin/python -m pytest -q
# 763 passed in 34.67s，含13项既有真实只读集成，无skipped；获准沙箱外运行

PYTHONPATH=src /opt/anaconda3/envs/MANTO/bin/python -m compileall -q src tests scripts
# exit 0
```

此前中间全量761 passed in 35.74s；随后增加背景计数与隔离目标测试，最终以763项结果为准。专项56 passed in 2.84s与全量部分重叠，不能相加。新协议/模型/隔离配置测试最终13 passed in 0.49s。
