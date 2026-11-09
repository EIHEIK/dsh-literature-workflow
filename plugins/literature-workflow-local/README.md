# Dot 本地接入包（开发中）

包含启动与监督技能，MCP 实现在仓库 `runtime/dot_mcp.py`。当前未安装到用户的 Dot，也未发布为可用 RC。

安装器将来需要生成根目录 `mcp.json`，其中的绝对 Python 路径、已安装 runtime 路径与本机私有配置路径必须来自已验收部署；不在公共仓库写入用户路径或密钥。当前刻意不放入可误启动的默认连接配置。

私有配置包含 `url`（仅 `http://127.0.0.1:端口`）、`dotToken`、`confirmationToken`、`sessionsRoot`。MCP 宿主必须提供实际 `CODEX_THREAD_ID`；适配器读取对应本地运行记录中最新 `turn_context` 的型号和 effort，且记录目录对 Work 只读。缺少这个环境信息时拒绝运行，不接受模型传入 thread/model/effort。

一次批次确认采用 MCP `elicitation/create`，只接受关联请求的宿主响应；拒绝、取消、超时、重放或模型在等待期间变更，均不启动。该机制仍须验证当前 Dot 发起的实际 Work 是否透传宿主线程信息并支持 elicitation。

2026-11-10 实机只读核查已确认 Dot 对当前 Windows 电脑显示 Access allowed；这不等于插件发现、模型锁定、确认与后台端到端验收完成。此前电脑工具的授权超时已解决。

后续实机创建测试：Dot 报告显式指定 GPT-6.1 Sol / medium 后，桌面返回 `DesktopTaskWorkspaceUnavailableError`，未创建出任务、未得到宿主运行记录。当前不能宣称本机 MCP、`CODEX_THREAD_ID` 或 elicitation 已受宿主支持。`scripts/dot_host_probe.py` 是待工作区恢复后使用的只读探针；不能由其他型号的主聊天运行后冒充 Dot 验收。

参考：[OpenAI 插件格式](https://developers.openai.com/plugins/build/plugins)、[MCP elicitation](https://modelcontextprotocol.io/specification/2025-06-18/client/elicitation)、[MCP stdio](https://modelcontextprotocol.io/specification/2025-06-18/basic/transports)。
