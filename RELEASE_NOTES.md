# ZoteroMinerU 2.8.4

- 主程序 ZIP 整合完整项目源码 `source/`、Zotero 桥接插件、使用说明及许可证，可直接分享给其他 Windows x64 用户。
- 首次启动时缺少 BabelDocWorker，会询问是否从 GitHub 下载；设置页提供“下载翻译资源包”按钮。
- 下载显示进度，完成后校验固定 SHA-256、自动解压配置；安装成功后再次启动不再提示。支持把手动下载的引擎文件夹放在 EXE 旁离线安装。
- 发布采用软件文件白名单，不包含个人 API Key、设置、数据库、日志和论文。

下载主程序：**ZoteroMinerU-2.8.4-Windows-x64.zip**。完整解压后运行 EXE，无需 Python。翻译引擎资源包约 175 MB，首次检查或使用引擎时还会下载约 300 MB 的版面模型和字体。

主程序和 BabelDocWorker 为两个独立程序。只使用 Markdown 和 AI 总结时可暂不下载引擎。Zotero 插件仍需在 Zotero 的“工具 → 插件 → 从文件安装插件”中安装。

所有使用者自行填写 MinerU 和大模型 API Key。PDF 解析会上传勾选的 PDF 至 MinerU，总结和翻译会把相应内容发至所选大模型服务商。EXE 暂无代码签名。
