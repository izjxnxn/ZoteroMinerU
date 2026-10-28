"""LLM interface (OpenAI-compatible + Anthropic), deep-reading workflow and the summary worker."""
from __future__ import annotations

import base64
import json
import tempfile
import threading
import time
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from test_core import Harness
from zotero_mineru.clients import ServiceError, is_markdown_attachment, is_summary_attachment
from zotero_mineru.llm import ContextTooLong, LLMClient
from zotero_mineru.summarizer import INDEX_JSONL, INDEX_MD, Summarizer
from zotero_mineru.summary import SECTIONS, clean_markdown, parse_card, split_parts, summarize

def png_bytes(width, height):
    from PySide6.QtCore import QBuffer, QIODevice
    from PySide6.QtGui import QColor, QImage
    image = QImage(width, height, QImage.Format.Format_RGB32)
    image.fill(QColor("#2563eb"))
    buffer = QBuffer()
    buffer.open(QIODevice.OpenModeFlag.WriteOnly)
    image.save(buffer, "PNG")
    return bytes(buffer.data())


CARD = """一句话总结：采用相场法研究钢化玻璃在冲击下的碎裂。
关键词：钢化玻璃；相场；残余应力；冲击

## 研究背景
钢化玻璃碎裂机理尚不清楚。
## 研究目标
预测碎片形态。
## 研究对象
6 mm 钢化玻璃板，表面压应力 90 MPa。
## 研究方法
- 有限元相场模拟（Abaqus UEL）
## 模型与理论
$$\\psi = g(d)\\psi_0$$
## 研究内容
- 残余应力场建模
## 研究结论
- 碎片尺寸随残余应力增大而减小。
"""


class FakeLLM(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def reply(self, body, status=200):
        payload = json.dumps(body, ensure_ascii=False).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def do_GET(self):
        self.server.requests.append(("GET", self.path, dict(self.headers), None))
        self.reply({"data": [{"id": "model-b"}, {"id": "model-a"}]})

    def do_POST(self):
        body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        self.server.requests.append(("POST", self.path, dict(self.headers), body))
        script = self.server.script
        status, payload = script.pop(0) if script else (200, None)
        if payload is None:
            text = "<think>internal</think>OK"
            payload = ({"content": [{"type": "text", "text": text}], "stop_reason": "end_turn",
                        "usage": {"input_tokens": 3, "output_tokens": 1}} if self.path.endswith("/messages") else
                       {"choices": [{"message": {"content": text}, "finish_reason": "stop"}],
                        "usage": {"prompt_tokens": 3, "completion_tokens": 1}})
        self.reply(payload, status)


class LLMClientTests(unittest.TestCase):
    def setUp(self):
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), FakeLLM)
        self.server.requests, self.server.script = [], []
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.addCleanup(self.server.server_close)
        self.addCleanup(self.server.shutdown)
        self.base = f"http://127.0.0.1:{self.server.server_port}"

    def test_openai_compatible_request_and_reasoning_strip(self):
        client = LLMClient("deepseek", "sk-test", "deepseek-v4-flash", self.base + "/v1")
        text, usage = client.chat("系统", "用户", max_tokens=2000)
        self.assertEqual(text, "OK")
        self.assertEqual(usage, {"input": 3, "output": 1})
        method, path, headers, body = self.server.requests[-1]
        self.assertEqual(path, "/v1/chat/completions")
        self.assertEqual(headers["Authorization"], "Bearer sk-test")
        self.assertEqual(body["messages"][0], {"role": "system", "content": "系统"})
        self.assertEqual((body["max_tokens"], body["temperature"]), (2000, 0.2))
        self.assertEqual(client.models(), ["model-a", "model-b"])

    def test_output_unlimited_by_default_and_learns_model_ceiling(self):
        from zotero_mineru import llm
        llm.OUTPUT_CAPS.clear()
        self.server.script = [(400, {"error": {"message": "Invalid max_tokens value, the valid range of max_tokens is [1, 8192]"}})]
        client = LLMClient("deepseek", "sk-test", "deepseek-flash", self.base)
        self.assertEqual(client.chat("s", "u")[0], "OK")
        bodies = [r[3] for r in self.server.requests]
        self.assertEqual([b["max_tokens"] for b in bodies], [65536, 8192])
        LLMClient("deepseek", "sk-test", "deepseek-flash", self.base).chat("s", "u")
        self.assertEqual(self.server.requests[-1][3]["max_tokens"], 8192, "the ceiling is remembered")
        LLMClient("anthropic", "ant-key", "claude-x", self.base).chat("s", "u")
        self.assertEqual(self.server.requests[-1][3]["max_tokens"], 32000)

    def test_thinking_that_uses_the_whole_budget_is_retried_with_more_room(self):
        from zotero_mineru import llm
        llm.OUTPUT_CAPS.clear()
        self.server.script = [
            (200, {"choices": [{"message": {"content": "", "reasoning_content": "long thoughts"}, "finish_reason": "length"}],
                   "usage": {"prompt_tokens": 5, "completion_tokens": 8192}}),
        ]
        text, usage = LLMClient("deepseek", "sk-test", "deepseek-flash", self.base).chat("s", "u", max_tokens=8192)
        self.assertEqual(text, "OK")
        self.assertEqual([r[3]["max_tokens"] for r in self.server.requests], [8192, 16384])
        self.assertEqual(len(self.server.requests[-1][3]["messages"]), 2, "a fresh request, not a continuation")

    def test_large_output_budget_overflowing_context_is_reduced(self):
        from zotero_mineru import llm
        llm.OUTPUT_CAPS.clear()
        self.server.script = [(400, {"error": {"message": "This model's maximum context length is 131072 tokens. However, you "
                                               "requested 160000 tokens (94464 in the messages, 65536 in the completion)."}})]
        LLMClient("qwen", "sk-test", "qwen-plus", self.base).chat("s", "u")
        self.assertEqual(self.server.requests[-1][3]["max_tokens"], 131072 - 94464 - 256)

    def test_adapts_to_rejected_parameters(self):
        self.server.script = [
            (400, {"error": {"message": "Unsupported parameter: 'max_tokens' is not supported with this model. Use 'max_completion_tokens' instead."}}),
            (400, {"error": {"message": "Unsupported value: 'temperature' does not support 0.2"}}),
        ]
        client = LLMClient("custom", "k", "some-model", self.base)
        self.assertEqual(client.chat("s", "u")[0], "OK")
        body = self.server.requests[-1][3]
        self.assertIn("max_completion_tokens", body)
        self.assertNotIn("max_tokens", body)
        self.assertNotIn("temperature", body)

    def test_openai_gpt5_uses_max_completion_tokens(self):
        LLMClient("openai", "k", "gpt-5.6", self.base).chat("s", "u")
        body = self.server.requests[-1][3]
        self.assertIn("max_completion_tokens", body)
        self.assertNotIn("temperature", body)

    def test_truncated_answer_is_continued(self):
        self.server.script = [(200, {"choices": [{"message": {"content": "前半段"}, "finish_reason": "length"}],
                                     "usage": {"prompt_tokens": 10, "completion_tokens": 5}}),
                              (200, {"choices": [{"message": {"content": "后半段"}, "finish_reason": "stop"}],
                                     "usage": {"prompt_tokens": 12, "completion_tokens": 4}})]
        text, usage = LLMClient("qwen", "k", "qwen-plus", self.base).chat("s", "u")
        self.assertEqual(text, "前半段后半段")
        self.assertEqual(usage, {"input": 22, "output": 9})
        messages = self.server.requests[-1][3]["messages"]
        self.assertEqual([m["role"] for m in messages], ["system", "user", "assistant", "user"])

    def test_context_and_auth_errors(self):
        self.server.script = [(400, {"error": {"message": "This model's maximum context length is 65536 tokens"}})]
        with self.assertRaises(ContextTooLong):
            LLMClient("mimo", "k", "mimo-v2.5", self.base).chat("s", "u")
        self.server.script = [(401, {"error": {"message": "invalid api key sk-secret"}})]
        with self.assertRaises(ServiceError) as caught:
            LLMClient("mimo", "sk-secret", "mimo-v2.5", self.base).chat("s", "u")
        self.assertTrue(caught.exception.auth)
        self.assertNotIn("sk-secret", str(caught.exception))
        with self.assertRaises(ServiceError):
            LLMClient("deepseek", "", "x", self.base)

    def test_images_sent_in_both_formats_and_text_only_fallback(self):
        from zotero_mineru import llm
        llm.TEXT_ONLY.clear()
        image = {"label": "[图 1]", "mime": "image/jpeg", "data": "QUJD"}
        LLMClient("qwen", "sk-test", "qwen3-vl-plus", self.base).chat("s", "正文", images=[image])
        content = self.server.requests[-1][3]["messages"][1]["content"]
        self.assertEqual(content[0], {"type": "text", "text": "正文"})
        self.assertEqual(content[2], {"type": "text", "text": "[图 1]"})
        self.assertEqual(content[3], {"type": "image_url", "image_url": {"url": "data:image/jpeg;base64,QUJD"}})
        LLMClient("anthropic", "ant-key", "claude-x", self.base).chat("s", "正文", images=[image])
        blocks = self.server.requests[-1][3]["messages"][0]["content"]
        self.assertEqual(blocks[3], {"type": "image", "source": {"type": "base64", "media_type": "image/jpeg", "data": "QUJD"}})
        # A text-only model (DeepSeek-style error) is remembered and the request is retried without pictures.
        self.server.script = [(400, {"error": {"message": "Failed to deserialize the JSON body: messages[1]: unknown variant `image_url`, expected `text`"}})]
        client = LLMClient("deepseek", "sk-test", "deepseek-flash", self.base)
        text, _ = client.chat("s", "正文", images=[image])
        self.assertEqual(text, "OK")
        self.assertEqual(self.server.requests[-1][3]["messages"][1]["content"], "正文")
        self.assertFalse(client.sees_images)
        LLMClient("deepseek", "sk-test", "deepseek-flash", self.base).chat("s", "再来", images=[image])
        self.assertEqual(self.server.requests[-1][3]["messages"][1]["content"], "再来", "no second failed attempt")

    def test_anthropic_messages_protocol(self):
        self.server.script = [(200, {"content": [{"type": "text", "text": "第一段"}], "stop_reason": "max_tokens",
                                     "usage": {"input_tokens": 7, "output_tokens": 2}}), (200, None)]
        client = LLMClient("anthropic", "ant-key", "claude-sonnet-5-5", self.base)
        text, usage = client.chat("系统提示", "问题", max_tokens=4000)
        self.assertEqual(text, "第一段OK")
        method, path, headers, body = self.server.requests[0]
        self.assertEqual(path, "/v1/messages")
        self.assertEqual(headers["x-api-key"], "ant-key")
        self.assertEqual(headers["anthropic-version"], "2023-06-01")
        self.assertEqual((body["system"], body["max_tokens"]), ("系统提示", 4000))
        self.assertEqual(body["messages"], [{"role": "user", "content": "问题"}])
        self.assertEqual([m["role"] for m in self.server.requests[1][3]["messages"]], ["user", "assistant", "user"])


class ScriptedClient:
    display = "测试服务 · test-model"

    def __init__(self, draft=CARD, context_limit=None):
        self.prompts, self.draft, self.context_limit = [], draft, context_limit
        self.images = []

    def chat(self, system, prompt, *, max_tokens=8192, temperature=0.2, images=None):
        from zotero_mineru.summary import estimate_tokens
        if self.context_limit and estimate_tokens(prompt) > self.context_limit:
            raise ContextTooLong("too long")
        self.prompts.append(prompt)
        self.images.append([image["label"] for image in images or []])
        if "<part>" in prompt:
            return f"- 笔记 {len(self.prompts)}", {"input": 10, "output": 2}
        if "缺少或改动了这些部分" in prompt:
            return CARD, {"input": 10, "output": 2}
        if "<draft>" in prompt:
            return CARD.replace("碎片尺寸", "碎片尺寸（已核对）"), {"input": 10, "output": 2}
        return self.draft, {"input": 10, "output": 2}


PAPER = "\n\n".join(f"# Section {n}\n\n" + ("Tempered glass fracture text. " * 400) for n in range(6)) \
        + "\n\n# References\n\n[1] Some reference.\n\n# Appendix A\n\nAppendix model details.\n"
META = {"title": "Tempered glass", "authors": ["A Liu"], "year": "2024", "journal": "J", "key": "PARENT01"}


class WorkflowTests(unittest.TestCase):
    def test_deep_reading_reads_the_whole_paper_twice(self):
        client = ScriptedClient()
        card, usage = summarize(client, META, PAPER, depth="deep")
        self.assertEqual(usage["calls"], 2, "write from the full text, then check against the full text")
        self.assertFalse(any("<part>" in p for p in client.prompts), "a paper that fits is never split")
        cleaned = clean_markdown(PAPER)
        self.assertIn(cleaned, client.prompts[0])
        self.assertIn("逐节精读全文", client.prompts[0])
        self.assertIn("<draft>", client.prompts[1])
        self.assertIn(cleaned, client.prompts[1])
        self.assertIn("已核对", card["sections"]["研究结论"])
        self.assertNotIn("Some reference", "".join(client.prompts), "reference list is not sent")
        self.assertIn("Appendix model details", client.prompts[0])

    def test_paper_longer_than_input_cap_is_read_in_parts(self):
        client = ScriptedClient()
        card, usage = summarize(client, META, PAPER, depth="deep", part_tokens=4000, budget=4000)
        parts = split_parts(clean_markdown(PAPER), 4000)
        notes = [p for p in client.prompts if "<part>" in p]
        self.assertEqual(len(notes), len(parts))
        self.assertIn("<previous_notes>", notes[1], "later parts carry the previous part's notes")
        self.assertIn("<paper_notes>", client.prompts[len(parts)])
        self.assertIn("<paper_notes>", client.prompts[-1])
        self.assertEqual(usage["calls"], len(parts) + 2)

    def test_quick_reading_is_one_call(self):
        client = ScriptedClient()
        card, usage = summarize(client, META, PAPER, depth="quick", budget=60000)
        self.assertEqual(usage["calls"], 1)
        self.assertEqual(card["keywords"], ["钢化玻璃", "相场", "残余应力", "冲击"])

    def test_format_repair_and_context_fallback(self):
        client = ScriptedClient(draft="一句话总结：x\n## 研究背景\n只有背景")
        summarize(client, META, PAPER, depth="quick", budget=60000)
        self.assertTrue(any("缺少或改动了这些部分" in p for p in client.prompts))
        small = ScriptedClient(context_limit=5000)
        card, usage = summarize(small, META, PAPER, depth="deep")
        self.assertEqual(set(card["sections"]), set(SECTIONS["zh"]))
        self.assertTrue(all("<paper>" not in p for p in small.prompts if "<paper_notes>" in p))

    def test_parse_tolerates_common_model_styles(self):
        styled = CARD.replace("## 研究背景", "### 1. 研究背景").replace("## 研究目标", "**研究目标**")
        card, missing = parse_card("```markdown\n" + styled + "\n```", "zh")
        self.assertEqual(missing, [])
        self.assertIn("6 mm", card["sections"]["研究对象"])

    def test_figures_numbered_shrunk_and_sent_with_the_paper(self):
        from zotero_mineru.summary import extract_figures
        big, small = png_bytes(1600, 900), png_bytes(40, 30)
        folder = Path(tempfile.mkdtemp())
        (folder / "images").mkdir()
        (folder / "images" / "c.png").write_bytes(png_bytes(500, 400))
        md = (
            "# T\n\n![](data:image/png;base64," + base64.b64encode(big).decode() + ")\nFig. 1 caption.\n\n"
            "logo ![](data:image/png;base64," + base64.b64encode(small).decode() + ")\n\n"
            "again ![](data:image/png;base64," + base64.b64encode(big).decode() + ")\n\n![](images/c.png)\nFig. 2.\n")
        text, figures = extract_figures(md, "zh", 30, folder)
        self.assertEqual([f["label"] for f in figures], ["[图 1]", "[图 2]"], "logo skipped, repeated figure sent once")
        self.assertIn("[图 1]\nFig. 1 caption.", text)
        self.assertIn("logo [图]", text)
        self.assertIn("again [图 1]", text)
        self.assertIn("[图 2]\nFig. 2.", text)
        self.assertNotIn("base64", text)
        from PySide6.QtGui import QImage
        shrunk = QImage()
        shrunk.loadFromData(base64.b64decode(figures[0]["data"]))
        self.assertEqual((shrunk.width(), figures[0]["mime"]), (1024, "image/jpeg"), "downscaled to 1024 px")
        self.assertEqual(len(extract_figures(md, "zh", 1, folder)[1]), 1, "per-paper limit")

        client = ScriptedClient()
        paper = md.replace("# T", PAPER)
        card, usage = summarize(client, META, paper, depth="deep", vision=True, base_dir=folder)
        self.assertEqual(client.images, [["[图 1]", "[图 2]"], ["[图 1]", "[图 2]"]], "write and check both see the figures")
        self.assertIn("[图 1]", client.prompts[0])
        self.assertIn("编号与正文中的 [图 N] 标记对应", client.prompts[0])
        self.assertEqual(usage["images"], 2)
        plain = ScriptedClient()
        summarize(plain, META, paper, depth="deep")
        self.assertEqual(plain.images, [[], []], "off by default")
        parted = ScriptedClient()
        summarize(parted, META, paper, depth="deep", vision=True, base_dir=folder, budget=4000, part_tokens=4000)
        sent = [labels for labels, prompt in zip(parted.images, parted.prompts) if "<part>" in prompt and labels]
        self.assertEqual(sorted(label for labels in sent for label in labels), ["[图 1]", "[图 2]"],
                         "long papers: each part carries the figures that appear in it")

    def test_word_count_and_length_limit(self):
        from zotero_mineru.summary import card_words, count_words, fit_card, parse_card
        self.assertEqual(count_words("钢化玻璃 tempered glass 90 MPa"), 8, "Word-style: each Han character and each word")
        card, _ = parse_card(CARD, "zh")
        long_card = {**card, "sections": {**card["sections"], "研究背景": "钢化玻璃断裂研究。" * 400}}
        self.assertGreater(card_words(long_card), 3000)
        prompts = []
        fitted = fit_card(lambda prompt: prompts.append(prompt) or CARD, long_card, "zh", 3000)
        self.assertIn("超过了 3000 字的上限", prompts[0])
        self.assertLessEqual(card_words(fitted), 3000)
        stubborn = fit_card(lambda prompt: CARD.replace("钢化玻璃碎裂机理尚不清楚。", "钢化玻璃断裂研究。" * 400),
                            long_card, "zh", 3000)
        self.assertLessEqual(card_words(stubborn), 3000, "trimmed when the model will not compress")
        self.assertEqual(set(stubborn["sections"]), set(SECTIONS["zh"]))

    def test_prompts_carry_the_word_limit(self):
        client = ScriptedClient()
        summarize(client, META, PAPER, depth="deep", max_words=3000)
        self.assertIn("不超过 3000 字", client.prompts[0])
        self.assertIn("仍不得超过 3000 字", client.prompts[1])

    def test_summary_attachment_is_not_full_text(self):
        self.assertTrue(is_summary_attachment({"filename": "SUM_Liu 等_x.md", "contentType": "text/markdown"}))
        self.assertFalse(is_markdown_attachment({"filename": "SUM_Liu 等_x.md", "contentType": "text/markdown"}))
        self.assertTrue(is_markdown_attachment({"filename": "MD_Liu 等_x.md", "contentType": "text/markdown"}))
        self.assertFalse(is_summary_attachment({"filename": "SUM_x.pdf", "contentType": "application/pdf"}))


class SettingsMigrationTests(unittest.TestCase):
    def test_old_length_caps_become_automatic(self):
        from zotero_mineru.config import Config
        with tempfile.TemporaryDirectory() as folder:
            (Path(folder) / "settings.json").write_text(json.dumps(
                {"summary_max_output": 8192, "summary_budget": 60000, "summary_part_tokens": 8000}), encoding="utf-8")
            config = Config(Path(folder))
            self.assertEqual((config.data["summary_max_output"], config.data["summary_budget"]), (0, 0))
            config.data["summary_max_output"] = 16000
            config.save()
            self.assertEqual(Config(Path(folder)).data["summary_max_output"], 16000, "a choice made in 2.3 is kept")


class SummarizerTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        self.h = Harness(self.root)
        self.addCleanup(self.h.close)
        self.client = ScriptedClient()
        settings = {**self.h.settings, "llm_provider": "deepseek", "llm_bases": {}, "llm_models": {},
                    "summary_language": "zh", "summary_depth": "deep", "summary_part_tokens": 4000,
                    "summary_budget": 60000, "summary_max_output": 8192, "summary_note": True, "summary_auto": True}
        self.events = []
        port = self.h.server.server_port
        from zotero_mineru.clients import ZoteroClient
        self.worker = Summarizer(self.h.store, settings, {"deepseek": "sk-test"}, self.h.state["bridge_token"],
                                 self.root / "work", lambda kind, data: self.events.append((kind, data)),
                                 zotero_factory=lambda _port, token: ZoteroClient(port, token),
                                 llm_factory=lambda *args: self.client)
        self.h.paper("PARENT01", "Tempered glass fracture")
        self.h.pdf("PDFKEY01", "PARENT01", "a.pdf")
        md = self.root / "storage" / "MDKEY001" / "MD_Liu 等_Tempered glass fracture.md"
        md.parent.mkdir(parents=True)
        md.write_text(PAPER, encoding="utf-8")
        self.h.markdown("MDKEY001", "PARENT01", md)

    def drain(self):
        for _ in range(20):
            self.worker.process_one()
            if not any(j["stage"] in ("queued", "attach") for j in self.h.store.summaries().values()):
                return

    def test_summary_card_attached_with_note_and_index(self):
        self.worker.handle("enqueue", [("PARENT01", "Tempered glass fracture")])
        self.drain()
        job = self.h.store.summaries()["PARENT01"]
        self.assertEqual(job["stage"], "done", job["error"])
        request = self.h.state["attach_requests"][-1]
        self.assertEqual(request["kind"], "summary")
        self.assertEqual(Path(request["file"]).name, "SUM_Liu 等_Tempered glass fracture.md")
        stored = next((self.root / "storage").glob("MDRES*/SUM_*.md")).read_text(encoding="utf-8")
        self.assertTrue(stored.startswith("---\ntitle: \"Tempered glass fracture\""))
        for name in SECTIONS["zh"]:
            self.assertIn("## " + name, stored)
        self.assertIn('zotero_key: "PARENT01"', stored)
        self.assertEqual(self.h.state["notes"][0]["parent"], "PARENT01")
        self.assertIn("<h2>研究背景</h2>", self.h.state["notes"][0]["html"])
        index = (self.root / "output" / INDEX_MD).read_text(encoding="utf-8")
        self.assertIn("采用相场法研究钢化玻璃", index)
        line = json.loads((self.root / "output" / INDEX_JSONL).read_text(encoding="utf-8").splitlines()[0])
        self.assertEqual(line["zotero_key"], "PARENT01")
        self.assertTrue(line["summary_file"].endswith(".md"))
        self.assertFalse(any((self.root / "work" / "summaries").glob("*/*")), "work files cleaned")
        # Already summarised: a second request does not call the model again.
        calls = len(self.client.prompts)
        self.h.store.update_summary("PARENT01", stage="failed")
        self.worker.handle("enqueue", [("PARENT01", "Tempered glass fracture")])
        self.drain()
        self.assertEqual(len(self.client.prompts), calls)
        self.assertIn("已有 AI 总结", self.h.store.summaries()["PARENT01"]["progress"])

    def test_paper_without_markdown_fails_clearly_and_cancel(self):
        self.h.paper("PARENT02", "No markdown")
        self.worker.handle("enqueue", [("PARENT02", "No markdown")])
        self.drain()
        job = self.h.store.summaries()["PARENT02"]
        self.assertEqual(job["stage"], "failed")
        self.assertIn("Markdown", job["error"])
        self.h.paper("PARENT03", "Queued")
        self.worker.handle("enqueue", [("PARENT03", "Queued")])
        self.assertEqual(self.worker.cancel(), 1)
        self.assertNotIn("PARENT03", self.h.store.summaries())

    def blocked_reading(self):
        """Start reading PARENT01 (PARENT02 queued behind it) on a thread whose model call waits for `release`."""
        started, release = threading.Event(), threading.Event()
        inner = self.client

        class Blocking:
            display = inner.display

            def chat(self, *args, **kwargs):
                started.set()
                release.wait(10)
                return inner.chat(*args, **kwargs)

        self.client = Blocking()
        self.worker.handle("enqueue", [("PARENT01", "Tempered glass fracture")])
        time.sleep(0.01)
        self.worker.handle("enqueue", [("PARENT02", "Queued behind")])
        thread = threading.Thread(target=self.worker.process_one)
        thread.start()
        self.assertTrue(started.wait(10))
        self.assertEqual(self.worker.reading, "PARENT01")
        self.assertEqual(self.worker.reading_steps[:2], (0, 2), "deep reading plans two model calls")
        self.assertTrue(1 <= self.worker.reading_percent() <= 10)
        return inner, release, thread

    def logs(self):
        return [str(data) for kind, data in self.events if kind == "log"]

    def test_stop_all_takes_effect_while_the_model_is_still_answering(self):
        inner, release, thread = self.blocked_reading()
        self.assertEqual(self.worker.cancel(current=True), 1)
        self.assertEqual(self.h.store.summaries(), {}, "both jobs are gone before the model answers")
        self.assertIn("不再继续调用大模型", self.logs()[-1])
        release.set()
        thread.join(10)
        self.assertEqual(len(inner.prompts), 1, "deep mode reads twice: nothing is sent after stopping")
        self.assertEqual(self.h.state["attach_requests"], [])
        self.assertFalse(any("失败" in line for line in self.logs()), "stopping is not reported as a failure")
        self.assertEqual(self.worker.reading, "")

    def test_finished_card_shows_at_once_instead_of_looking_deleted(self):
        self.h.engine.handle("scan", None)  # the list was read from Zotero before the card existed
        self.assertEqual(self.h.rows()["PARENT01"]["summary_status"], "未总结")
        self.worker.handle("enqueue", [("PARENT01", "Tempered glass fracture")])
        self.h.engine.publish()
        self.assertEqual(self.h.rows()["PARENT01"]["summary_status"], "排队中")
        self.drain()
        self.assertIn(("summary_attached", "PARENT01"), self.events, "the window rescans Zotero")
        self.h.engine.publish()  # what every summary event triggers, before that rescan has happened
        row = self.h.rows()["PARENT01"]
        self.assertEqual(row["summary_status"], "已有总结", "not 未总结 until some later scan")
        self.assertTrue(row["summaries"][0]["filename"].startswith("SUM_"), "the paper can no longer be ticked for it")
        card = row["summaries"][0]["key"]
        self.h.engine.handle("scan", None)
        self.assertEqual(self.h.rows()["PARENT01"]["summary_status"], "已有总结")
        self.h.state["items"][card]["data"]["deleted"] = True  # the card is deleted in Zotero afterwards
        self.h.engine.handle("scan", None)
        self.assertEqual(self.h.rows()["PARENT01"]["summary_status"], "未总结")

    def test_status_labels_and_progress_steps(self):
        from zotero_mineru.engine import summary_label
        job = lambda **values: {"stage": "queued", "progress": "", "error": "", **values}  # noqa: E731
        self.assertEqual(summary_label(job()), "排队中")
        self.assertEqual(summary_label(job(progress="正在完整阅读全文…")), "排队中", "only the paper really being read gets a bar")
        self.assertEqual(summary_label(job(progress="等待在设置中填写大模型 API Key")), "等待中")
        self.assertEqual(summary_label(job(error="大模型响应超时")), "等待重试")
        self.assertEqual(summary_label(job(stage="attach")), "添加到 Zotero")
        self.assertEqual(summary_label(job(stage="failed")), "失败，待重试")
        steps = []
        summarize(ScriptedClient(), META, PAPER, depth="deep", step=lambda done, total: steps.append((done, total)))
        self.assertEqual(steps, [(0, 2), (1, 2)])
        steps.clear()
        summarize(ScriptedClient(), META, PAPER, depth="deep", budget=4000, part_tokens=4000,
                  step=lambda done, total: steps.append((done, total)))
        self.assertGreater(len(steps), 3, "long papers are read in parts")
        self.assertEqual(steps, [(n, len(steps)) for n in range(len(steps))], "parts + card + check, planned up front")

    def test_stop_queue_only_lets_the_paper_being_read_finish(self):
        inner, release, thread = self.blocked_reading()
        self.assertEqual(self.worker.cancel(current=False), 1)
        self.assertEqual(list(self.h.store.summaries()), ["PARENT01"])
        self.assertIn("会写完并加入 Zotero", self.logs()[-1])
        release.set()
        thread.join(10)
        self.drain()
        self.assertEqual(self.h.store.summaries()["PARENT01"]["stage"], "done")
        self.assertEqual(len(self.h.state["attach_requests"]), 1)

    def test_outdated_plugin_waits_without_calling_the_model(self):
        self.h.state["protocol"] = 2
        self.worker.handle("enqueue", [("PARENT01", "Tempered glass fracture")])
        self.worker.process_one()
        job = self.h.store.summaries()["PARENT01"]
        self.assertEqual(job["stage"], "queued")
        self.assertIn("插件需要更新", job["progress"])
        self.assertEqual(self.client.prompts, [], "no tokens are spent while the card could not be attached")
        self.assertEqual(self.h.state["attach_requests"], [])

    def test_card_refused_by_old_plugin_is_reattached_from_saved_text(self):
        from zotero_mineru.summary import parse_card
        card, _ = parse_card(CARD, "zh")
        md = str(self.root / "storage" / "MDKEY001" / "MD_Liu 等_Tempered glass fracture.md")
        self.h.store.enqueue_summary("PARENT01", "Tempered glass fracture")
        # What 2.1–2.3 recorded when a 2.0 plugin answered "already has Markdown".
        self.h.store.update_summary("PARENT01", stage="done", card=card, model="DeepSeek · deepseek-flash",
                                    meta={"key": "PARENT01", "title": "Tempered glass fracture", "authors": ["A Liu"],
                                          "markdown_name": Path(md).name, "markdown_path": md, "summary_path": md},
                                    progress="条目已有 AI 总结，未重复添加")
        self.worker.repair()
        self.assertEqual(self.h.store.summaries()["PARENT01"]["stage"], "attach")
        self.drain()
        job = self.h.store.summaries()["PARENT01"]
        self.assertEqual(job["stage"], "done", job["error"])
        self.assertEqual(self.client.prompts, [], "the saved card is reused; the model is not called again")
        request = self.h.state["attach_requests"][-1]
        self.assertEqual((request["kind"], Path(request["file"]).name), ("summary", "SUM_Liu 等_Tempered glass fracture.md"))
        self.assertTrue(Path(job["meta"]["summary_path"]).name.startswith("SUM_"))
        stored = next((self.root / "storage").glob("MDRES*/SUM_*.md")).read_text(encoding="utf-8")
        self.assertIn("6 mm 钢化玻璃板", stored)
        self.assertIn('generated_by: "DeepSeek · deepseek-flash"', stored)
        index = json.loads((self.root / "output" / INDEX_JSONL).read_text(encoding="utf-8").splitlines()[0])
        self.assertTrue(Path(index["summary_file"]).name.startswith("SUM_"))

    def test_card_links_to_item_pdf_and_markdown(self):
        self.worker.handle("enqueue", [("PARENT01", "Tempered glass fracture")])
        self.drain()
        stored = next((self.root / "storage").glob("MDRES*/SUM_*.md")).read_text(encoding="utf-8")
        self.assertIn('zotero_key: "PARENT01"', stored)
        self.assertIn('pdf_zotero_key: "PDFKEY01"', stored)
        self.assertIn('pdf_link: "zotero://open-pdf/library/items/PDFKEY01"', stored)
        self.assertIn('markdown_zotero_key: "MDKEY001"', stored)
        self.assertIn("[论文 PDF](zotero://open-pdf/library/items/PDFKEY01)", stored)
        self.assertIn("[全文 Markdown](<../MDKEY001/MD_Liu 等_Tempered glass fracture.md>)", stored)
        self.assertIn("[Zotero 条目](zotero://select/library/items/PARENT01)", stored)
        self.assertIn("word_count:", stored)
        self.assertIn('href="zotero://open-pdf/library/items/PDFKEY01"', self.h.state["notes"][0]["html"])
        line = json.loads((self.root / "output" / INDEX_JSONL).read_text(encoding="utf-8").splitlines()[0])
        self.assertTrue(line["pdf_file"].endswith("a.pdf"))

    def test_old_format_card_is_refreshed_in_place_without_model(self):
        from zotero_mineru.summary import parse_card
        card, _ = parse_card(CARD, "zh")
        old = self.root / "storage" / "OLDSUM01" / "SUM_Liu 等_Tempered glass fracture.md"
        old.parent.mkdir(parents=True)
        old.write_text("old card", encoding="utf-8")
        self.h.markdown("OLDSUM01", "PARENT01", old)
        md = str(self.root / "storage" / "MDKEY001" / "MD_Liu 等_Tempered glass fracture.md")
        self.h.store.enqueue_summary("PARENT01", "Tempered glass fracture")
        self.h.store.update_summary("PARENT01", stage="done", card=card, model="DeepSeek · deepseek-flash", result_key="OLDSUM01",
                                    note_key="OLDNOTE1", meta={"key": "PARENT01", "title": "Tempered glass fracture",
                                                               "markdown_name": Path(md).name, "markdown_path": md,
                                                               "summary_path": str(old)})
        self.worker.repair()
        self.drain()
        job = self.h.store.summaries()["PARENT01"]
        self.assertEqual(job["stage"], "done", job["error"])
        self.assertEqual(self.client.prompts, [])
        request = self.h.state["attach_requests"][-1]
        self.assertEqual(request["replace_key"], "OLDSUM01")
        self.assertEqual(request["note_html"], "", "the existing note is kept, no duplicate")
        self.assertTrue(self.h.state["items"]["OLDSUM01"]["data"]["deleted"], "old card moved to the trash")
        self.assertEqual(job["note_key"], "OLDNOTE1")
        self.assertEqual(job["meta"]["pdf_key"], "PDFKEY01")
        self.assertNotIn("replace_key", job["meta"])

    def test_vision_setting_reaches_the_model_and_the_card(self):
        md = self.root / "storage" / "MDKEY001" / "MD_Liu 等_Tempered glass fracture.md"
        md.write_text(PAPER + "\n\n![](data:image/png;base64," + base64.b64encode(png_bytes(800, 600)).decode()
                      + ")\nFig. 1. Set-up.\n", encoding="utf-8")
        self.worker.settings.update(summary_vision=True, summary_vision_max=10)
        self.worker.handle("enqueue", [("PARENT01", "Tempered glass fracture")])
        self.drain()
        self.assertEqual(self.client.images[0], ["[图 1]"])
        job = self.h.store.summaries()["PARENT01"]
        self.assertEqual(job["meta"]["figures_viewed"], 1)
        stored = next((self.root / "storage").glob("MDRES*/SUM_*.md")).read_text(encoding="utf-8")
        self.assertIn("figures_viewed: 1", stored)
        self.assertIn("看图 1 张", job["progress"] + "".join(d for k, d in self.events if k == "log"))

    def test_waits_for_api_key(self):
        self.worker.keys = {}
        self.worker.handle("enqueue", [("PARENT01", "Tempered glass fracture")])
        self.worker.process_one()
        job = self.h.store.summaries()["PARENT01"]
        self.assertEqual(job["stage"], "queued")
        self.assertIn("API Key", job["progress"])
        self.assertEqual(self.client.prompts, [])

    def test_conversion_queues_summary_when_auto(self):
        h = Harness(self.root / "second") if (self.root / "second").mkdir() is None else None
        self.addCleanup(h.close)
        h.engine.settings["summary_auto"] = True
        h.paper("PAPERNEW", "Fresh paper")
        h.pdf("PDFNEW01", "PAPERNEW", "n.pdf")
        h.select("PAPERNEW")
        h.engine.handle("start", None)
        h.drain()
        self.assertEqual(h.store.summaries()["PAPERNEW"]["stage"], "queued")


if __name__ == "__main__":
    unittest.main()
