# 第三方组件

本软件使用下列第三方组件。其商标、版权和许可证属于各自权利人；打包未改变这些组件的许可证。

| 组件 | 用途 | 官方许可证／说明 |
|---|---|---|
| Python | 程序运行时 | [PSF License](https://docs.python.org/3/license.html) |
| PySide6 / Qt | 中文桌面界面 | [Qt for Python 许可](https://doc.qt.io/qtforpython-6/licenses.html)；随包 Qt/PySide6 的许可证见对应发行包 |
| requests | HTTP 请求 | [Requests Apache 2.0](https://github.com/psf/requests/blob/main/LICENSE) |
| pypdf | 读取 PDF 页数（超过 200 页时分段解析） | [BSD-3-Clause](https://github.com/py-pdf/pypdf/blob/main/LICENSE) |
| PyInstaller | EXE 打包 | [GPL 及启动器例外](https://pyinstaller.org/en/stable/license.html) |

翻译引擎 `BabelDocWorker` 是单独的程序（主程序只通过命令行和 JSON 文件调用它），打包了下列组件：

| 组件 | 用途 | 官方许可证／说明 |
|---|---|---|
| [BabelDOC](https://github.com/funstory-ai/BabelDOC) 0.6.4 | PDF 解析、版面分析、译文排版与双语 PDF 生成 | [AGPL-3.0](https://github.com/funstory-ai/BabelDOC/blob/main/LICENSE)；未修改源码；运行时只替换了斜体字体的选择、不加载 PyMuPDF 2 自带的版面模型（见 `translate_worker/worker.py`） |
| [PyMuPDF](https://github.com/pymupdf/PyMuPDF) / MuPDF | PDF 读写 | [AGPL-3.0](https://github.com/pymupdf/PyMuPDF/blob/main/COPYING)（或 Artifex 商业许可） |
| DocLayout-YOLO 版面模型（BabelDOC 提供的 ONNX 版本） | 识别页面中的标题、正文、公式、图表 | [AGPL-3.0](https://github.com/opendatalab/DocLayout-YOLO)；首次使用时下载到 `%USERPROFILE%\.cache\babeldoc` |
| 思源宋体/思源黑体、Noto、Go Noto Kurrent 等字体 | 嵌入译文 | [SIL Open Font License 1.1](https://openfontlicense.org/)；首次使用时下载 |
| ONNX Runtime、OpenCV、NumPy、SciPy、scikit-learn、scikit-image、Hyperscan、Rtree、HarfBuzz、FreeType、tiktoken、httpx、OpenAI Python 等 | BabelDOC 的依赖 | 各自许可证（MIT、Apache-2.0、BSD、FreeType License 等），见 `BabelDocWorker\licenses` |

按 AGPL-3.0 的要求：BabelDOC 和 PyMuPDF 的完整源码可从上面的官方仓库按所列版本获取；`BabelDocWorker` 自身的源码（`translate_worker/worker.py` 及其调用的 `zotero_mineru/llm.py`）和打包脚本包含在本项目源码包中，依赖版本固定在 `translate_worker/requirements-lock.txt`。

AI 总结和翻译通过用户自选的大模型服务商网络 API 调用，本软件不打包任何大语言模型。部分设计思路参考 [zotero-AI-Butler](https://github.com/steven-jianhao-li/zotero-AI-Butler)，未复制其源码。

研究综述与新文献追踪的设计思路参考了 [Agentero](https://github.com/poco-ai/Agentero)（MIT 许可，反向引用扫描的过滤与排序方法）、[GraphRAG](https://arxiv.org/abs/2404.16130)（逐级汇总）和 [ArxivDigest](https://github.com/AutoLLM/ArxivDigest)（大模型相关度评分），未复制其源码。新文献追踪调用 [Semantic Scholar API](https://www.semanticscholar.org/product/api)（Allen Institute for AI）和 [OpenAlex API](https://openalex.org/)，数据的使用条件以各服务的条款为准；本软件不打包它们的数据。

MinerU 通过网络 API 调用，本软件未打包 MinerU 模型或开源推理引擎。MinerU 的服务条件和账户额度由服务方决定。

Zotero 桥接扩展独立编写，通过 Zotero 的 JavaScript 接口和本地 HTTP 服务工作；没有修改 Zotero 自身数据库结构或复制其实现源码。

本项目包含客户端和扩展的完整源码及打包脚本，便于检查、修改和重新构建。发布给其他人时，应一并保留相应第三方组件的许可证和通知。
