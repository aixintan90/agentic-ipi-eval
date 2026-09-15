# AgenticIPI Workbench v0.1.3 Preview

首个公开预览版。

## 主要能力

- 三步式 Windows 实验配置界面；
- Excel / JSON / JSONL 用例导入与预览；
- Cursor CLI 真实模型目录读取，默认 Grok 4.6 High；
- 思维树 Prompt 生成，默认预算 `8 → 4 → 2`；
- 多 CLI 并发、暂停恢复与基础设施重试；
- 只把可验证效果计入 Effect-ASR，调用意图本身不算成功；
- Prompt 级、用例级证据账本，以及 Excel / JSON / Markdown 导出；
- 可选的受控邮件和 SFTP 效果验证，默认关闭。

## 发布边界

- 发行包只包含两条隔离的合成示例；
- 不包含私有语料、历史结果、真实目标配置或任何凭据；
- EXE 尚未代码签名，Windows 可能显示未知发布者提示；
- 本版本标记为预发布，建议先在无害小语料上验收。

