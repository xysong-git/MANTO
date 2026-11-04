# 阶段7 CPU协议独立验收

本页保留修复前验收记录。三项缺陷最新修复结果见 [修复复审](PHASE7_CPU_FIX_REREVIEW.md)；原4个审查反例现已改为拒绝/终止回归。

日期：2026-11-02。**NOT ACCEPTED：3项代码FAIL，真实策略启用条件BLOCKED。** 本轮只增加审查反例和文档，不修复业务代码，不覆盖原始日志。

依据IMPLEMENTATION_GUIDE.md:948阶段7任务、:967通过条件及:1357审查清单。代码存在不等于完成真实收益验证；模拟偏好变化与离线IPS不等于已执行的自适应策略收益。

## 缺陷与最小修复

### F1 [P1] 60秒轮次上限没有在等待过程中执行

位置：`scripts/phase7_profile_load.py:123`、`:126`、`:132`。while等待所有future完成时没有deadline检查；ThreadPoolExecutor上下文退出也会等待线程。SDK查询timeout不能保证连接、客户端完整拉取、清理和worker挂起都在60秒内。elapsed>60在结束后才检测。

反例`docs/evidence/phase7_cpu_acceptance_probes.py::test_reproduces_sampling_after_round_deadline`使用显式合成调度器，无数据库访问：第一轮时间已超过60秒，仍在70、71秒采样，到future完成才抛异常。若future一直不返回，当前路径无有界退出。此前真实轮次都小于60秒，只能证明那些运行未超限。

最小修复：统一deadline覆盖连接、执行、fetch、清理及后台停止；增加独立监护与可终止的客户端worker边界，超限立即持久化aborted并停止后续派发。不能仅使用Future.cancel或无期限shutdown(wait=True)。服务端取消未确认时保持UNKNOWN、禁止重试。为不返回worker/连接阻塞/fetch阻塞加入有界测试。`scripts/phase7_exploration.py`的Background退出等待及事后elapsed检查也需要同类审查。

### F2 [P2] 筛选汇总不能证明预声明计划被执行

位置：`scripts/phase7_summarize_load_profile.py:17`、`:35`。只比较完成数量与schedule长度，不校验每轮index对应的case/hash/workers/repeat；忽略aborted等事件，最后一条是complete即可标schedule_complete=true。

两个隔离副本反例：把首个预声明workers改成99，仍complete；在真实完整日志的终点前插入aborted，仍complete。原文件未修改。

最小修复：严格事件状态机；逐轮绑定预声明schedule、SQL/file hash、workers/repeat、唯一execution IDs、结果数量；禁止终态后事件、abort再complete、空结果完成。显式区分“计划完成”和“执行成功”。对损坏/删改/重排/重复/缺失事件加入拒绝测试。当前online_ready固定false限制了部署影响，但筛选结论仍可能失真。

### F3 [P2] 新采样协议未验证来源、时间及派生标签一致性

位置：`scripts/phase7_profile_load.py:29`、`:41`，读取端`phase7_summarize_load_profile.py:28`。ActivityObservation.db_host接受通用SystemSnapshot，没有强制DB_HOST；sampled_at未规范为带时区UTC；cpu_bucket只验证枚举，没有核对CPU原值与可用性。

反例将source_type改为client_host、CPU改为1%、标签写HIGH、sampled_at改为无时区2000年日期，Pydantic同时接受。汇总又直接统计传入cpu_bucket。

最小修复：schema验证DB_HOST来源、UTC及采样时间关系，按新协议从可用原始CPU值派生/复核标签；过期/未来/缺失样本按UNKNOWN处理。读取与写入共用一个实现，增加错source、错标签、naive时间、过期/未来时间失败测试。当前真实采样器确实验证DataNode身份，未发现真实client指标被改名；FAIL针对证据协议接收边界。

## 逐项结果

路径相对仓库根目录；缩写业务路径共同前缀为src/tsdb_optimizer/。

| 检查 | 判定 | 位置与证据 |
|---|---|---|
| 阶段所有任务真实实现 | BLOCKED | 手册:967；bandit/model.py:251部署锁；重放v2日志64 VALID、全部LOW、各单元样本不足；缺实际自适应端到端收益。另有F1有界实验FAIL |
| TODO/pass/mock假实现 | PASS（离线范围） | bandit/model.py:251的None是显式锁；decision/llm_advisor.py:42无provider返回UNAVAILABLE；模拟测试标识明确。未将synthetic数据冒充真实实验 |
| 数据字段、单位、时间戳、source一致 | FAIL | F3；schemas/learning.py:44、:54旧新协议隔离已有测试通过，但不能覆盖新ActivityObservation缺口 |
| 异常是否被吞掉 | FAIL（汇总边界） | F2忽略aborted；执行器tree_adapter.py:631结构化异常、:649关闭warning，bandit/store.py:35回滚重抛均PASS |
| 未验证IoTDB API或SQL | PASS（现有2.0.6/tree范围） | adapters/iotdb/tree_adapter.py:539既有SDK调用；本轮13项真实集成通过；140次成功筛选原日志复核。301内存拒绝保留为失败，不视为成功 |
| client冒充DB | FAIL（协议接收）；真实采集PASS | F3；monitoring/local_db.py:25、:64验证身份与独立采样；原始COUNT/SUM和分组日志source全部db_host，不声称IoTDB独占指标 |
| LLM绕过Registry/Validator | PASS | decision/llm_advisor.py:45、:58、:60；execution/executor.py:304；test_llm_advisor.py:34、:44、:54拒绝未知ID/额外SQL/超时/资源否决 |
| 错误样本正向学习 | PASS（已查路径） | bandit/model.py:202、:209、:232准入；randomized_model.py:43有坏随机样本拒绝整批IPS；筛选器无学习agent；失败4条未进入模型 |
| 普通查询使用EXPLAIN ANALYZE | PASS | adapters/iotdb/tree_adapter.py:512、:790；test_tree_adapter.py:527通过；本轮无新增诊断执行 |
| 重复执行、缓存污染、未消费结果 | PASS（不可变epoch契约内） | tree_adapter.py:614完整消费、:645关闭；execution/fallback.py:57超时不重试；缓存/真实workflow执行计数测试通过。实验reference/prewarm独立记录，不称普通单次查询 |
| 测试真实运行、失败覆盖 | FAIL（覆盖不足）；运行真实性PASS | 本轮796项实测通过；新增4反例复现F1-F3，证明原测试未覆盖。新反例4 passed意味着缺陷被成功复现，绝不是修复通过 |

LLM保持关闭；真实provider on/off收益未测，属于可选增强未验证范围，不单独阻塞无LLM系统。

## 实际测试和证据核验

```bash
IOTDB_INTEGRATION=1 IOTDB_TEST_SQL='SELECT s_355 FROM root.small_scale.g_0.d_1 WHERE time >= 1640968700000 AND time < 1640978355000 LIMIT 5' /opt/anaconda3/envs/MANTO/bin/python -m pytest -q
# 796 passed in 29.90s；13项真实只读集成，无skipped
PYTHONPATH=src /opt/anaconda3/envs/MANTO/bin/python -m pytest -q docs/evidence/phase7_cpu_acceptance_probes.py
# 4 passed in 0.43s；全部为“已存在缺陷可复现”断言，不是修复验收测试
/opt/anaconda3/envs/MANTO/bin/python -m compileall -q src tests scripts docs/evidence/phase7_cpu_acceptance_probes.py
# exit0
```

本轮另直接调用analyze重放data/phase7_cpu_v2_pilot_20261102/journal.jsonl：64条、all_samples_valid=true、cpu-activity-v2、LOW64；仍有部署未认证、收益未建立、多负载不足、每单元样本不足4项blocker。原COUNT/SUM84次和分组56次结果逐条核验SUCCESS/FULL/无fallback、source=db_host、CPU标签与原值相符，原日志SHA与报告一致。因此F2/F3目前是可复现的验证边界缺陷，没有证据认定历史真实实验已造假。

最小修复顺序：先F1安全边界，再F2/F3证据可信度，把审查反例转为“应拒绝”的正常回归测试并复审。随后才继续取得稳定可比负载及充分样本、实际自适应收益。**不进入下一阶段。**
