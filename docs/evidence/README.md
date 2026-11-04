# 证据文件

本目录保存可复现的原始证据。证据文件不包含 IoTDB 密码、连接凭据或完整查询结果行。

## `repeat_cache_evidence_2026-11-01.json`

- **生成脚本：** `scripts/repeat_cache_evidence.py`
- **环境：** Apache IoTDB 2.0.6 / tree model；客户端
  `apache-iotdb==2.0.6`；同主机 `127.0.0.1`；静态热数据；单进程。
- **运行量：** q8/q7/q11 每种 workload 30 次 Native + 30 次 S1（S1 第 1 次
  miss，后 29 次 hit）；q8 另做 50 次启用 S1 的 service run 和 50 次
  Native-only service run。
- **内容：** 每次运行的原始计时、dispatch/hit 状态、SQLite 计数，以及
  n/mean/median/p50/p95/p99/variance/stdev/min/max 汇总；
  `non_client_overhead_ms` 是 `end_to_end_ms - client_wall_ms` 的派生值，
  包含决策、验证、SQLite 审计/持久化、日志和 harness，不是单独测量的纯
  optimizer 成本。
- **限制：** 受控热缓存微基准，不是生产 benchmark；没有冷缓存、并发、多日、
  DB 主机指标。q11 hit 有一个 77.469 ms 离群值，典型命中延迟看 median。
- **复现：**

  ```bash
  scripts/setup_iotdb206_env.sh
  PYTHONPATH=src .venv-iotdb206/bin/python scripts/repeat_cache_evidence.py \
    > docs/evidence/repeat_cache_evidence_2026-11-01.json
  ```

脚本在开始前调用 `doctor()`，若 server 不是 2.0.6/tree 或任一执行失败则拒绝
输出收益结论。它只执行 `query/queries_dbs/` 中的只读 SELECT，不写入 IoTDB。

## `phase5_learning_2026-11-02.json`

- 脚本：`scripts/phase5_learning_evidence.py`，当前 Conda MANTO / apache-iotdb 2.0.6。
- q4/q5 各 Native 预热 1 次，再固定种子打乱各 4 次 Native/4 次 S1 miss，miss 后
  另测 1 次 hit。24 条测量观测、16 次测量 dispatch；预热与 doctor 不计奖励。
- 全部实际完整结果对比通过才标 MATCHED；这是显式诊断实验，普通查询仍 NOT_CHECKED。
- 12 VALID、12 LOW_CONFIDENCE（q5 模板超出已验证解析范围），后者 reward=null。
- 保存真实经验协议、IDs、客户端快照、质量原因、reward、分组统计与源成员；不保存结果行。
  statistics 中 model_version 为本次派生统计快照 ID，published_model_version 才是
  原子发布的当前 head（无合格发布时为 null）。
- SERVER/CACHE_HIT、行数和负载上下文隔离；无真实 DB metrics agent，DB 负载 UNKNOWN。
  不声称生产加速、冷缓存实验或在线学习泛化有效。
- 复现（调用者须保证 epoch 内数据不变）：

```bash
PYTHONPATH=src python scripts/phase5_learning_evidence.py \
  --dataset-epoch phase5-static-20261102 --repeats 4 \
  > docs/evidence/phase5_learning_2026-11-02.json
```

## `phase5_learning_fixes_2026-11-02.json`

阶段 5 审查修复后的独立真实证据。生成命令与初版相同，输出到本文件；原文件保留历史。
24 条真实观测、16 次测量 dispatch；12 VALID / 12 LOW_CONFIDENCE（未支持模板）。
全部经验新增 quality_version=2、resource_policy_version/hash，模型同样携带质量版本
与策略指纹。初版缺少新证明的经验仍可审计，但不能加入当前奖励/延迟训练。
本轮没有真实 DB 指标，也没有宣称跨负载生产收益。

`phase5_review_probes.py` 是三个原始审查反例，显式运行时修复后为 3 passed；
相同断言及扩展边界已纳入 tests/unit/test_learning_review_regressions.py，默认套件会执行。
