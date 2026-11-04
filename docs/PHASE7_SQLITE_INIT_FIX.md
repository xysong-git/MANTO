# 阶段7：重复SQLite初始化的最小修复

2026-11-03。本轮只定位和修复共享准备路径的重复初始化，不改模型、奖励、策略集合、实验调度或收益门槛。阶段7仍NOT ACCEPTED；F2真实额外收益尚未通过。

## 已证实的根因

v7每批构造Orchestrator，其构造函数调用同一SQLiteStore.initialize()。旧实现即使数据库已经是当前schema，也重复执行表/索引脚本、列检查和user_version写入。离线临时SQLite探测：第一次初始化成功后，再调用一次，连接配置完成后trace捕获27条SQL。该路径位于v7的setup计时内，但不能据此把全部setup耗时归因于迁移。

## 修复边界

仅修改storage/sqlite.py：首次成功初始化后记住数据库身份与SQLite schema_version；重复调用仍读取user_version、schema_version，核对文件绝对路径/device/inode（内存库核对连接身份）。全部一致且user_version为当前值，才跳过迁移。正常重复调用的trace为2条只读PRAGMA；连接的WAL/FULL配置检查仍保留，不计入上述两次trace的比较。

初始化标记只在commit/close完成后发布。schema变化、文件替换、版本不兼容均重新走原有校验/迁移；失败不留下ready标记。close清除标记，内存数据库关闭再打开会重新建库。没有复用查询结果/动态资源状态，没有改变持久化模式、事务提交频率或扣除真实耗时。

继续遵守既有单owner用法；不承诺支持其他进程在调用中并发覆盖数据库文件，或通过writable_schema等手工方式绕过SQLite schema计数器。数据库数据内容正常更新不会失效schema标记，这是预期行为。

## 实际验证

使用Conda MANTO，自行执行了不调用IoTDB的测试：

```bash
conda run --no-capture-output -n MANTO python -m pytest -q tests/unit/test_storage_initialization.py tests/unit/test_storage.py tests/unit/test_wal_request_session.py tests/unit/test_orchestrator.py tests/unit/test_learning.py tests/unit/test_learning_review_regressions.py
```

**124 passed in16.68s**，exit0。新增测试覆盖重复初始化仅检查版本、外部schema修改修复、文件替换、内存库重建、初始化失败重试、未知user_version拒绝。

```bash
conda run --no-capture-output -n MANTO python -m pytest -q tests/unit
```

**863 passed in43.49s**，exit0。没有执行IoTDB集成测试、训练或压测；没有更改已有v7日志/SQLite/报告。

## 改动与剩余事项

运行代码只改src/tsdb_optimizer/storage/sqlite.py；新增tests/unit/test_storage_initialization.py；更新本报告、README与IMPLEMENTATION_STATUS。没有重写Orchestrator或学习模块。

结论限定为重复迁移工作已消除并通过离线回归，不是查询端到端提速或F2收益通过证明。该成本也影响固定策略，减少它不保证自适应相对固定S1更优。现有v7证据保持原结论；若之后验证真实收益，应先预声明新版本/独立留出，由用户执行，不能将修改后的代码冒充原v7实现。
