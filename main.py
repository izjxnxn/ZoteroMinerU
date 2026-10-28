from __future__ import annotations

import argparse
import json
import logging
from logging.handlers import RotatingFileHandler
import os
from pathlib import Path
import sys


def main():
    parser = argparse.ArgumentParser(description="Zotero MinerU 中文 PDF 转 Markdown 工具")
    parser.add_argument("--data-dir", type=Path, help="独立配置目录（测试用，扩展进程需设置同一环境变量）")
    parser.add_argument("--smoke-test", type=Path, help="保存界面截图后自动退出，不启动云端解析")
    args = parser.parse_args()
    if args.data_dir:
        os.environ["MINERU_ZOTERO_HOME"] = str(args.data_dir.resolve())
    from PySide6.QtCore import QLockFile, QTimer, Qt
    from PySide6.QtWidgets import QApplication, QMessageBox
    from zotero_mineru.config import Config
    from zotero_mineru.gui import STYLE, Window
    app = QApplication(sys.argv[:1])
    app.styleHints().setColorScheme(Qt.ColorScheme.Light)
    app.setApplicationName("ZoteroMinerU")
    app.setQuitOnLastWindowClosed(False)
    app.setStyle("Fusion")
    app.setStyleSheet(STYLE)
    # The Windows offscreen plugin has no system font database; use installed fonts for QA.
    if args.smoke_test:
        from PySide6.QtGui import QFontDatabase
        for name in ("msyh.ttc", "segoeui.ttf"):
            font = Path(os.environ.get("WINDIR", "C:/Windows")) / "Fonts" / name
            if font.is_file():
                QFontDatabase.addApplicationFont(str(font))
    try:
        config = Config()
        lock = QLockFile(str(config.home / "application.lock"))
        lock.setStaleLockTime(0)
        if not lock.tryLock(0):
            QMessageBox.information(None, "软件已运行", "转换软件已在运行。请从系统托盘打开，避免重复上传。")
            return 0
        config.save()
    except Exception as error:
        QMessageBox.critical(None, "配置加载失败", str(error))
        return 1
    handler = RotatingFileHandler(config.home / "application.log", maxBytes=2 * 1024 * 1024, backupCount=2, encoding="utf-8")
    logging.basicConfig(level=logging.INFO, handlers=[handler], format="%(asctime)s %(message)s")
    window = Window(config, smoke=bool(args.smoke_test))
    window.show()
    if args.smoke_test:
        def finish():
            target = args.smoke_test.resolve()
            target.parent.mkdir(parents=True, exist_ok=True)
            window.tabs.setCurrentIndex(0)
            expanded = 0
            for n in range(window.table.topLevelItemCount()):  # show the Zotero-like dropdown in the screenshot
                item = window.table.topLevelItem(n)
                if item.childCount() >= 2 and expanded < 2:
                    item.setExpanded(True)
                    expanded += 1
            app.processEvents()
            window.grab().save(str(target))
            for index, suffix in ((1, "research"), (2, "settings"), (3, "help")):
                window.tabs.setCurrentIndex(index)
                app.processEvents()
                window.grab().save(str(target.with_name(target.stem + "-" + suffix + target.suffix)))
            window.tabs.setCurrentIndex(2)  # the settings page scrolled to the bilingual-PDF section
            window.settings_page.ensureWidgetVisible(window.translate_engine_label, 0, 220)
            app.processEvents()
            window.grab().save(str(target.with_name(target.stem + "-translate" + target.suffix)))
            target.with_suffix(".json").write_text(json.dumps({
                "title": window.windowTitle(), "collection_roots": window.tree.topLevelItemCount(),
                "pdf_rows": window.table.topLevelItemCount(), "connection": window.connection.text(),
                "token_masked": window.token.echoMode().name, "worker_running": window.engine.is_alive(),
                "packaged_executable": bool(getattr(sys, "frozen", False)),
                "embedded_xpi": (Path(getattr(sys, "_MEIPASS", Path(__file__).resolve().parent)) / "resources/mineru-zotero-bridge.xpi").is_file(),
            }, ensure_ascii=False, indent=2), encoding="utf-8")
            window.quit_app()
        QTimer.singleShot(4500, finish)
    result = app.exec()
    threads = (window.engine, window.summarizer, window.researcher, window.translator)
    for thread in threads:
        thread.close()
    for thread in threads:
        thread.join(timeout=3)
    if not any(thread.is_alive() for thread in threads):
        window.store.close()
    lock.unlock()
    return result


if __name__ == "__main__":
    raise SystemExit(main())
