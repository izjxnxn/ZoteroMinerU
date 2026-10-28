# 数据与安全

发布包不应包含任何真实 API Key、个人 `settings.json` / `bridge.json`、数据库、日志或论文文件。用户配置位于 `%LOCALAPPDATA%\ZoteroMinerU`，与 EXE 和项目源码分开。API Key 使用当前 Windows 账户的 DPAPI 加密。

发布前运行 `python tools/audit_release.py`；在本机还可加 `--check-local-config`，仅在内存中读取已保存密钥，与源码、发布 ZIP、EXE 内嵌模块进行比对。工具只输出问题位置，不输出密钥值，也不会修改个人配置。

翻译资源包从本项目固定版本的 GitHub Release 下载，必须通过程序内固定的 SHA-256 校验。解压限制路径和大小，在临时目录完成后才启用；失败或取消不会启用半成品。离线安装时请核对 Release 中的 SHA256SUMS.txt。

如发现漏洞，先通过 GitHub 的私密漏洞报告功能联系维护者（若已启用），或提交不含密钥、个人文库及私人论文的最小复现。请勿把密钥贴到公开 Issue。若误公开密钥，应到服务商撤销并重新生成。
