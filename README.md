# DSH Literature Workflow

**DeepSeek Harness 的论文翻译插件，当前为 `0.1.0-rc.1` 测试版。**

Zotero 取材 → PDFMathTranslate-next / DeepSeek API → 中文阅读版和左右双语版 → 独立复核 → 验收发布 → Zotero 附件回挂。

这是工具型插件，注册 `literature_workflow` 工具和三个 skills，不额外安装阅读器界面。Python 状态层、API 计量代理、PDF 组合和归档代码随包提供。专业翻译引擎、PyMuPDF 和 Zotero MCP 分别配置，安装插件不会自动下载大型模型或开始付费请求。

## 已实现

- SQLite 状态、源 PDF 冻结、创建去重、异步 worker、租约、取消、失效结果拒绝；RC1 全局最多运行一篇。
- 独立 DeepSeek API 代理：真实密钥不进引擎命令行，逐请求计量、并发/QPS/尝试上限、截断拒绝与日志脱敏。
- 固定 CLI 能力检查、表格文字翻译开关、源页试译、mono 输出收集。不是翻译模型的自定义实现。
- 从同一份译文 PDF 生成中文和左右双语 PDF；明确源页映射、续页、图表中文对照附页及审查预览。
- 必须有实际独立会话、当前候选哈希和完整覆盖的审查报告；任意旧报告、删页、证据更改不能通过。
- 不可变发布、发布文件回读、进程崩溃后重新核验；发布版不直接覆盖，使用 revise 创建新版本。
- Zotero 个人库读取、分类分页、附件冻结；MCP 导入后通过本地 API 检查真实附件文件哈希。每份附件先落归档意图，响应丢失时先对账，避免盲目重复导入。

## 先知道的范围

离线合成夹具通过不等于真实论文质量通过。首个 RC 留给 DSH 实机验证：宿主接口、DeepSeek API、具体引擎版本、原文定位、真实 Zotero 归档。

- 当前选中条目需已有选择桥（例如 Eason 的 dsh-zotero-skill）；本插件直接支持分类 key、父条目 key 和附件 key，不猜桌面 UI 选中状态。
- 只直接接入 Zotero **个人库**；群组库未实现，不会冒用个人库路由。
- 不自动 OCR 扫描件、不自动修复所有数学／复杂表格，也不自动生成图内中文标签；Harness 根据原图编写对应说明，或修订 mono PDF，再 stage 和独立复核。
- RC1 每次候选修改要求完整重新复核，不复用旧页码审查。
- 失败尝试不保证跨引擎重启复用翻译缓存；同一输入/config 的作业创建去重，但再次 start 可能重计费。限制每次尝试请求数，不将其称为人民币预算。
- 本插件独立保存数据，**不迁移、不修改旧翻译工作区**。旧译文可用 stage 导入，旧验收 PASS 不导入。

## 安装

推荐从 GitHub Release 安装 `.tgz`，或使用 GitHub 源码入口。项目是预构建 JavaScript，没有 prepare/postinstall 构建脚本。依赖 `@deepseek-ai/schemastery` 由 DSH 包管理器安装。

```text
dsh plugin --profile <你的实际 profile> add github:EIHEIK/dsh-literature-workflow
```

桌面 profile 常见为 desktop，但应先确认，不能盲目创建一个没有使用的 web profile。也可让 DSH 的插件管理器安装 Release 下载的 `.tgz` 绝对路径。重启后应出现 `literature_workflow` 工具和三个 skills。

最低声明环境：Node ≥22.19、DSH ≥0.2.1-rc.3 且低于 0.3；这些是接口目标范围，不是对全部宿主版本的实测保证。宿主若没有 skills provider 接口，工具仍可使用，手工阅读包内技能；不要伪称自动技能注册成功。

## Python 和引擎配置

使用两个独立 venv 更容易避免引擎依赖影响控制层。已有含 PyMuPDF 的解释器可直接复用。示例路径可替换，不改系统 Python。

```powershell
python -m venv D:/LiteratureTranslation/runtime
& D:/LiteratureTranslation/runtime/Scripts/python.exe -m pip install "PyMuPDF==1.28.2"

python -m venv D:/LiteratureTranslation/engine
# 先选定要测试的明确版本，再将 VERSION 替换为真实版本号。
& D:/LiteratureTranslation/engine/Scripts/python.exe -m pip install "pdf2zh-next==VERSION"
```

在插件设置中配置 `python`、`engine`、`dataDir`，参考 [config.example.json](config.example.json)。engine 应指向 `.../Scripts/pdf2zh_next.exe`。先运行 doctor，确认版本后填写 expectedEngineVersion。首次引擎运行可能下载布局模型和字体，此类下载与模型 API 费用分开。

API Key 通过 DSH 进程继承的 `DEEPSEEK_API_KEY` 提供，不发到聊天；环境更新后重新启动 DSH。默认 `allowApiCalls=false` 和 `allowZoteroWrite=false`。只有测试已获授权并配置就绪时才分别开启；doctor 不需要开启它们。

## 最小试点

在 DSH 中说：

> 读取 dsh-literature-workflow skill，先运行 doctor。配置就绪后对我指定论文的 2–3 个高风险源页试译，先停在 needs_review，不自动声称验收通过。报告引擎版本、候选路径、缺陷和 API usage；独立审查及归档按技能执行。

或者直接按工具动作操作：

```json
{"action":"doctor"}
{"action":"zotero_inventory","collectionKey":"ABCDEFGH","start":0}
{"action":"zotero_inventory","parentItemKey":"ABCDEFGH"}
{"action":"create_zotero","parentItemKey":"ABCDEFGH","sourceAttachmentKey":"IJKLMNOP","pages":"1-3"}
{"action":"start","job":"返回的 job ID"}
{"action":"status","job":"返回的 job ID"}
```

本地文件使用 create/source。stage 可接收修好的 mono PDF、mapping 数组和 supplements，例如 `[{"sourcePage":2,"title":"图 3 中文对应","text":"Wind tunnel：风洞\nUpper layer：上层"}]`。每个 mono 页必须有一个源物理页，续页可重复同一源页。

独立会话依次 review_begin、检查所有源页与两版、填写模板、review_submit；制作会话再 accept、publish、archive。真实会话身份由工具执行上下文读取，不接受模型填写 reviewerId。没有独立会话能力就保持待审。

## Zotero 归档

启用 Zotero 本地通信；安装并配置 [cookjohn/zotero-mcp](https://github.com/cookjohn/zotero-mcp) 的集成 MCP 和写操作。默认本地 API 23119，MCP 23120。插件只连接 loopback HTTP，不把论文传到第三方 OCR 云。

归档会新增“中文阅读版 · 候选短编号”和“中英左右对照 · 候选短编号”，保留原文、旧版和批注。导入后的本地附件文件必须可读取并且 SHA-256 一致。若 intent 已存在但找不到附件，返回需要对账，不自动再次导入。见 [测试与恢复说明](docs/TESTING.md)。

## 测试与数据

```text
python -m unittest discover -s tests -p "test_*.py" -v
node --test tests/host.test.mjs
python scripts/package_release.py
```

Python 测试需要 PyMuPDF，仅用合成论文、假模型和假 Zotero；包括真实进程退出和跨进程争抢。数据默认写到 `~/.dsh/storages/dsh-literature-workflow`，与插件代码分离；升级前备份该目录。不要将此目录或个人配置上传到仓库。

首次完整实机测试请使用 [交给 DSH 的测试指令](docs/DSH_TEST_PROMPT.md)。发布记录见 [VALIDATION.md](docs/VALIDATION.md)。

提供 [GitHub Actions 测试模板](docs/github-actions.yml)。当前上传凭据没有 workflow scope，因此模板尚未启用；需要时将它复制为 `.github/workflows/test.yml` 后由有该权限的账号提交。当前验证记录仅包含实际完成的本地测试。

## 许可

本项目代码 MIT。外部引擎和 PyMuPDF 保持其自身许可；未随包分发它们。详见 [第三方说明](THIRD_PARTY_NOTICES.md)。
