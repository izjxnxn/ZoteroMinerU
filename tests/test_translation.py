from __future__ import annotations

import json
import os
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

from test_core import PDF, Harness
from zotero_mineru.clients import ZoteroClient, is_bilingual_attachment
from zotero_mineru.config import DEFAULTS
from zotero_mineru.engine import translation_label
from zotero_mineru.translation import NO_ENGINE, OLD_PLUGIN, Translator, outcome, pdf_filename, translation_model

# Stands in for BabelDocWorker: reads the job, reports progress and writes the PDFs the real engine would write.
FAKE_ENGINE = r'''
import json, os, sys, time
from pathlib import Path
job = json.loads(Path(sys.argv[sys.argv.index("--job") + 1]).read_text(encoding="utf-8"))
mode = os.environ.get("FAKE_ENGINE_MODE", "ok")
def emit(**data):
    print(json.dumps(data, ensure_ascii=False), flush=True)
emit(type="progress", percent=10, stage="识别版面（1/2）")
if mode == "slow":
    time.sleep(60)
if mode == "error":
    emit(type="error", message="这是扫描版 PDF（页面是图片，没有文字层），保留排版翻译暂不支持。", retryable=False)
    sys.exit(1)
if mode == "crash":
    sys.exit(3)
out = Path(job["output_dir"])
(out / job["name"]).write_bytes(b"%PDF-1.7 bilingual")
mono = ""
if job.get("mono"):
    mono = str(out / job["mono_name"])
    Path(mono).write_bytes(b"%PDF-1.7 mono")
Path(job["output_dir"], "seen.json").write_text(json.dumps({"key": os.environ.get("ZMT_LLM_KEY"), "job": job},
                                                           ensure_ascii=False), encoding="utf-8")
emit(type="done", dual=str(out / job["name"]), mono=mono, seconds=95, model="DeepSeek · deepseek-v4-flash",
     usage={"calls": 12, "input": 30000, "output": 9000}, paragraphs={"total": 40, "ok": 38, "fallback": 2},
     pages=2, broken_chars=0, cjk_chars=900, untranslated_pages=[2] if mode == "warn" else [], missing_glyphs=[],
     glossary_error="")
'''


class HelperTests(unittest.TestCase):
    def test_file_names_follow_the_md_and_sum_scheme(self):
        self.assertEqual(pdf_filename("Liu 等_Phase-field: a review"), "双语_Liu 等_Phase-field a review.pdf")
        self.assertEqual(pdf_filename("Liu 等_Title", "译文_"), "译文_Liu 等_Title.pdf")
        self.assertLessEqual(len(pdf_filename("x" * 400)), 184)

    def test_model_defaults_to_the_summary_model(self):
        settings = dict(DEFAULTS, llm_provider="deepseek", llm_models={"deepseek": "deepseek-v4-pro"},
                        llm_bases={"qwen": "https://proxy.example/v1"})
        self.assertEqual(translation_model(settings), ("deepseek", "deepseek-v4-pro", ""))
        settings.update(translate_provider="qwen", translate_model="qwen-flash")
        self.assertEqual(translation_model(settings), ("qwen", "qwen-flash", "https://proxy.example/v1"))
        settings.update(translate_model="")
        self.assertEqual(translation_model(settings)[1], "qwen-plus", "provider's first suggested model")

    def test_the_column_shows_the_real_state(self):
        def label(stage="queued", progress="", error=""):
            return translation_label({"stage": stage, "progress": progress, "error": error})
        self.assertEqual(label(), "排队中")
        self.assertEqual(label(progress="启动翻译引擎（DeepSeek · deepseek-flash，25 页）…"), "翻译中")
        self.assertEqual(label(progress="翻译中 37%：翻译段落（120/445）"), "翻译中 37%")
        self.assertEqual(label(progress="等待在设置中填写大模型 API Key"), "等待中")
        self.assertEqual(label(stage="attach", progress="已生成（25 页…）"), "添加到 Zotero")
        self.assertEqual(label(stage="attach", progress="双语 PDF 已生成（x.pdf），" + OLD_PLUGIN), "待更新插件")
        self.assertEqual(label(stage="failed", progress="翻译中 90%", error="x"), "失败，待重试")

    def test_outcome_lists_what_to_check(self):
        line, warnings = outcome({"pages": 25, "seconds": 300, "usage": {"calls": 60, "input": 90000, "output": 40000},
                                  "paragraphs": {"total": 100, "fallback": 20}, "untranslated_pages": [3, 9],
                                  "missing_glyphs": ["ꙮ"], "broken_chars": 2, "glossary_error": ""})
        self.assertIn("25 页", line)
        self.assertIn("约 5 分钟", line)
        self.assertEqual(len(warnings), 4)
        self.assertIn("第 3、9 页疑似没有翻译", warnings)
        self.assertEqual(outcome({"pages": 2, "paragraphs": {"total": 40, "fallback": 2}})[1], [])


sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "translate_worker"))
import worker  # noqa: E402  (the engine's pure helpers; BabelDOC itself is not needed here)


def cmap_entries(cmap: str) -> dict[int, str]:
    """CID → text of a ToUnicode CMap, as a PDF reader resolves it."""
    import re
    result = {}
    for block in re.findall(r"beginbfchar(.*?)endbfchar", cmap, re.DOTALL):
        for source, target in re.findall(r"(?m)^\s*<([0-9a-f]+)>\s*<([0-9a-f]+)>\s*$", block):
            result[int(source, 16)] = bytes.fromhex(target).decode("utf-16-be")
    for block in re.findall(r"beginbfrange(.*?)endbfrange", cmap, re.DOTALL):
        for low, high, start in re.findall(r"(?m)^\s*<([0-9a-f]+)>\s*<([0-9a-f]+)>\s*<([0-9a-f]+)>\s*$", block):
            for n in range(int(high, 16) - int(low, 16) + 1):
                result[int(low, 16) + n] = chr(int(start, 16) + n)
    return result


class TextLayerTests(unittest.TestCase):
    CMAP = """/CIDInit /ProcSet findresource begin
begincmap
1 begincodespacerange
<0000> <ffff>
endcodespacerange
3 beginbfchar
<0001> <00a0>
<0002> <f98a>
<0003> <00660069>
endbfchar
3 beginbfrange
<0010> <0012> <4e00>
<0020> <0024> <f97c>
<0030> <0031> <fa0e>
endbfrange
endcmap"""

    def test_compatibility_twins_point_at_the_ordinary_characters(self):
        before = cmap_entries(self.CMAP)
        fixed, count = worker.normalize_tounicode(self.CMAP)
        after = cmap_entries(fixed)
        self.assertEqual(after[0x02], "力", "U+F98A becomes U+529B")
        self.assertEqual(after[0x03], "fi", "ligatures keep their two letters")
        self.assertEqual([after[0x20 + n] for n in range(5)], [chr(0xF97C + n) if worker._standard(0xF97C + n) == 0xF97C + n
                                                               else chr(worker._standard(0xF97C + n)) for n in range(5)])
        self.assertTrue(all(worker._standard(ord(text)) == ord(text) for text in after.values() if len(text) == 1))
        self.assertEqual(after[0x30], "\ufa0e", "unified ideographs in the compatibility block stay")
        self.assertEqual({k: v for k, v in before.items() if worker._standard(ord(v[0])) == ord(v[0])},
                         {k: v for k, v in after.items() if k in before and worker._standard(ord(before[k][0])) == ord(before[k][0])},
                         "every other entry is unchanged")
        self.assertEqual(count, 1 + sum(worker._standard(0xF97C + n) != 0xF97C + n for n in range(5)))

    def test_a_ligature_line_does_not_turn_neighbours_into_a_huge_range(self):
        # The first parser paired "<00660066>" with the next line's "<0020>" and expanded millions of entries.
        cmap = self.CMAP.replace("<0020> <0024> <f97c>", "<0040> <0040> <00660066>\n<0020> <0024> <f97c>")
        fixed, _ = worker.normalize_tounicode(cmap)
        self.assertLess(len(fixed), len(cmap) + 400)
        self.assertIn("<0040> <0040> <00660066>", fixed)

    def test_reference_entries_start_new_paragraphs(self):
        for line in ("43. Raghu P, Rajagopal A", "[12] Smith J. A model", "  7. Lee K"):
            self.assertTrue(worker.LIST_ENTRY.match(line), line)
        for line in ("0.5 mm thick plates", "2D model of the shell", "2022;123:4094-4118.", "Fig. 3 shows"):
            self.assertFalse(worker.LIST_ENTRY.match(line), line)


class ThinkingSwitchTests(unittest.TestCase):
    def test_a_rejected_optional_field_is_dropped_and_the_call_repeated(self):
        from zotero_mineru.clients import ServiceError
        from zotero_mineru.llm import LLMClient
        client = LLMClient("deepseek", "sk-test-key-123456", "deepseek-v4-flash")
        client.extra = {"thinking": {"type": "disabled"}}
        bodies = []

        def post(path, body):
            bodies.append(dict(body))
            if "thinking" in body:
                raise ServiceError("大模型返回 HTTP 400：Unrecognized request argument supplied: thinking")
            return {"choices": [{"message": {"content": "译文"}, "finish_reason": "stop"}], "usage": {}}

        with mock.patch.object(client, "_post", post):
            self.assertEqual(client.chat("system", "text", max_tokens=100, temperature=0)[0], "译文")
            self.assertEqual(client.chat("system", "more", max_tokens=100, temperature=0)[0], "译文")
        self.assertEqual(["thinking" in b for b in bodies], [True, False, False], "dropped once, not sent again")
        self.assertEqual(bodies[1]["temperature"], 0)


class TranslatorFlowTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        self.h = Harness(self.root)
        self.addCleanup(self.h.close)
        self.h.state["protocol"] = 5
        self.engine_script = self.root / "fake_engine.py"
        self.engine_script.write_text(FAKE_ENGINE, encoding="utf-8")
        self.h.paper("PAPER001", "Phase-field fracture")
        self.original = self.h.pdf("PDFORIG1", "PAPER001", "Liu 等 - 2022 - Phase-field fracture.pdf")
        # A newer Doc2X translation-only PDF beside the original must never be used as the source.
        self.h.pdf("PDFDOC2X", "PAPER001", "保留排版译文PDF_Liu.pdf", content=PDF + b"doc2x",
                   added="2026-02-01T00:00:00Z")
        self.events = []
        self.settings = dict(DEFAULTS, **self.h.settings, llm_provider="deepseek", translate_qps=4)

    def translator(self, command=None, **settings):
        port = self.h.server.server_port
        translator = Translator(self.h.store, dict(self.settings, **settings), {"deepseek": "sk-test-key-123456"},
                                self.h.state["bridge_token"], self.root / "work", self.root / "home",
                                lambda kind, data: self.events.append((kind, data)),
                                zotero_factory=lambda _port, token: ZoteroClient(port, token),
                                command=command or (lambda: [sys.executable, str(self.engine_script)]))
        self.addCleanup(translator.close)
        return translator

    def wait(self, predicate, seconds=20):
        end = time.monotonic() + seconds
        while time.monotonic() < end:
            if predicate():
                return
            time.sleep(0.05)
        self.fail("timed out")

    def job(self):
        return self.h.store.translations().get("PAPER001")

    def test_translates_the_original_pdf_and_attaches_the_bilingual_pdf(self):
        self.h.engine.handle("scan", None)  # the list was read from Zotero before the PDF existed
        translator = self.translator(translate_mono=True)
        translator.start()
        translator.command("enqueue", [("PAPER001", "Phase-field fracture")])
        self.wait(lambda: ("translation_attached", "PAPER001") in self.events)
        requests = self.h.state["attach_requests"]
        self.assertEqual([r["kind"] for r in requests], ["translation", "translation"])
        self.assertEqual(Path(requests[0]["file"]).name, "双语_Liu 等_Phase-field fracture.pdf")
        self.assertEqual(Path(requests[1]["file"]).name, "译文_Liu 等_Phase-field fracture.pdf")
        self.assertEqual(requests[0]["mode"], "import")
        job = self.job()
        self.assertEqual(Path(job["meta"]["source"]), self.original, "the original, not the Doc2X translation")
        self.assertIn("已添加到 Zotero：双语_Liu 等_Phase-field fracture.pdf", job["progress"])
        self.assertTrue(Path(job["file"]).is_file(), "imported into Zotero storage")
        self.assertFalse((self.root / "work" / "translations" / "PAPER001").exists(), "work folder cleaned up")
        logs = " ".join(str(d) for k, d in self.events if k == "log")
        self.assertNotIn("sk-test-key-123456", logs)
        self.h.engine.publish()  # before the window's rescan: the new PDF must not look deleted (未翻译)
        row = self.h.rows()["PAPER001"]
        self.assertEqual(row["translation_status"], "已有双语")
        self.assertEqual(row["translations"][0]["filename"], "双语_Liu 等_Phase-field fracture.pdf")

    def test_engine_gets_the_key_by_environment_and_the_settings_in_the_job(self):
        translator = self.translator(translate_keep_refs=True, translate_font="sans-serif")
        with mock.patch("zotero_mineru.translation.shutil.rmtree"):  # keep the work folder to inspect it
            translator.handle("enqueue", [("PAPER001", "Phase-field fracture")])
            translator.process_one()
        seen = json.loads((self.root / "work" / "translations" / "PAPER001" / "out" / "seen.json").read_text(encoding="utf-8"))
        self.assertEqual(seen["key"], "sk-test-key-123456")
        job = seen["job"]
        self.assertNotIn("sk-test-key-123456", json.dumps(job), "the key is never written to disk")
        self.assertEqual((job["provider"], job["lang_in"], job["lang_out"], job["qps"]), ("deepseek", "en", "zh", 4))
        self.assertTrue(job["keep_references"])
        self.assertEqual(job["font"], "sans-serif")
        self.assertEqual(self.job()["stage"], "attach")

    def test_link_mode_keeps_the_pdf_in_the_output_folder(self):
        translator = self.translator(storage_mode="link")
        translator.handle("enqueue", [("PAPER001", "Phase-field fracture")])
        translator.process_one()
        translator.process_one()
        request = self.h.state["attach_requests"][0]
        self.assertEqual(request["mode"], "link")
        self.assertEqual(Path(request["file"]).parent, Path(self.settings["output_root"]))
        self.assertEqual(self.job()["stage"], "done")

    def test_scanned_pdf_error_fails_without_retrying(self):
        translator = self.translator()
        with mock.patch.dict(os.environ, {"FAKE_ENGINE_MODE": "error"}):
            translator.handle("enqueue", [("PAPER001", "Phase-field fracture")])
            translator.process_one()
        job = self.job()
        self.assertEqual(job["stage"], "failed")
        self.assertIn("扫描版", job["error"])
        self.assertEqual(self.h.state["attach_requests"], [])

    def test_engine_crash_is_retried_later(self):
        translator = self.translator()
        with mock.patch.dict(os.environ, {"FAKE_ENGINE_MODE": "crash"}):
            translator.handle("enqueue", [("PAPER001", "Phase-field fracture")])
            translator.process_one()
        job = self.job()
        self.assertEqual(job["stage"], "queued")
        self.assertIn("异常退出（代码 3）", job["error"])
        self.assertGreater(job["next_try"], time.time())

    def test_warnings_are_kept_for_the_paper_list(self):
        translator = self.translator()
        with mock.patch.dict(os.environ, {"FAKE_ENGINE_MODE": "warn"}):
            translator.handle("enqueue", [("PAPER001", "Phase-field fracture")])
            translator.process_one()
            translator.process_one()
        job = self.job()
        self.assertEqual(job["stage"], "done")
        self.assertEqual(job["meta"]["warnings"], ["第 2 页疑似没有翻译"])
        self.assertIn("需检查", job["progress"])

    def test_stop_kills_the_running_engine_and_clears_the_queue(self):
        translator = self.translator()
        translator.start()
        with mock.patch.dict(os.environ, {"FAKE_ENGINE_MODE": "slow"}):
            translator.command("enqueue", [("PAPER001", "Phase-field fracture")])
            self.wait(lambda: self.job() and "翻译中 10%" in self.job()["progress"])
            started = time.monotonic()
            translator.command("cancel")
            self.wait(lambda: self.job() is None, seconds=10)
        self.assertLess(time.monotonic() - started, 10)
        self.assertEqual(self.h.state["attach_requests"], [])

    def test_an_old_plugin_only_delays_adding_the_pdf(self):
        self.h.state["protocol"] = 3  # the user's Zotero still had plugin 2.1: translating must not wait for it
        translator = self.translator()
        translator.handle("enqueue", [("PAPER001", "Phase-field fracture")])
        translator.process_one()
        self.assertEqual(self.job()["stage"], "attach", "translated without waiting for the plugin")
        translator.process_one()
        job = self.job()
        self.assertEqual(job["stage"], "attach")
        self.assertIn("双语 PDF 已生成", job["progress"])
        self.assertIn(OLD_PLUGIN, job["progress"])
        self.assertEqual(translation_label(job), "待更新插件")
        self.assertEqual(self.h.state["attach_requests"], [])
        self.assertEqual([k for k, _ in self.events].count("translation_plugin_outdated"), 1)
        self.h.store.update_translation("PAPER001", next_try=0)
        translator.process_one()
        self.assertEqual([k for k, _ in self.events].count("translation_plugin_outdated"), 1, "asked once only")
        self.h.state["protocol"] = 5  # plugin updated: the finished PDF is added without translating again
        self.h.store.update_translation("PAPER001", next_try=0)
        translator.process_one()
        self.assertEqual(self.job()["stage"], "done")
        self.assertEqual(len(self.h.state["attach_requests"]), 1)

    def test_missing_engine_says_where_it_belongs(self):
        translator = self.translator(command=lambda: None)
        translator.handle("enqueue", [("PAPER001", "Phase-field fracture")])
        translator.process_one()
        self.assertEqual(self.job()["stage"], "failed")
        self.assertEqual(self.job()["error"], NO_ENGINE)

    def test_paper_that_already_has_a_bilingual_pdf_is_skipped(self):
        self.h.pdf("PDFDUAL1", "PAPER001", "双语_Liu 等_Phase-field fracture.pdf")
        translator = self.translator(command=lambda: [sys.executable, "-c", "raise SystemExit(9)"])
        translator.handle("enqueue", [("PAPER001", "Phase-field fracture")])
        translator.process_one()
        self.assertEqual(self.job()["stage"], "done")
        self.assertIn("已有双语 PDF", self.job()["progress"])

    def test_paper_list_shows_translations_apart_from_the_original(self):
        self.h.pdf("PDFDUAL1", "PAPER001", "双语_Liu 等_Phase-field fracture.pdf")
        paper = ZoteroClient(self.h.server.server_port, self.h.state["bridge_token"]).papers(
            ["COLLECT1"], True, [{"key": "COLLECT1", "data": {"name": "测试分类", "parentCollection": False}}])[0]
        kinds = {c["key"]: c["kind"] for c in paper["children"]}
        self.assertEqual(kinds, {"PDFORIG1": "pdf", "PDFDOC2X": "translation", "PDFDUAL1": "translation"})
        self.assertEqual([t["key"] for t in paper["translations"]], ["PDFDUAL1"], "a translation-only PDF is not bilingual")

    def test_bilingual_pdf_from_another_tool_means_already_translated(self):
        self.h.pdf("PDFDOC2B", "PAPER001", "保留排版双语PDF(译左原右)_zh_deepseek-v4-pro_doc2x-v3-2509-beta_Liu 等 - 2022.pdf")
        paper = ZoteroClient(self.h.server.server_port, self.h.state["bridge_token"]).papers(
            ["COLLECT1"], True, [{"key": "COLLECT1", "data": {"name": "测试分类", "parentCollection": False}}])[0]
        self.assertEqual([t["key"] for t in paper["translations"]], ["PDFDOC2B"])
        translator = self.translator(command=lambda: [sys.executable, "-c", "raise SystemExit(9)"])
        translator.handle("enqueue", [("PAPER001", "Phase-field fracture")])
        translator.process_one()
        self.assertEqual(self.job()["stage"], "done")
        self.assertIn("已有双语 PDF", self.job()["progress"])
        self.assertEqual(self.h.state["attach_requests"], [], "nothing is translated or added")

    def test_which_pdfs_count_as_bilingual(self):
        pdf = lambda name: {"contentType": "application/pdf", "filename": name}  # noqa: E731
        for name in ("双语_Liu 等_Phase.pdf", "保留排版双语PDF(译左原右)_zh_x.pdf", "保留排版双语PDF_zh_qwen-flash_x.pdf",
                     "中文-Bedon 等 - 2025 - Damage assessment_dual.pdf", "paper.zh.dual.pdf", "paper-dual.pdf"):
            self.assertTrue(is_bilingual_attachment(pdf(name)), name)
        for name in ("保留排版译文PDF_zh_x.pdf", "译文_Liu 等_Phase.pdf", "paper-mono.pdf",
                     "Liu 等 - 2022 - Dual-phase steel.pdf", "Liu 等 - 2022 - Phase-field fracture.pdf"):
            self.assertFalse(is_bilingual_attachment(pdf(name)), name)
        self.assertFalse(is_bilingual_attachment({"contentType": "text/markdown", "filename": "双语_x.md"}))


if __name__ == "__main__":
    unittest.main()
