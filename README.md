# ZoteroMinerU

Windows 中文桌面工具：把 Zotero 论文 PDF 转为 Markdown、生成 AI 总结、保留排版生成双语 PDF，并按分类整理研究综述、追踪新文献。使用者自行填写 MinerU 和大模型 API Key。

## 下载与安装

前往 [最新版本下载](https://github.com/izjxnxn/ZoteroMinerU/releases/latest)，下载 **ZoteroMinerU-2.8.5-Windows-x64.zip**，完整解压后运行 `ZoteroMinerU.exe`。无需安装 Python，适用于 Windows 10/11 x64。

主程序 ZIP 已包含 **Zotero 插件、完整项目源码（`source/`）、许可证和使用说明**，可以直接把这个 ZIP 发给别人。源码也可在本仓库浏览，或单独下载 `ZoteroMinerU-2.8.5-source.zip`。

首次启动时，如尚未安装翻译资源包，会询问是否从本项目 GitHub Release 下载 **BabelDocWorker（约 175 MB）**。确认后自动校验 SHA-256、解压并配置到 `%LOCALAPPDATA%\ZoteroMinerU\engines\2.8.4`；安装成功后再次启动不再提示。也可稍后在 **设置 → 保留排版翻译 → 下载翻译资源包** 安装。只使用 Markdown 和 AI 总结时，可以暂不下载。

下载和解压配置期间，软件右下角显示当前阶段及进度条；校验时显示等待动画。安装成功后弹窗提示“下载和配置成功”，失败时弹窗提示原因，右下角也保留结果。

网络无法连接 GitHub 时，可在 [v2.8.4 Release](https://github.com/izjxnxn/ZoteroMinerU/releases/tag/v2.8.4) 手动下载 `BabelDocWorker-2.8.4-Windows-x64.zip`，解压后把 `BabelDocWorker` 文件夹放到主程序旁边。引擎首次检查或翻译时还会下载版面模型和字体（约 300 MB）。

在软件设置页点击“安装/更新 Zotero 插件”，按提示安装随包的 `mineru-zotero-bridge.xpi`（2.3.1）。实际验证版本为 Zotero 9.0.6、10.0.6；插件声明支持 Zotero 7～10.0，其余版本尚未逐一验证。已使用 2.8.4 主程序的用户也可直接安装新版 XPI，无需重新下载翻译引擎。详细步骤见 [使用说明](使用说明.md)。EXE 暂无代码签名。

## 数据与密钥

- 发布文件不包含作者的 API Key、个人设置、Zotero 文库、数据库或运行日志。每位使用者需填写自己的密钥。
- 密钥使用 Windows DPAPI 加密，保存在当前账户的 `%LOCALAPPDATA%\ZoteroMinerU\settings.json`，不会写入 EXE 或源码目录。分享时只发送 Release 中的 ZIP，勿分享个人应用数据目录。
- PDF 转 Markdown 会把勾选的 PDF 上传到 MinerU；总结、综述及翻译会把相应论文内容发给自己选择的大模型服务商。费用和额度由服务商决定。
- 链接附件模式不随 Zotero 文件同步；默认导入 Zotero 存储模式。支持本机个人文库，群组文库尚未验证。

## 从源码运行

使用 Windows x64、Python 3.12：

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements-lock.txt
.\.venv\Scripts\python.exe build.py --bridge-only
.\.venv\Scripts\python.exe main.py
```

构建主程序（不需要本地安装 BabelDOC）：

```powershell
.\.venv\Scripts\python.exe build.py
```

可选：自行构建翻译引擎，使用独立环境：

```powershell
python -m venv .venv-translate
.\.venv-translate\Scripts\python.exe -m pip install -r translate_worker\requirements-lock.txt
.\.venv-translate\Scripts\python.exe -m pip install pyinstaller==6.22.3 pyinstaller-hooks-contrib==2026.9
.\.venv\Scripts\python.exe build.py --worker
```

重新构建的引擎 ZIP 字节可能不同。脚本会生成 ZIP 并检查固定哈希；如果提示不符，核对新包内容，把提示的 SHA-256 更新到 `zotero_mineru/resources.py`，再运行 `build.py`。更换发布资源包时，版本、文件名、下载地址和哈希应保持一致。

```powershell
.\.venv\Scripts\python.exe -m unittest discover -s tests
node tests/test_bridge.js
.\.venv\Scripts\python.exe tools/audit_release.py
```

测试使用隔离的模拟服务，不代表真实 MinerU 云端 OCR、额度和任意论文翻译质量已经通过验证。

真实 Zotero 验收使用 `tools/qa_real_zotero.py` 创建独立文库。可用环境变量 `MINERU_ZOTERO_QA_EXE` 指定另一个 Zotero 可执行文件，用 `MINERU_ZOTERO_QA_DIR` 指定项目 `qa/` 内的独立输出目录；不指定时沿用本机 Zotero 和 `qa/real-zotero`。

## 源码结构

用户安装、运行和升级说明见 [使用说明.md](使用说明.md)。本项目自行编写的代码使用 [AGPL-3.0](LICENSE) 开源；第三方组件保留各自许可证，见 [第三方说明](THIRD_PARTY_NOTICES.md)。

- `main.py`：程序入口与单实例锁。
- `zotero_mineru/gui.py`：中文界面和托盘。
- `zotero_mineru/engine.py`：勾选＋开关驱动的转换、新论文检测、PDF 选择、已有 Markdown 跳过、持久任务状态机。
- `zotero_mineru/clients.py`：Zotero 本地 API（只读）、桥接插件调用和 MinerU 精准解析 API。
- `zotero_mineru/markdown.py`：结果解析、图片 Base64 内嵌、`MD_作者_论文名称.md` 命名、长文档分段。
- `zotero_mineru/llm.py`：统一大模型接口（OpenAI 兼容 Chat Completions + Anthropic Messages），覆盖 DeepSeek、通义千问、小米 MiMo、Claude、ChatGPT、Gemini、Kimi、GLM、硅基流动、OpenRouter、Ollama 等。
- `zotero_mineru/summary.py`：内置提示词与精读工作流（完整阅读全文撰写 → 再读全文核对补全 → 格式校验 → 统一排版；超长论文自动分段精读）。
- `zotero_mineru/summarizer.py`：AI 总结后台队列、`SUM_` 卡片与笔记添加、`AI总结索引.md` / `ai_summaries.jsonl`。
- `zotero_mineru/translation.py`：保留排版翻译队列：选原文 PDF、调用翻译引擎子进程（进度、取消、错误）、检查结果、作为 `双语_作者_论文名称.pdf` 加入论文条目。
- `translate_worker/worker.py`：翻译引擎（单独打包为 `BabelDocWorker`）：BabelDOC 解析与排版 + 通过 `llm.py` 调用用户自选的大模型；输出译左原右的双语 PDF，并检查缺字、乱码和未翻译页面。依赖版本固定在 `translate_worker/requirements-lock.txt`。
- `zotero_mineru/research.py`：分类研究综述：按 Zotero 分类树自下而上（最底层读总结卡片/摘要与论文间引用关系，回读关键论文原文核对；上级汇总子分类综述），JSON 输出由软件渲染为 Markdown（Mermaid 图）和 HTML 研究地图。
- `zotero_mineru/tracking.py`：新文献追踪：引用本分类论文的新论文 + 相似推荐 + 关键词检索 → 过滤 → SPECTER2 语义门槛（自校准）→ 引用证据加权 + MMR → 大模型逐篇评分。
- `zotero_mineru/scholar.py`：Semantic Scholar 与 OpenAlex 客户端（限速、重试、批量接口受限时逐篇查询）及向量运算。
- `zotero_mineru/researcher.py`：研究综述与追踪的后台线程：范围与层级、增量指纹、定期执行、文件与索引输出。
- `zotero_mineru/store.py`：SQLite 任务与论文勾选状态（默认不勾选），以及研究综述、追踪记录和文献数据库缓存。
- `zotero_mineru/config.py`：配置与 Windows DPAPI 密钥保存。
- `bridge/`：Zotero 桥接插件（协议 5），只提供状态、Zotero 命名、经过校验的 Markdown/总结/双语 PDF 附件与笔记添加，以及把论文名下的附件/笔记移入 Zotero 回收站，不接受任意代码。
- `build.py`：生成 XPI、单文件 EXE 和交付目录；`--worker` 时用 `.venv-translate` 打包翻译引擎。
- `tests/`、`tools/`：自动测试及独立 Zotero 验收。

扩展清单的 `update_url` 是 Zotero 强制字段，使用保留的 `.invalid` 地址，升级时手动重新安装 XPI。

解析请求只发送到 MinerU API 与服务返回的 HTTPS 文件地址；只有测试工具显式允许本机 HTTP 模拟服务。

AI 总结的服务商适配（推理标签清理、截断续写、上下文超限处理）和逐段精读时携带上一段笔记的做法，参考了开源项目 [zotero-AI-Butler](https://github.com/steven-jianhao-li/zotero-AI-Butler) 的设计思路；本项目代码独立编写。

保留排版翻译使用开源的 [BabelDOC](https://github.com/funstory-ai/BabelDOC)（AGPL-3.0）作为独立运行的翻译引擎，未修改其源码；翻译请求发给用户自选的大模型。设计参考了 [PDFMathTranslate-next](https://github.com/PDFMathTranslate-next/PDFMathTranslate-next) 和 [zotero-pdf2zh](https://github.com/guaguastandup/zotero-pdf2zh)。

研究综述的逐级汇总参考了 [GraphRAG](https://arxiv.org/abs/2404.16130) 自下而上生成社区报告的思路；新文献追踪的“跳过经典种子、按被引稀有度加权、中心化最大相似度 + 自校准阈值、MMR 多样化”参考了 [Agentero](https://github.com/poco-ai/Agentero)（MIT）的反向引用扫描设计，大模型逐篇评分参考了 [ArxivDigest](https://github.com/AutoLLM/ArxivDigest)；代码均为独立编写。追踪只调用 Semantic Scholar 和 OpenAlex 的公开接口，发送的是论文 DOI 和检索词，不上传论文内容。
