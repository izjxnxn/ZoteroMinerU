"""Checkbox-driven actions in the real window, against the fake Zotero/bridge service."""
from __future__ import annotations

import os
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import Qt
from PySide6.QtWidgets import QApplication, QMessageBox

from test_core import PNG, Harness, item
from zotero_mineru.clients import MinerUClient
from zotero_mineru.config import Config
from zotero_mineru.gui import Window

APP = QApplication.instance() or QApplication([])


class CheckboxTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        (self.root / "service").mkdir()
        self.service = Harness(self.root / "service")
        self.addCleanup(self.service.close)
        config = Config(self.root / "app")
        config.token = "test-token"
        config.data.update({"collections": ["COLLECT1"], "zotero_port": self.service.server.server_port,
                            "output_root": str(self.root / "output")})
        config.save()
        self.service.state["bridge_token"] = config.pairing["token"]
        for n in range(3):
            key = f"LEGACY0{n}"
            self.service.paper(key, f"Legacy {n}")
            self.service.pdf(f"PDFLEG0{n}", key, f"{n}.pdf")
            folder = self.root / "old" / key / ("f" * 64)
            (folder / "images").mkdir(parents=True)
            (folder / "images" / "a.png").write_bytes(PNG)
            (folder / "full.md").write_text("![](images/a.png)", encoding="utf-8")
            (folder / "conversion.json").write_text("{}", encoding="utf-8")
            self.service.markdown(f"OLDMD00{n}", key, folder / "full.md", link_mode="linked_file")
        self.service.paper("DONEMD01", "Has Markdown")
        self.service.pdf("PDFDONE1", "DONEMD01", "d.pdf")
        self.service.markdown("USERMD01", "DONEMD01", self.root / "user.md")
        self.service.markdown("USERSUM1", "DONEMD01", self.root / "SUM_user.md")
        self.service.pdf("PDFDUAL1", "DONEMD01", "双语_Liu 等_Has Markdown.pdf")
        self.service.paper("NOTRANS1", "Markdown and summary, not translated")
        self.service.pdf("PDFNOTR1", "NOTRANS1", "t.pdf")
        self.service.markdown("USERMD03", "NOTRANS1", self.root / "MD_user3.md")
        self.service.markdown("USERSUM3", "NOTRANS1", self.root / "SUM_user3.md")
        self.service.paper("NOSUMMRY", "Markdown without summary")
        self.service.pdf("PDFNOSUM", "NOSUMMRY", "n.pdf")
        self.service.markdown("USERMD02", "NOSUMMRY", self.root / "MD_user2.md")
        self.window = Window(config, smoke=True)
        self.addCleanup(self.close_window)
        self.wait(lambda: len(self.window._rows) == 6)

    def close_window(self):
        self.window.quit_app()
        for thread in (self.window.engine, self.window.summarizer, self.window.researcher, self.window.translator):
            thread.join(timeout=5)
            self.assertFalse(thread.is_alive(), "background thread did not stop before closing the test database")
        self.window.store.close()

    def wait(self, predicate, seconds=15):
        end = time.monotonic() + seconds
        while time.monotonic() < end:
            APP.processEvents()
            if predicate():
                return
            time.sleep(0.05)
        self.fail("timed out")

    def row(self, key):
        return next(n for n, r in enumerate(self.window._rows) if r["key"] == key)

    def test_installed_engine_suppresses_startup_download_prompt(self):
        with mock.patch("zotero_mineru.gui.engine_command", return_value=["installed.exe"]), \
                mock.patch.object(QMessageBox, "question") as question:
            self.window.offer_engine_download()
        question.assert_not_called()

    def test_missing_engine_offers_download_and_manual_button_works(self):
        with mock.patch("zotero_mineru.gui.engine_command", return_value=None), \
                mock.patch.object(QMessageBox, "question", return_value=QMessageBox.StandardButton.Yes), \
                mock.patch.object(self.window, "install_engine") as install:
            self.window.offer_engine_download()
        install.assert_called_once()
        self.assertEqual(self.window.download_engine_button.text(), "下载翻译资源包")

    def test_resource_download_updates_progress_and_reenables_button(self):
        self.window.download_engine_button.setEnabled(False)
        self.window.handle_event("engine_download", "正在下载：12 MB")
        self.assertIn("12 MB", self.window.translate_engine_label.text())
        self.window.handle_event("engine_download_done", "已安装")
        self.assertTrue(self.window.download_engine_button.isEnabled())

    def test_summaries_use_checkboxes_not_row_selection(self):
        table = self.window.table
        done = table.topLevelItem(self.row("DONEMD01"))
        self.assertFalse(done.flags() & Qt.ItemFlag.ItemIsUserCheckable, "nothing left to do: no checkbox")
        self.assertEqual(self.window._rows[self.row("DONEMD01")]["translation_status"], "已有双语")
        untranslated = table.topLevelItem(self.row("NOTRANS1"))
        self.assertTrue(untranslated.flags() & Qt.ItemFlag.ItemIsUserCheckable, "can still be ticked for a bilingual PDF")
        self.assertIn("生成双语 PDF", untranslated.toolTip(0))
        self.assertTrue(table.topLevelItem(self.row("NOSUMMRY")).flags() & Qt.ItemFlag.ItemIsUserCheckable,
                        "a paper with Markdown but no summary can be ticked for summarising")
        self.assertTrue(all(table.topLevelItem(n).checkState(0) == Qt.CheckState.Unchecked
                            for n in range(table.topLevelItemCount())))
        self.assertEqual(self.window._rows[self.row("LEGACY00")]["status"], "已有 Markdown", "a 1.x result is just Markdown")
        table.topLevelItem(self.row("LEGACY00")).setCheckState(0, Qt.CheckState.Checked)
        table.topLevelItem(self.row("LEGACY02")).setCheckState(0, Qt.CheckState.Checked)
        table.setCurrentItem(table.topLevelItem(self.row("LEGACY01")))  # a highlighted row must not widen or narrow the action
        self.window.llm_key.setText("sk-test-key-123456")
        with mock.patch.object(QMessageBox, "question", return_value=QMessageBox.StandardButton.Yes), \
                mock.patch.object(self.window.summarizer, "command") as command:
            self.window.generate_summaries()
        queued = [c for c in command.call_args_list if c.args[0] == "enqueue"]
        self.assertEqual(queued, [mock.call("enqueue", [("LEGACY00", "Legacy 0"), ("LEGACY02", "Legacy 2")])])
        self.assertEqual(self.service.state["attach_requests"], [])

    def test_paper_being_read_shows_a_progress_bar(self):
        from PySide6.QtGui import QPainter, QPixmap
        from PySide6.QtWidgets import QStyleOptionViewItem
        from zotero_mineru.gui import PROGRESS_ROLE, SUMMARY_COLUMN, translation_percent
        self.assertEqual((translation_percent("翻译中 37%"), translation_percent("翻译中"), translation_percent("排队中")),
                         (37, 2, None))
        self.window.summarizer.close()  # keep the worker from touching the jobs this test sets up
        self.window.summarizer.join(timeout=5)
        self.window.store.enqueue_summary("NOSUMMRY", "Markdown without summary")
        self.window.store.enqueue_summary("LEGACY00", "Legacy 0")
        self.window.summarizer.reading = "NOSUMMRY"
        self.window.summarizer.reading_steps = (1, 2, time.monotonic())  # the first of two model calls is done
        self.window.engine.command("publish")
        self.wait(lambda: self.window._rows[self.row("NOSUMMRY")]["summary_status"] == "排队中")
        reading = self.window.table.topLevelItem(self.row("NOSUMMRY"))
        waiting = self.window.table.topLevelItem(self.row("LEGACY00"))
        self.assertTrue(reading.text(SUMMARY_COLUMN).startswith("阅读中 5"), reading.text(SUMMARY_COLUMN))
        self.assertGreaterEqual(reading.data(SUMMARY_COLUMN, PROGRESS_ROLE), 50)
        self.assertEqual(waiting.text(SUMMARY_COLUMN), "排队中", "papers still waiting say so instead of 排队/阅读中")
        self.assertIsNone(waiting.data(SUMMARY_COLUMN, PROGRESS_ROLE))
        pixmap = QPixmap(200, 24)
        painter = QPainter(pixmap)
        option = QStyleOptionViewItem()
        option.rect = pixmap.rect()
        self.window.progress_delegate.paint(painter, option,
                                            self.window.table.model().index(self.row("NOSUMMRY"), SUMMARY_COLUMN))
        painter.end()
        self.window.summarizer.reading = ""  # finished: the bar goes away on the next tick
        self.window.refresh_progress()
        self.assertEqual(reading.text(SUMMARY_COLUMN), "排队中")
        self.assertIsNone(reading.data(SUMMARY_COLUMN, PROGRESS_ROLE))

    def test_stop_summaries_confirms_then_cancels_at_once(self):
        self.window.summarizer.close()  # keep the worker from touching the jobs this test sets up
        self.window.summarizer.join(timeout=5)
        with mock.patch.object(QMessageBox, "information") as info:
            self.window.stop_summaries()
        self.assertIn("没有排队", info.call_args[0][2])
        store = self.window.store
        store.enqueue_summary("NOSUMMRY", "Markdown without summary")
        store.enqueue_summary("LEGACY00", "Legacy 0")
        self.window.summarizer.reading = "NOSUMMRY"  # as if the model were reading it right now
        with mock.patch.object(self.window, "_ask_stop", return_value="") as ask:
            self.window.stop_summaries()
        ask.assert_called_once_with("Markdown without summary", 1)
        self.assertEqual(len(store.summaries()), 2, "不停止 changes nothing")
        with mock.patch.object(self.window, "_ask_stop", return_value="queue"):
            self.window.stop_summaries()
        self.assertEqual(list(store.summaries()), ["NOSUMMRY"], "the paper being read keeps going")
        self.assertIn("写完后结束", self.window.statusBar().currentMessage())
        with mock.patch.object(self.window, "_ask_stop", return_value="all"):
            self.window.stop_summaries()
        self.assertEqual(store.summaries(), {})
        self.assertIn("已停止", self.window.statusBar().currentMessage())
        self.assertIn("不再继续调用大模型", self.window.logs.toPlainText())
        with mock.patch.object(QMessageBox, "exec", return_value=0):
            self.assertEqual(self.window._ask_stop("A paper", 3), "", "closing the dialog stops nothing")

    def test_bilingual_pdf_uses_ticked_papers_after_confirming(self):
        table = self.window.table
        for key in ("NOTRANS1", "DONEMD01"):  # DONEMD01 already has one: it cannot be ticked, so it is never sent
            table.topLevelItem(self.row(key)).setCheckState(0, Qt.CheckState.Checked)
        table.setCurrentItem(table.topLevelItem(self.row("NOSUMMRY")))  # the highlighted row must not matter
        self.window.llm_key.setText("sk-test-key-123456")
        self.wait(lambda: self.window.engine.bridge_info.get("protocol"))
        with mock.patch("zotero_mineru.gui.engine_command", return_value=["engine.exe"]), \
                mock.patch.object(self.window.translator, "command") as command, \
                mock.patch.object(self.window, "offer_plugin_update") as offer, \
                mock.patch.object(QMessageBox, "question", return_value=QMessageBox.StandardButton.Yes) as question:
            self.window.generate_translations()
        self.assertEqual(offer.call_args[0][2], "2.3", "the fake Zotero has an older plugin: offer the update first")
        self.assertIn("1 篇论文", question.call_args[0][2])
        self.assertIn("左边译文、右边原文", question.call_args[0][2])
        queued = [c for c in command.call_args_list if c.args[0] == "enqueue"]
        self.assertEqual(queued, [mock.call("enqueue", [("NOTRANS1", "Markdown and summary, not translated")])])
        with mock.patch("zotero_mineru.gui.engine_command", return_value=None), \
                mock.patch.object(QMessageBox, "warning") as warning:
            self.window.generate_translations()
        self.assertIn("BabelDocWorker", warning.call_args[0][2])

    def test_header_toggles_all_checkable_rows(self):
        old = lambda r: r["key"].startswith("LEGACY")  # noqa: E731
        self.window.header_clicked(0)
        self.assertTrue(all(r["selected"] for r in self.window._rows if old(r)))
        self.wait(lambda: all(self.window.store.selected(f"LEGACY0{n}") for n in range(3)))
        self.window.header_clicked(0)
        self.wait(lambda: not any(self.window.store.selected(f"LEGACY0{n}") for n in range(3))
                  and not any(r["selected"] for r in self.window._rows if old(r)))
        for _ in range(20):  # let the rescan triggered by the last click reach the window
            APP.processEvents()
            time.sleep(0.05)
        self.window.llm_key.setText("sk-test-key-123456")
        with mock.patch.object(QMessageBox, "question") as question, mock.patch.object(QMessageBox, "information") as info:
            self.window.generate_summaries()
        question.assert_not_called()
        self.assertIn("勾选", info.call_args[0][2])
        self.assertEqual(self.service.state["attach_requests"], [])


class ConversionPromptTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        (self.root / "service").mkdir()
        self.service = Harness(self.root / "service")
        self.addCleanup(self.service.close)
        self.config = Config(self.root / "app")
        self.config.token = "test-token"
        self.config.data.update({"collections": ["COLLECT1"], "zotero_port": self.service.server.server_port,
                                 "output_root": str(self.root / "output")})
        self.config.save()
        self.service.state["bridge_token"] = self.config.pairing["token"]
        self.service.paper("NEWPAPER", "Unconverted paper")
        self.service.pdf("NEWPDF01", "NEWPAPER", "n.pdf")
        self.service.paper("DONEPAPR", "Converted paper")
        self.service.pdf("DONEPDF1", "DONEPAPR", "d.pdf")
        self.service.markdown("DONEMD01", "DONEPAPR", self.root / "MD_done.md")

    def open_window(self, choose):
        """Open the window with the prompt answered by `choose(box, buttons)`; returns the shown texts."""
        shown = []

        port = self.service.server.server_port

        def exec_(box):
            # Keep MinerU traffic on the local fake service.
            engine = box.parent().engine
            engine.mineru_factory = lambda token: MinerUClient(token, f"http://127.0.0.1:{port}", allow_local=True)
            engine.mineru = engine.mineru_factory("test-token")
            shown.append(box.text() + "\n" + box.informativeText())
            buttons = {b.text(): b for b in box.buttons()}
            box._clicked = choose(buttons)

        with mock.patch.object(QMessageBox, "exec", exec_), \
                mock.patch.object(QMessageBox, "clickedButton", lambda box: getattr(box, "_clicked", None)):
            window = Window(self.config, smoke=False)
            window.tray.hide()
            self.addCleanup(lambda: (window.quit_app(), window.engine.join(5), window.summarizer.join(5), window.store.close()))
            end = time.monotonic() + 15
            while time.monotonic() < end and (not shown or window._prompt_pending):
                APP.processEvents()
                time.sleep(0.05)
            for _ in range(20):
                APP.processEvents()
                time.sleep(0.05)
        return window, shown

    def test_returning_offers_conversion_and_confirm_starts_switch(self):
        window, shown = self.open_window(lambda buttons: buttons["确认开启转换（推荐）"])
        self.assertEqual(len(shown), 1)
        self.assertIn("1 篇论文的 PDF 还没有转换", shown[0])
        self.assertIn("Unconverted paper", shown[0])
        self.assertNotIn("Converted paper\n", shown[0])
        end = time.monotonic() + 20
        while time.monotonic() < end and not self.service.state["attach_requests"]:
            APP.processEvents()
            time.sleep(0.05)
        self.assertEqual(len(self.service.state["batches"]), 1)
        while time.monotonic() < end and window._watching:
            APP.processEvents()
            time.sleep(0.05)
        self.assertFalse(window._watching, "switch closes itself after the batch")
        self.assertIn("自动关闭", window.statusBar().currentMessage())

    def test_not_now_converts_nothing_until_next_visit(self):
        window, shown = self.open_window(lambda buttons: buttons["暂不转换"])
        self.assertEqual(len(shown), 1)
        self.assertEqual(self.service.state["batches"], {})
        self.assertFalse(window._watching)
        self.assertFalse(window.store.selected("NEWPAPER"))
        window.engine.command("scan")  # a routine re-scan does not ask again
        for _ in range(40):
            APP.processEvents()
            time.sleep(0.05)
        self.assertEqual(len(shown), 1)


class PaperTreeTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        (self.root / "service").mkdir()
        self.service = Harness(self.root / "service")
        self.addCleanup(self.service.close)
        config = Config(self.root / "app")
        config.token = "test-token"
        config.data.update({"collections": ["COLLECT1"], "zotero_port": self.service.server.server_port,
                            "output_root": str(self.root / "output")})
        config.save()
        self.service.state["bridge_token"] = config.pairing["token"]
        self.service.paper("PARENT01", "Paper with a summary")
        self.service.pdf("PDFKEY01", "PARENT01", "a.pdf")
        md = self.root / "MD_Liu 等_Paper.md"
        md.write_text("# Paper", encoding="utf-8")
        self.service.markdown("MDKEY001", "PARENT01", md)
        self.service.markdown("SUMKEY01", "PARENT01", self.root / "SUM_Liu 等_Paper.md")
        self.service.state["items"]["NOTEKEY1"] = item("NOTEKEY1", "note", parentItem="PARENT01",
                                                       note="<div><h1>AI 论文总结：Paper</h1></div>")
        self.window = Window(config, smoke=True)
        self.addCleanup(lambda: (self.window.quit_app(), self.window.engine.join(5), self.window.summarizer.join(5),
                                 self.window.store.close()))
        self.wait(lambda: self.window.table.topLevelItemCount() == 1)

    def wait(self, predicate, seconds=15):
        end = time.monotonic() + seconds
        while time.monotonic() < end:
            APP.processEvents()
            if predicate():
                return
            time.sleep(0.05)
        self.fail("timed out")

    def test_expand_shows_files_and_delete_syncs_to_zotero(self):
        top = self.window.table.topLevelItem(0)
        self.assertEqual(top.childCount(), 4)
        labels = [top.child(n).text(2) for n in range(top.childCount())]
        self.assertEqual(labels, ["PDF 原文", "Markdown 全文", "AI 总结卡片", "笔记"])
        top.setExpanded(True)
        self.window.table.setCurrentItem(top.child(2))  # the SUM_ card

        def answer(box):
            self.assertIsNotNone(box.checkBox(), "offers to delete the AI note too")
            box._clicked = next(b for b in box.buttons() if b.text() == "移入回收站")

        with mock.patch.object(QMessageBox, "exec", answer), \
                mock.patch.object(QMessageBox, "clickedButton", lambda box: getattr(box, "_clicked", None)), \
                mock.patch.object(QMessageBox, "question", return_value=QMessageBox.StandardButton.Yes) as question:
            self.window.delete_selected()
            self.wait(lambda: self.service.state["items"]["NOTEKEY1"]["data"].get("deleted"))
            self.wait(lambda: question.called)
            self.wait(lambda: "PARENT01" in self.window.store.summaries())
        self.assertTrue(self.service.state["items"]["SUMKEY01"]["data"]["deleted"])
        self.assertFalse(self.service.state["items"]["MDKEY001"]["data"].get("deleted"))
        self.assertIn("重新生成 AI 总结", question.call_args[0][1])
        self.assertEqual(self.window.store.summaries()["PARENT01"]["stage"], "queued", "re-summary queued")
        self.wait(lambda: self.window.table.topLevelItem(0).childCount() == 2)
        self.assertTrue(self.window.table.topLevelItem(0).isExpanded(), "expansion kept across refreshes")

    def test_delete_needs_a_child_selected(self):
        self.window.table.setCurrentItem(self.window.table.topLevelItem(0))
        with mock.patch.object(QMessageBox, "information") as info:
            self.window.delete_selected()
        self.assertIn("展开", info.call_args[0][2])
        self.assertEqual(self.service.state["attach_requests"], [])


if __name__ == "__main__":
    unittest.main()


class ResearchTabTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        (self.root / "service").mkdir()
        self.service = Harness(self.root / "service")
        self.addCleanup(self.service.close)
        config = Config(self.root / "app")
        config.token = "test-token"
        config.llm_keys["deepseek"] = "sk-test"
        config.data.update({"collections": ["COLLECT1"], "zotero_port": self.service.server.server_port,
                            "output_root": str(self.root / "output")})
        config.save()
        self.service.state["bridge_token"] = config.pairing["token"]
        self.window = Window(config, smoke=True)
        self.addCleanup(self.close_window)
        self.sent = []
        self.window.researcher.command = lambda action, data=None: action != "configure" and self.sent.append(action)
        end = time.monotonic() + 15
        while time.monotonic() < end and not self.window.research_tree.topLevelItemCount():
            APP.processEvents()
            time.sleep(0.05)

    def close_window(self):
        self.window.quit_app()
        self.window.researcher.join(timeout=5)
        self.window.engine.join(timeout=5)
        self.window.store.close()

    def test_switch_needs_a_collection_then_confirms_estimate_and_starts(self):
        w = self.window
        self.assertEqual(w.tabs.tabText(1), "研究综述与追踪")
        self.assertFalse(w.config.data["research_enabled"], "off by default")
        with mock.patch.object(QMessageBox, "information") as info:
            w.toggle_research()
        info.assert_called_once()
        self.assertEqual(self.sent, [], "no collection chosen: nothing starts")
        w.research_tree.topLevelItem(0).setCheckState(0, Qt.CheckState.Checked)
        self.assertEqual(w.research_roots(), ["COLLECT1"])
        w.toggle_research()
        self.assertEqual(self.sent, ["estimate"], "the cost estimate comes first")
        w.after_estimate({"collections": [{"key": "COLLECT1", "name": "测试分类", "papers": 2, "children": 0,
                                           "input": 12000, "calls": 2}], "input": 12000, "calls": 2, "scope": 1, "model": "m"})
        self.assertTrue(w.config.data["research_enabled"])
        self.assertEqual(self.sent, ["estimate", "both"], "confirmed: review then tracking run at once")
        self.assertEqual(w.research_switch.text(), "■ 关闭研究综述与追踪")
        self.assertEqual(Config(self.root / "app").data["research_roots"], ["COLLECT1"], "the choice is saved")
        w.toggle_research()
        self.assertFalse(w.config.data["research_enabled"])
        self.assertEqual(w.research_switch.text(), "▶ 开启研究综述与追踪")

    def test_status_rows_follow_the_collection_tree(self):
        w = self.window
        w.config.data["research_roots"] = ["COLLECT1"]
        w.handle_event("research", {"rows": [{"key": "COLLECT1", "name": "测试分类", "path": "测试分类", "status": "done",
                                              "error": "", "generated": time.time(), "files": {}, "papers_total": 2,
                                              "coverage": {"card": 1, "abstract": 1, "meta": 0}, "tracked": "2026-10-28",
                                              "new_papers": 3, "tracking_error": ""}],
                                     "running": "", "progress": "", "review_last": time.time(), "track_last": 0})
        row = w.research_table.topLevelItem(0)
        self.assertEqual((row.text(0), row.text(2), row.text(3), row.text(5)), ("测试分类", "1 / 1 / 0", "已生成", "3 篇（10-28）"))
        self.assertIn("上次综述", w.research_state.text())
