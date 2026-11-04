# 阶段7 CPU实验缺陷修复复审

日期：2026-11-02。**F1/F2/F3修复复审PASS；阶段7整体仍NOT ACCEPTED / NOT READY。** 本轮只修复验收指出的执行边界和证据协议，不改变IoTDB源码、SQL、数据、学习阈值或部署开关。

## 三项缺陷闭环

| 缺陷 | 复审 | 实现与证据 |
|---|---|---|
| F1 等待worker时无有界退出 | PASS（当前Linux实验CLI范围） | `src/tsdb_optimizer/experiments/supervisor.py:37`独立父进程监护；两个实验CLI均在子进程启动，父进程不连接数据库、不调用采样器。启动、每轮、退出均受60秒窗口约束，预留1秒清理，超限SIGKILL客户端进程组并有界join，记录aborted/UNKNOWN/no-retry。完成通知在日志flush之后发送。真实阻塞线程测试覆盖连接前、fetch、close及创建输出前卡住；客户端PID确已消失，无后续派发 |
| F2 汇总不绑定计划且忽略abort | PASS | `experiments/profile_audit.py:70`严格状态机，终态后禁止事件，缺终态拒绝；逐轮核对case/file hash/SQL hash/workers/repeat/index；结果数量、唯一execution ID、执行时间及实际SQL摘要均验证。aborted只能产生未完成报告；完成与成功分别输出。新日志不可删除实际SQL摘要 |
| F3 来源/时间/CPU标签不一致 | PASS | `monitoring/activity.py:9`、`:17`共享Pydantic协议：仅DB_HOST；aware时间规范为UTC；按原始值、有效性和新鲜度核验标签；未来/过期/不可用样本仅允许UNKNOWN，不能成为已知CPU档位。读取与写入共用逻辑；历史computed expiry严格核验后兼容 |

进程监护覆盖筛选与随机探索，包含后台线程清理。脚本内部`run`是子进程工作体，命令行必须经`supervise`入口；不是替换生产Orchestrator的执行模型。父进程终止客户端不能证明IoTDB服务端取消，因此超限报告保持UNKNOWN，整批停止，不重试。当前实现依赖Linux fork/setsid/进程组信号，不宣称其他操作系统已验证，也不宣称操作系统不可中断I/O下能得到实时调度保证；不能确认终止时明确报错而非继续执行。

## 测试及真实证据

- 新增 `tests/unit/test_experiment_supervisor.py`、`test_profile_audit_regressions.py`；原测试保留。
- 原 `docs/evidence/phase7_cpu_acceptance_probes.py` 的4个反例改成拒绝/终止断言。此前“4 passed意味着缺陷复现”的历史结论保留在旧报告；现在4项表示反例已被拒绝。
- 更新原roundtrip测试的合成fixture，使其包含合法计划、前置观测及非空结果；没有删除或放松断言。
- 历史原始日志重放：失败批1轮仍未完成；COUNT/SUM36轮与分组24轮仍完成且成功。旧结果缺少每次执行SQL摘要，明确`execution_sql_binding_verified=false`，不追溯补写。报告：[历史重放](evidence/phase7_cpu_fix_historical_replay_20261102.json)。
- 最终受监护筛选：3轮、1/2/4连接、7次成功Native执行，最长0.962秒；完成/成功/执行SQL绑定均true。[最终筛选](evidence/phase7_cpu_guard_final_profile_20261102.json)。
- 最终受监护随机冒烟：4轮、8条全部VALID、CPU LOW8，最长约1.08秒；随机审计通过。[最终探索](evidence/phase7_cpu_guard_final_exploration_20261102.json)。此为修复验证，不是充分训练或收益实验。
- 完成通知调整前另有同规模7次筛选和8条探索，原始目录`data/phase7_cpu_guard_fix_*_20261102`保留；不与最终批次混合补样本。

## 实际命令

```bash
# 最终版本的两个真实只读CLI冒烟，分别运行，不叠加连接数
PYTHONPATH=src /opt/anaconda3/envs/MANTO/bin/python scripts/phase7_profile_load.py --output data/phase7_cpu_guard_final_profile_20261102 --epoch heavy-complete-20261102-v2 --immutable-epoch-confirmed --local-db-pid 3879777 --repeats 1 --scales small_scale --types q03_count_aggregation
PYTHONPATH=src /opt/anaconda3/envs/MANTO/bin/python scripts/phase7_exploration.py --output data/phase7_cpu_guard_final_exploration_20261102 --epoch heavy-complete-20261102-v2 --immutable-epoch-confirmed --sql-file query/heavy_scale/small_scale/q03_count_aggregation.sql --local-db-pid 3879777 --trials 2 --rounds-per-cell 1 --load-protocol cpu-activity-v2
# 均exit0；重跑须验证新PID并使用新的输出目录
PYTHONPATH=src /opt/anaconda3/envs/MANTO/bin/python scripts/phase7_summarize_load_profile.py data/phase7_cpu_guard_final_profile_20261102/journal.jsonl --output docs/evidence/phase7_cpu_guard_final_profile_20261102.json
PYTHONPATH=src /opt/anaconda3/envs/MANTO/bin/python scripts/phase7_analyze_exploration.py data/phase7_cpu_guard_final_exploration_20261102/journal.jsonl --output docs/evidence/phase7_cpu_guard_final_exploration_20261102.json
# 均exit0，仅说明审计报告生成，不代表阶段通过
PYTHONPATH=src:tests/unit /opt/anaconda3/envs/MANTO/bin/python -m pytest -q tests/unit/test_experiment_supervisor.py tests/unit/test_profile_audit_regressions.py tests/unit/test_load_protocol.py docs/evidence/phase7_cpu_acceptance_probes.py
# 最终专项44 passed in 4.81s
IOTDB_INTEGRATION=1 IOTDB_TEST_SQL='SELECT s_355 FROM root.small_scale.g_0.d_1 WHERE time >= 1640968700000 AND time < 1640978355000 LIMIT 5' /opt/anaconda3/envs/MANTO/bin/python -m pytest -q
# 最终全量829 passed in 36.44s，含13项真实集成，无skipped
/opt/anaconda3/envs/MANTO/bin/python -m compileall -q src tests scripts docs/evidence/phase7_cpu_acceptance_probes.py
# compileall exit0
```

## 当前阶段逐项复审

| 项目 | 判定 | 依据 |
|---|---|---|
| 阶段7全部任务/通过条件 | BLOCKED | 手册:967；真实多负载、充分样本、实际自适应收益和部署认证未完成 |
| TODO/pass/mock假实现 | PASS（已实现范围） | 实验monitor不产生虚构指标；阻塞测试明确合成且不访问DB；model.py:251部署锁保持 |
| 字段/单位/时间戳/source | PASS | activity.py共享验证、profile_audit严格关联，F3拒绝回归通过 |
| 异常传播/失败可观测 | PASS | 子进程异常保留stderr与非零退出；父进程记录abort；汇总拒绝非法终态，F1/F2测试通过 |
| 未验证IoTDB API/SQL | PASS（现有2.0.6/tree） | 未新增数据库API，继续Native/Registry/Validator；真实只读集成与CLI通过 |
| client指标冒充DB | PASS | 仅接受DB_HOST，实际采样仍验证DataNode身份；整机指标不称IoTDB独占资源 |
| LLM绕过Registry/Validator | PASS | 决策/执行校验未改，LLM保持关闭，原相关回归保留 |
| 错误样本正向学习 | PASS | 筛选无学习入口，坏随机样本准入仍拒绝；奖励/阈值未改 |
| 普通路径EXPLAIN ANALYZE | PASS | 无新增诊断执行；原never_analyze回归通过 |
| 重复执行/缓存污染/未消费结果 | PASS（epoch契约内） | 不自动重试；超限停止整批；真实Native完整消费；旧epoch不混合 |
| 测试真实运行/本轮失败路径覆盖 | PASS（本轮修复范围） | 原反例拒绝、真正不返回线程的进程终止、旧日志重放、真实CLI和全量回归 |

改动文件：新增`experiments/__init__.py`、`experiments/supervisor.py`、`experiments/profile_audit.py`、`monitoring/activity.py`；修改两个实验CLI及筛选汇总脚本；新增两组回归和合成fixture、更新原协议测试及审查反例；更新状态、README、指标文档和本报告及JSON证据。未将整个既有未跟踪项目提交Git。

剩余工作不因修复完成而消失：当前仍缺稳定且合格的多负载场景、v2每单元充分样本和可重复的实际自适应端到端收益。保持online_enabled=false；**不进入阶段8**。
