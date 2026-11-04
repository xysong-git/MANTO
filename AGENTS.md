# MANTO 项目约定

- 本项目的 Python 与测试环境固定为 Conda 环境 `MANTO`。运行 Python、pytest、静态检查、离线重放和项目脚本时，必须使用 `conda run -n MANTO ...`，或先 `conda activate MANTO`；除非用户明确更改，不使用其他环境，也不再反复询问或要求用户确认环境。
- Python 版本只是 `MANTO` 环境中的实际观测值，不改变环境选择；不把历史版本号当作环境名或固定解释器路径。
- 给用户的后续命令默认其终端已激活 `MANTO`，直接写 `python ...` / `pytest ...`，不重复添加 `conda run -n MANTO` 或激活步骤。助手执行时仍须确保实际使用MANTO环境。
- 开发与验收遵循根目录 `IMPLEMENTATION_GUIDE.md`，先读 `docs/IMPLEMENTATION_STATUS.md`；只推进用户指定阶段。
- 不调用 IoTDB 的单元测试、离线重放和静态检查由助手自行执行（用户已明确授权）。涉及 IoTDB 的集成测试、训练和性能实验仍由用户执行：先准备代码、配置与可复制命令，完成后读取日志分析；未经授权不自行启动数据库测试或长测。
