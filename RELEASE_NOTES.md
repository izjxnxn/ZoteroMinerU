# ZoteroMinerU 2.8.5

- 桥接插件更新为 2.3.1，兼容上限由 Zotero 9 扩展到 Zotero 10.0（`strict_max_version: 10.0.*`），解决 Zotero 10 正式版拒绝安装的问题。
- 已在官方 Zotero 10.0.6 的独立测试文库中验证安装加载、桥接认证、论文命名、Markdown/总结卡片/笔记/双语 PDF 附件和移入回收站流程；云端解析及大模型使用本地模拟服务。
- 主程序内嵌 XPI、独立 XPI、整合 ZIP 和源码包同步更新。已有 2.8.4 主程序可直接安装新版插件，无需重新下载 BabelDocWorker。
- 资源包下载、校验、解压配置的进度显示在软件右下角；下载及配置全部成功后弹窗确认，失败也会弹窗提示，右下角保留结果。

下载主程序：**ZoteroMinerU-2.8.5-Windows-x64.zip**。完整解压后运行 EXE，无需 Python。翻译引擎仍使用 v2.8.4 Release 中的 BabelDocWorker 2.8.4，自动下载地址及 SHA-256 保持不变。引擎资源包约 175 MB，首次检查或使用引擎时还会下载约 300 MB 的版面模型和字体。

主程序和 BabelDocWorker 为两个独立程序。只使用 Markdown 和 AI 总结时可暂不下载引擎。Zotero 插件仍需在 Zotero 的“工具 → 插件 → 从文件安装插件”中安装。

所有使用者自行填写 MinerU 和大模型 API Key。PDF 解析会上传勾选的 PDF 至 MinerU，总结和翻译会把相应内容发至所选大模型服务商。EXE 暂无代码签名。
