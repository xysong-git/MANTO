# CO_TENANT_SAFETY_STOP：失败证据丢失修复

2026-11-04。仅处理本次停止诊断，不调整算法以通过实验。整体NOT ACCEPTED。

## 已证实与未知

cpu_load.py控制器以独立psutil整机采样检查CPU>80或memory>90；超过任何一项便停止workers，并由check()抛错。它不是IoTDB查询错误，也不是普通查询自动制造负载：扩展配置明确启用MEDIUM CPU co-tenant。占用来源包括co-tenant、数据库和其他进程，不能将整机峰值全部归因于数据库。

用户trace没有具体指标，原实现仅保存通用错误码。LiveEnvironment.load的emit位于yield/context manager之后；worker抛异常时该语句不执行，故控制器样本丢失。这是可复现代码缺陷。

只读检查最新目录phase8_multiload_c2_20261104_150700_361081920：17120请求、5760训练更新，最后allocation为HOLDOUT block9/APPLICATION_WARM/MEDIUM/concurrency2/A1，终态aborted。1038条load_control均来自先前已退出单元，失败单元没有控制器记录。先前单元的61.5等CPU值不是故障触发值；当时究竟CPU还是memory越界、峰值来源、控制器是否超调仍UNKNOWN。目录按最新匹配选取，用户未提供明确输出路径；若trace属于其他目录须重新绑定，不将该目录视为唯一可能来源。

## 最小变更

- src/tsdb_optimizer/experiments/cpu_load.py：错误保留CO_TENANT_SAFETY_STOP前缀，追加whole_host及实际超限项/数值/阈值。
- src/tsdb_optimizer/benchmark/runner.py：在finally中、worker清理完成后写load_control，保留samples及error，失败仍向上传播。
- tests/unit/test_cotenant_failure_evidence.py：CPU、memory、双项超限诊断；失败原异常身份、清理完成后事件写出、样本保留。

不改CPU80/memory90阈值、采样周期、负载目标、worker数量、warmup、SQL、策略、模型或停止行为。没有自动重试。未修改SystemSnapshot/RequestRecord Pydantic协议；既有诊断事件只增加error字段，分析器原本允许load_control，不影响收益资格判定。符合手册来源隔离、保留失败原始数据和不吞异常要求。它不能补回历史未落盘样本，也不保证资源峰值消失。日志磁盘写入失败仍抛错，不伪报保存成功。

## 实际测试（MANTO，无IoTDB）

修复前：`conda run -n MANTO python -m pytest -q tests/unit/test_cotenant_failure_evidence.py` → 4 failed in1.23s。

修复后：

```bash
conda run -n MANTO python -m pytest -q tests/unit/test_cotenant_failure_evidence.py tests/unit/test_staggered_load.py tests/unit/test_controlled_study.py tests/unit/test_phase8_benchmark.py tests/unit/test_phase8_admission.py tests/unit/test_experiment_supervisor.py
```

69 passed in61.87s，exit0。包含真实短生命周期进程清理测试和合成SDK实验，不是IoTDB训练/性能实验。原日志只读，未覆盖。

若用户再次执行原扩展命令，必须新输出目录；失败时保留目录，不删除/续写/自动重试。下一次同类故障会有超限项和失败控制器样本，再据实际值定位峰值，不通过调低负载或放宽阈值掩盖。尚不要求立即重跑整个长实验。
