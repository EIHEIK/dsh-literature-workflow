---
name: pdf2zh-next-runner
description: Diagnose or run the fixed PDFMathTranslate-next backend through literature_workflow; configure bounded DeepSeek API translation without exposing keys or treating engine success as acceptance.
---

# PDFMathTranslate-next 适配

先 `literature_workflow {action:"doctor"}`。运行时必须是 Python 3.10+ 且含 PyMuPDF；engine 是 pdf2zh_next 的真实绝对可执行路径。doctor 检查引擎帮助所含 OpenAI-compatible 与表格翻译参数，读取版本，不调用 API。固定通过试点的版本，填写 expectedEngineVersion。

API Key 使用 DSH 进程继承的 `DEEPSEEK_API_KEY`。插件只将随机短期 loopback token 传给引擎；真实 key 留在代理进程中。不把 key 写进参数、作业 JSON 或聊天。

`start` 使用官方 DeepSeek 接口，初始 deepseek-flash、并发 2、1 QPS、每次尝试上限 100 请求。代理统一计量请求；空答案和截断不算成功。限流和网络错误由引擎可能重试，每次代理请求都计入上限。引擎退出零也只能生成待审候选。

运行结束若发现多个 mono 文件，或译文页数不支持安全推断，任务保持 needs_attention。查看 redacted 日志，选择正确 mono 路径并提供经过实际检查的逐页 mapping 交给 stage。不得为了继续而把对应关系猜成连续页。

英文图内标签需追加中文对应，公式和表格要对照原页检查。RC1 不自动 OCR 扫描件、不重建复杂表格，也不自动给科学图重画中文；通过后端或局部人工/模型修订生成待审材料，再由独立复核裁定。
