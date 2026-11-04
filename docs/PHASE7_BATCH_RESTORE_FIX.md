# 阶段7 F1：冻结批次模型恢复

后续结果：用户报告49 passed in6.46s，冻结后独立推断恢复在限定范围内通过；不包含训练回退续训或生产部署。下文保留执行前说明。

2026-11-03。只修复审查F1的工程缺项，不调整收益门槛、模型参数、实验计划或IoTDB接口。**实现已补，测试待用户执行，阶段7仍NOT ACCEPTED。**

## 根因与处理

`DelayedBatchStore`已经保存完整历史版本，但只能读取当前训练head。仅回拨head会使旧模型owners/evidence与已APPLIED反馈不一致；若重新消费，将产生重复归因风险。旧`OfflineModelStore.rollback`不适用于这一数据协议。

在同一SQLite中新增`inference_head`和`inference_restores`，作为显式离线推断选择与审计。`restore_inference()`只允许训练已冻结、没有PENDING时使用；事务内验证训练revision和推断generation令牌，加载并核验目标checksum、Pydantic结构、scope、配置、训练截止时间及历史账本包含关系。新审计记录与推断指针一起提交，失败一起回滚。

`current()`、`freeze()`及原实验脚本仍读取训练head；`inference_current()`返回独立选择的模型。恢复操作不修改训练head、versions、feedback、feedback_ids，不把APPLIED改回PENDING，不解除冻结，不启用生产策略。可再次显式选择原冻结版本。

## 接口

```python
# queue是已冻结且排空的离线模型库；先使用副本验证操作流程。
model, selected_revision, generation, training_revision = queue.inference_current()
receipt = queue.restore_inference(
    target_revision,
    expected_training_revision=training_revision,
    expected_generation=generation,
)
restored_model, selected_revision, generation, training_revision = queue.inference_current()
# 显式推断消费者使用restored_model.choose(...)，仍经过Registry过滤。
```

`InferenceRestoreReceipt`为冻结Pydantic协议，包含schema_version、递增generation、训练/前一/目标revision、目标fingerprint和UTC时间。并发旧调用持有过期generation时拒绝，不能覆盖较新的选择。

默认无恢复记录时，inference_current返回原冻结head和generation=0。旧库由构造函数添加两张空表；原模型/反馈格式不迁移。此次未实例化或修改任何已有实验SQLite，未修改v6原日志/报告。

## 测试与实际检查

新增`tests/unit/test_delayed_batch_restore.py`，使用历史衍生fixture和临时SQLite，不访问IoTDB：

- 旧版本预测/指纹恢复、重启持久化、切回原版本；训练四张表逐行保持不变。
- 冻结后仍拒绝入队、不会重复消费已APPLIED反馈。
- 未冻结/PENDING、过期令牌、未知/未来/非法revision拒绝。
- 损坏checksum、跨server scope、配置/奖励版本不兼容拒绝。
- 写入指针时注入SQLite失败，审计记录和指针原子回滚。
- 恢复审计损坏时明确报错，不静默换模型。

实际只运行Conda MANTO下的`ast.parse`静态语法检查：两个变动Python文件均可解析，exit0。**未运行pytest，不能沿用v6的942 passed证明本次改动。**

由用户执行：

```bash
cd /opt/program-code/MANTO
conda run --no-capture-output -n MANTO python -m pytest -q tests/unit/test_delayed_batch_restore.py tests/unit/test_delayed_batch.py tests/unit/test_linucb.py
```

本次不需要启动训练或数据库压测来检查恢复事务。收到实际测试结果后再判定F1是否在此限定范围内通过。

## 限制与未完成项

仅支持冻结后的离线推断版本恢复，不支持训练倒退后续训，也没有自动发布到生产。调用方必须显式采用inference_current；原实验中的训练/留出模型不会被替换。训练head本身或恢复审计损坏时失败关闭，不提供绕过校验的灾难恢复。

F2收益门槛、F3同口径L1统计对照均未修改、仍未完成；此次修复不构成性能提升或阶段7通过证据。

修改：`src/tsdb_optimizer/bandit/delayed_store.py`；新增恢复测试；本报告、README、IMPLEMENTATION_STATUS及原审查报告的后续说明。没有重写其他模块。
