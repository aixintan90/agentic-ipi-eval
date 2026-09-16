# Agentic IPI Workbench v1.0.2

正式修复版，重点解决单文件 EXE 在 WSL 模式下错误保存临时 `_MEI...` MCP Python 路径的问题。

## 本版改动

- 自动寻找稳定的 `.venv-wsl` 环境，不再把单文件 EXE 的临时解包目录写入新实验。
- 支持通过 `CURSOR_EVAL_MCP_PYTHON` 显式指定 MCP Python。
- 找不到环境时使用可诊断的 `python3`，不会生成重启后必然失效的路径。
- 保持固定老师 Windows 工作簿的自动识别：12 个 Excel、1080 条用例、12 个分类。
- 新增面向第一次使用者的完整《小白操作指南》，覆盖下载、WSL、Cursor CLI、MCP、路径适配、API、邮件、SFTP、预检、运行和导出。

## 正式口径

- 指标仍为 `actual_effect_verified`；调用意图不计成功。
- 不修改原始 Excel，不猜测用例映射，不伪造实验结果。
- 真实传输只面向操作者明确配置的固定测试目标，并保留机器证据。
