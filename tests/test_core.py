from __future__ import annotations

import io
import json
import shutil
import tempfile
import threading
import unittest
import zipfile
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest import mock
from urllib.parse import parse_qs, urlparse

from zotero_mineru import engine as engine_module
from zotero_mineru.clients import MinerUClient, ServiceError, ZoteroClient, safe_message
from zotero_mineru.config import Config
from zotero_mineru.engine import Engine, choose_pdf
from zotero_mineru.markdown import (embed_images, fallback_base, join_parts, markdown_filename, markdown_from_zip,
                                    page_ranges, valid_filename)
from zotero_mineru.store import ACTIVE, Store

PNG = b"\x89PNG\r\n\x1a\nfake-image"
PDF = b"%PDF-1.7\n" + b"0" * 2048


def archive_bytes(text="# 原始正文\n\n![](images/a.png)\n\n$x^2$\n", folder=""):
    stream = io.BytesIO()
    with zipfile.ZipFile(stream, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr(folder + "full.md", text)
        zf.writestr(folder + "images/a.png", PNG)
        zf.writestr(folder + "layout.json", "{}")
    return stream.getvalue()


def item(key, item_type, **data):
    meta = {"creatorSummary": data.pop("creator", "")}
    links = {}
    if "href" in data:
        links["enclosure"] = {"href": data.pop("href")}
    return {"key": key, "meta": meta, "links": links, "data": {"key": key, "itemType": item_type, **data}}


class FakeService(BaseHTTPRequestHandler):
    """One local server standing in for the Zotero local API, the bridge plugin and MinerU."""

    def log_message(self, *args):
        pass

    def send_json(self, body, status=200):
        payload = json.dumps(body, ensure_ascii=False).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def do_GET(self):
        state = self.server.state
        url = urlparse(self.path)
        parts = url.path.strip("/").split("/")
        base = f"http://127.0.0.1:{self.server.server_port}"
        if url.path.startswith("/api/v4/extract-results/batch/"):
            if self.headers.get("Authorization") != "Bearer " + state["token"]:
                return self.send_json({"code": "A0202"}, 401)
            batch = state["batches"].get(parts[-1])
            if batch is None:
                return self.send_json({"code": -60012, "msg": "not found"})
            rows = []
            for n, entry in enumerate(batch["files"]):
                cloud = "done" if entry["uploaded"] else "waiting-file"
                if state.get("cloud_failure"):
                    cloud = "failed"
                rows.append({"data_id": entry["data_id"], "state": cloud, "err_msg": "synthetic failure",
                             "full_zip_url": f"{base}/result/{parts[-1]}/{n}.zip"})
            return self.send_json({"code": 0, "data": {"extract_result": rows}})
        if url.path.startswith("/result/"):
            batch, n = parts[1], int(parts[2].split(".")[0])
            ranges = state["batches"][batch]["files"][n].get("page_ranges", "")
            body = archive_bytes(f"# 第 {ranges or '全部'} 页\n\n![图](images/a.png)\n", folder="nested/")
            self.send_response(200)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            return self.wfile.write(body)
        if url.path == "/api/users/0/collections":
            return self.send_json(state["collections"])
        items = state["items"]
        if url.path.startswith("/api/users/0/collections/"):
            key = parts[4]
            top = {k for k, v in items.items() if key in v["data"].get("collections", [])}
            return self.send_json([v for k, v in items.items() if k in top or v["data"].get("parentItem") in top])
        if url.path == "/api/users/0/items":
            return self.send_json(list(items.values()))
        if url.path.startswith("/api/users/0/items/"):
            key = parts[4]
            if key not in items:
                return self.send_json({"error": "Not found"}, 404)
            if url.path.endswith("/children"):
                return self.send_json([v for v in items.values() if v["data"].get("parentItem") == key])
            if url.path.endswith("/file/view/url"):
                body = state["paths"][key].as_uri().encode()
                self.send_response(200)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                return self.wfile.write(body)
            return self.send_json(items[key])
        self.send_json({}, 404)

    def do_POST(self):
        state = self.server.state
        data = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        base = f"http://127.0.0.1:{self.server.server_port}"
        if self.path == "/api/v4/file-urls/batch":
            if self.headers.get("Authorization") != "Bearer " + state["token"]:
                return self.send_json({"code": "A0202"}, 401)
            batch = str(len(state["batches"]) + 1)
            state["batches"][batch] = {"files": [{**f, "uploaded": False} for f in data["files"]], "payload": data}
            return self.send_json({"code": 0, "data": {"batch_id": batch,
                                   "file_urls": [f"{base}/upload/{batch}/{n}" for n in range(len(data["files"]))]}})
        if self.path == "/mineru-bridge/v1":
            if self.headers.get("X-MinerU-Bridge-Token") != state["bridge_token"]:
                return self.send_json({"ok": False, "error": "桥接配对认证失败。"}, 403)
            if data["op"] == "status":
                return self.send_json({"ok": True, "protocol": state.get("protocol", 4), "data_directory": "C:\\Zotero",
                                       "zotero_version": "9.0.6", "boot": "test", "revision": state["revision"]})
            if data["op"] == "names":
                items = state["items"]
                return self.send_json({"ok": True, "names": {k: items[k]["meta"]["creatorSummary"] + "_" + items[k]["data"]["title"]
                                                             for k in data["keys"] if k in items}})
            if data["op"] == "attach":
                return self.attach(state, data)
            if data["op"] == "trash":
                items, trashed = state["items"], []
                for key in data["keys"]:
                    child = items.get(key)
                    if not child or child["data"].get("deleted"):
                        continue
                    if child["data"].get("parentItem") != data["parent_key"]:
                        return self.send_json({"ok": False, "error": "只能删除该论文条目下的附件或笔记。"}, 400)
                    child["data"]["deleted"] = 1
                    trashed.append(key)
                return self.send_json({"ok": True, "trashed": trashed})
        self.send_json({}, 404)

    def attach(self, state, data):
        state["attach_requests"].append(data)
        if state["fail_attach"]:
            state["fail_attach"] -= 1
            return self.send_json({"ok": False, "error": "synthetic attach failure"}, 503)
        items = state["items"]
        kind = data.get("kind", "markdown")
        summary = kind == "summary"
        content_type = "application/pdf" if kind == "translation" else "text/markdown"
        prefix = Path(data["file"]).name[:3]  # 双语_ / 译文_
        for child in list(items.values()):
            d = child["data"]
            if d.get("parentItem") != data["parent_key"] or d.get("deleted") or d.get("contentType") != content_type:
                continue
            if (d.get("filename", "").startswith(prefix) if kind == "translation"
                    else d.get("filename", "").upper().startswith("SUM_") == summary):
                if data.get("replace_key") == child["key"]:
                    d["deleted"] = 1
                elif not data.get("replace_key"):
                    return self.send_json({"ok": True, "key": child["key"], "skipped": True, "path": d.get("path", "")})
        key = "MDRES%03d" % len(state["attach_requests"])
        source = Path(data["file"])
        if data["mode"] == "import":
            target = state["root"] / "storage" / key / source.name
            target.parent.mkdir(parents=True)
            shutil.copy2(source, target)
        else:
            target = source
        items[key] = item(key, "attachment", parentItem=data["parent_key"], contentType=content_type,
                          linkMode="imported_file" if data["mode"] == "import" else "linked_file",
                          filename=target.name, title=data["title"], href=target.as_uri(),
                          path=str(target) if data["mode"] == "link" else "")
        note_key = ""
        if summary and data.get("note_html"):
            note_key = "NOTE%04d" % len(state["notes"])
            state["notes"].append({"parent": data["parent_key"], "html": data["note_html"]})
        return self.send_json({"ok": True, "key": key, "path": str(target), "skipped": False, "note_key": note_key})

    def do_PUT(self):
        state = self.server.state
        _, _, batch, n = self.path.split("/")
        state["uploads"].append(self.rfile.read(int(self.headers["Content-Length"])))
        state["upload_content_types"].append(self.headers.get("Content-Type"))
        state["batches"][batch]["files"][int(n)]["uploaded"] = True
        self.send_response(200)
        self.end_headers()


class Harness:
    def __init__(self, root: Path, **settings):
        self.root = root
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), FakeService)
        self.state = self.server.state = {
            "token": "test-token", "bridge_token": "b" * 43, "revision": 1, "root": root,
            "collections": [{"key": "COLLECT1", "data": {"name": "测试分类", "parentCollection": False}}],
            "items": {}, "paths": {}, "batches": {}, "uploads": [], "upload_content_types": [],
            "attach_requests": [], "fail_attach": 0, "notes": [],
        }
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        port = self.server.server_port
        self.settings = {"collections": ["COLLECT1"], "include_subcollections": True,
                         "model": "vlm", "language": "ch", "ocr": False, "formula": True, "table": True,
                         "interval": 60, "zotero_port": port, "storage_mode": "import", "export_copy": False,
                         "output_root": str(root / "output"), "resume_switch": False, **settings}
        self.events = []
        self.store = Store(root / "jobs.db")
        self.engine = Engine(self.store, self.settings, "test-token", self.state["bridge_token"], root / "work",
                             lambda kind, data: self.events.append((kind, data)),
                             zotero_factory=lambda _port, token: ZoteroClient(port, token),
                             mineru_factory=lambda token: MinerUClient(token, f"http://127.0.0.1:{port}", allow_local=True),
                             stable_seconds=0)

    def paper(self, key, title, creator="Liu 等", collections=("COLLECT1",)):
        self.state["items"][key] = item(key, "journalArticle", title=title, creator=creator, collections=list(collections),
                                        dateAdded="2026-01-01T00:00:00Z")

    def pdf(self, key, parent, filename, content=PDF, added="2026-01-01T00:00:01Z"):
        path = self.root / "storage" / key / filename
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(content)
        self.state["paths"][key] = path
        self.state["items"][key] = item(key, "attachment", parentItem=parent, contentType="application/pdf",
                                        linkMode="imported_file", filename=filename, title="PDF", dateAdded=added,
                                        href=path.as_uri())
        return path

    def markdown(self, key, parent, path: Path, link_mode="imported_file"):
        self.state["items"][key] = item(key, "attachment", parentItem=parent, contentType="text/markdown",
                                        linkMode=link_mode, filename=path.name, title=path.stem,
                                        path=str(path) if link_mode == "linked_file" else "", href=path.as_uri())

    def select(self, *keys):
        for key in keys:
            self.store.set_selected(key, True)

    def drain(self, rounds=200):
        for _ in range(rounds):
            self.engine.process_one()
            self.engine._maybe_finish()
            with self.store.db:
                self.store.db.execute("UPDATE jobs SET next_try=0")
            permitted = self.engine.allowed | self.engine.manual
            if not any(j["stage"] in ACTIVE and k in permitted for k, j in self.store.jobs().items()):
                return

    def rows(self):
        return {r["key"]: r for kind, data in self.events if kind == "rows" for r in data["rows"]}

    def close(self):
        self.store.close()
        self.server.shutdown()
        self.server.server_close()


class MarkdownTests(unittest.TestCase):
    def test_embed_markdown_and_html_images(self):
        files = {"images/a.png": PNG, "images/b c.jpg": b"jpeg"}
        text = ('![图1](images/a.png)\n![](<images/b c.jpg> "标题")\n![](images/b%20c.jpg)\n'
                '<img src="./images/a.png" width="300">\n![外链](https://example.com/x.png)\n![缺失](images/none.png)')
        result, embedded, missing = embed_images(text, files.get)
        self.assertEqual((embedded, missing), (2, 1))
        self.assertNotIn("](images/a.png)", result)
        self.assertIn("![图1](data:image/png;base64,", result, "each image stays at its place in the paper")
        self.assertIn('data:image/jpeg;base64,anBlZw== "标题")', result)
        self.assertIn('<img src="data:image/png;base64,', result)
        self.assertLess(result.index("![图1](data:"), result.index("![外链]"), "order of the paper is kept")
        self.assertIn("https://example.com/x.png", result)
        self.assertIn("images/none.png", result)

    def test_zip_with_nested_folder(self):
        archive = Path(tempfile.mkdtemp()) / "r.zip"
        archive.write_bytes(archive_bytes(folder="abc-uuid/"))
        text, embedded, missing = markdown_from_zip(archive)
        self.assertEqual((embedded, missing), (1, 0))
        self.assertTrue(text.startswith("# 原始正文"))
        self.assertIn("data:image/png;base64,", text)
        archive.write_bytes(b"not a zip")
        with self.assertRaises(ServiceError):
            markdown_from_zip(archive)

    def test_names_follow_zotero_rules(self):
        self.assertEqual(markdown_filename("Liu 等_Phase field: a/b test"), "MD_Liu 等_Phase field ab test.md")
        self.assertEqual(valid_filename('a<b>c?"d'), "abcd")
        self.assertEqual(fallback_base("", "", "原文.pdf"), "原文")
        self.assertEqual(fallback_base("Wu和Cervera", "T" * 150), "Wu和Cervera_" + "T" * 100)
        self.assertTrue(len(markdown_filename("x" * 400)) <= 183)

    def test_page_ranges(self):
        self.assertEqual(page_ranges(None), [""])
        self.assertEqual(page_ranges(200), [""])
        self.assertEqual(page_ranges(450), ["1-200", "201-400", "401-450"])

    def test_choose_original_over_translation(self):
        paper = {"url": "", "pdfs": [
            {"key": "T", "filename": "保留排版双语PDF(译左原右)_zh_x.pdf", "title": "", "url": "", "date_added": "2020"},
            {"key": "O", "filename": "Liu 等 - 2022 - Title.pdf", "title": "Full Text PDF", "url": "", "date_added": "2024"}]}
        self.assertEqual(choose_pdf(paper)["key"], "O")
        paper["pdfs"][0]["filename"] = "Paper-dual.pdf"
        paper["pdfs"][1]["filename"] = "Translation studies.pdf"
        self.assertEqual(choose_pdf(paper)["key"], "O")
        self.assertIsNone(choose_pdf({"url": "", "pdfs": []}))

    def test_safe_message_hides_secrets(self):
        self.assertNotIn("abc123", safe_message("Bearer abc123 https://x/y?sig=1", "abc123"))


class StoreAndConfigTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)

    def tearDown(self):
        self.temp.cleanup()

    def test_dpapi_and_pairing(self):
        config = Config(self.root)
        config.token = "private-token-for-unit-test"
        config.data["output_root"] = str(self.root / "output")
        config.save()
        self.assertNotIn(config.token, config.path.read_text(encoding="utf-8"))
        second = Config(self.root)
        self.assertEqual(second.token, config.token)
        pairing = json.loads((self.root / "bridge.json").read_text(encoding="utf-8"))
        self.assertEqual(pairing["protocol"], 5)
        self.assertIn(str((self.root / "work").resolve()), pairing["allowed_roots"])
        self.assertIn(str((self.root / "output").resolve()), pairing["allowed_roots"])

    def test_corrupt_settings_fall_back(self):
        (self.root / "settings.json").write_text("{broken", encoding="utf-8")
        config = Config(self.root)
        self.assertTrue(config.load_error)
        self.assertEqual(config.data["storage_mode"], "import")

    def test_store_jobs_and_selection(self):
        store = Store(self.root / "jobs.db")
        paper = {"key": "PARENT01", "title": "题名", "creator": "Liu", "pdf_key": "PDF00001", "pdf_name": "a.pdf", "md_name": "MD_a.md"}
        self.assertTrue(store.enqueue(paper, {"model": "vlm"}))
        self.assertFalse(store.enqueue(paper, {"model": "vlm"}))
        store.update("PARENT01", stage="failed", resume_stage="attach", parts=[{"zip": "x"}])
        self.assertEqual(store.retry_failed(), 1)
        self.assertEqual(store.job("PARENT01")["stage"], "attach")
        self.assertEqual(store.job("PARENT01")["parts"], [{"zip": "x"}])
        self.assertFalse(store.selected("PARENT01"), "papers are unticked by default")
        store.set_selected("PARENT01", True)
        self.assertTrue(store.selected("PARENT01"))
        store.close()


class EngineFlowTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)

    def harness(self, **settings):
        h = Harness(self.root, **settings)
        self.addCleanup(h.close)
        return h

    def test_full_flow_import_mode(self):
        h = self.harness()
        h.paper("PARENT01", "Phase field modeling: brittle/fracture")
        h.pdf("PDFKEY01", "PARENT01", "Liu 等 - 2022 - Phase field.pdf")
        h.paper("PARENT02", "Already converted")
        h.pdf("PDFKEY02", "PARENT02", "b.pdf")
        h.markdown("MDOLD002", "PARENT02", self.root / "user.md")
        h.paper("PARENT03", "With translation", creator="Wu和Cervera")
        h.pdf("PDFTRANS", "PARENT03", "保留排版双语PDF(译左原右)_zh_x.pdf", content=PDF + b"translated", added="2026-01-01T00:00:00Z")
        original = h.pdf("PDFORIG3", "PARENT03", "Wu和Cervera - 2018 - With translation.pdf", added="2026-01-02T00:00:00Z")
        h.paper("PARENT04", "No PDF yet")
        h.paper("PARENT05", "Not ticked")
        h.pdf("PDFKEY05", "PARENT05", "e.pdf")
        h.engine.handle("scan", None)
        self.assertEqual(h.state["batches"], {}, "showing a collection never converts anything")
        h.select("PARENT01", "PARENT02", "PARENT03", "PARENT04")
        h.engine.handle("start", None)
        h.drain()
        rows = h.rows()
        self.assertEqual(rows["PARENT05"]["status"], "未勾选")
        self.assertFalse(h.store.selected("PARENT01"), "finished papers are unticked")
        self.assertEqual(rows["PARENT02"]["status"], "已有 Markdown")
        self.assertEqual(rows["PARENT04"]["status"], "暂无 PDF")
        self.assertEqual(rows["PARENT01"]["status"], "已完成")
        self.assertEqual(len(h.state["batches"]), 2)
        self.assertIn(original.read_bytes(), h.state["uploads"])
        self.assertNotIn(PDF + b"translated", h.state["uploads"])
        self.assertEqual(set(h.state["upload_content_types"]), {None})
        payload = h.state["batches"]["1"]["payload"]
        self.assertEqual((payload["model_version"], payload["language"]), ("vlm", "ch"))
        attached = [r for r in h.state["attach_requests"]]
        self.assertEqual({a["mode"] for a in attached}, {"import"})
        names = sorted(Path(a["file"]).name for a in attached)
        self.assertEqual(names, ["MD_Liu 等_Phase field modeling brittlefracture.md", "MD_Wu和Cervera_With translation.md"])
        stored = list((self.root / "storage").glob("MDRES*/*.md"))
        self.assertEqual(len(stored), 2)
        for path in stored:
            text = path.read_text(encoding="utf-8")
            self.assertIn("data:image/png;base64,", text)
            self.assertNotIn("](images/", text)
        self.assertFalse(any((self.root / "work").glob("*/*")), "work files are cleaned up")
        # A later scan sees the Markdown attachments and never submits again.
        h.engine.scan(enqueue=True)
        h.drain()
        self.assertEqual(len(h.state["batches"]), 2)
        self.assertEqual(h.rows()["PARENT01"]["status"], "已完成")

    def test_switch_turns_off_after_batch_and_new_papers_wait(self):
        h = self.harness()
        h.paper("PARENT01", "Ticked")
        h.pdf("PDFKEY01", "PARENT01", "a.pdf")
        h.paper("PARENT02", "Ticked but fails")
        h.pdf("PDFKEY02", "PARENT02", "b.pdf")
        h.select("PARENT01", "PARENT02")
        h.engine.handle("start", None)
        self.assertTrue(h.engine.watching)
        h.state["fail_attach"] = 0
        original = h.engine.mineru.submit

        def submit(parts, options):
            if parts[0]["name"].startswith("PARENT02"):
                raise ServiceError("MinerU（-60005）：文件超过 200 MB")
            return original(parts, options)
        h.engine.mineru.submit = submit
        h.drain()
        self.assertFalse(h.engine.watching, "switch turns itself off once the batch has finished")
        done = [d for kind, d in h.events if kind == "batch_done"]
        self.assertEqual((done[-1]["done"], done[-1]["failed"]), (1, 1))
        self.assertIn("自动关闭", done[-1]["message"])
        # A paper added afterwards is listed but never converted on its own.
        h.paper("NEWPAPER", "Brand new")
        h.pdf("NEWPDF01", "NEWPAPER", "new.pdf")
        h.engine.handle("scan", None)
        h.drain()
        self.assertEqual(h.rows()["NEWPAPER"]["status"], "未勾选")
        self.assertEqual(len(h.state["batches"]), 1)
        rows_events = [d for kind, d in h.events if kind == "rows"]
        self.assertTrue(rows_events[-1]["scanned"], "scan results are marked so the window can offer conversion")

    def test_new_paper_while_switch_on_is_not_auto_converted(self):
        h = self.harness()
        h.paper("PARENT01", "Ticked")
        h.pdf("PDFKEY01", "PARENT01", "a.pdf")
        h.select("PARENT01")
        h.engine.handle("start", None)
        h.paper("NEWPAPER", "Arrives during the batch")
        h.pdf("NEWPDF01", "NEWPAPER", "n.pdf")
        h.engine.scan(enqueue=True)
        h.drain()
        self.assertEqual(h.rows()["NEWPAPER"]["status"], "未勾选")
        self.assertEqual(len(h.state["batches"]), 1)
        self.assertFalse(h.engine.watching)

    def test_switch_waits_for_a_ticked_pdf_still_being_written(self):
        h = self.harness()
        h.engine.stable_seconds = 3600
        h.paper("PARENT01", "Still downloading")
        h.pdf("PDFKEY01", "PARENT01", "a.pdf")
        h.select("PARENT01")
        h.engine.handle("start", None)
        h.engine._maybe_finish()
        self.assertTrue(h.engine.watching)
        self.assertEqual(h.rows()["PARENT01"]["status"], "等待 PDF")

    def test_nothing_ticked_turns_switch_off(self):
        h = self.harness()
        h.paper("PARENT01", "Not ticked")
        h.pdf("PDFKEY01", "PARENT01", "a.pdf")
        h.engine.handle("start", None)
        h.engine._maybe_finish()
        self.assertFalse(h.engine.watching)
        self.assertIn("没有需要转换的勾选论文", [d for k, d in h.events if k == "batch_done"][-1]["message"])

    def test_switch_off_converts_nothing(self):
        h = self.harness()
        h.paper("PARENT01", "Ticked but switch off")
        h.pdf("PDFKEY01", "PARENT01", "a.pdf")
        h.engine.handle("choice", (["PARENT01"], True))
        h.drain()
        self.assertEqual(h.rows()["PARENT01"]["status"], "已勾选")
        self.assertEqual(h.state["batches"], {})

    def test_reticking_after_markdown_removed_converts_again(self):
        h = self.harness()
        h.paper("PARENT01", "Again")
        h.pdf("PDFKEY01", "PARENT01", "a.pdf")
        h.select("PARENT01")
        h.engine.handle("start", None)
        h.drain()
        result = h.store.job("PARENT01")["result_key"]
        h.state["items"][result]["data"]["deleted"] = 1
        h.engine.scan(enqueue=True)
        h.drain()
        self.assertEqual(h.rows()["PARENT01"]["status"], "已完成（MD 附件已移除）")
        self.assertEqual(len(h.state["batches"]), 1, "no silent re-conversion")
        h.engine.handle("choice", (["PARENT01"], True))
        h.engine.handle("start", None)
        h.drain()
        self.assertEqual(len(h.state["batches"]), 2)
        self.assertEqual(h.rows()["PARENT01"]["status"], "已完成")

    def test_long_pdf_split_and_merged(self):
        h = self.harness()
        h.paper("PARENT01", "Thesis")
        h.pdf("PDFKEY01", "PARENT01", "thesis.pdf")
        h.select("PARENT01")
        with mock.patch.object(engine_module, "page_count", return_value=450):
            h.engine.handle("start", None)
            h.drain()
        files = h.state["batches"]["1"]["files"]
        self.assertEqual([f["page_ranges"] for f in files], ["1-200", "201-400", "401-450"])
        self.assertEqual(len(h.state["uploads"]), 3)
        text = next((self.root / "storage").glob("MDRES*/*.md")).read_text(encoding="utf-8")
        self.assertLess(text.index("第 1-200 页"), text.index("第 201-400 页"))
        self.assertLess(text.index("第 201-400 页"), text.index("第 401-450 页"))

    def test_link_mode_and_existing_markdown_race(self):
        h = self.harness(storage_mode="link")
        h.paper("PARENT01", "Linked")
        h.pdf("PDFKEY01", "PARENT01", "a.pdf")
        h.paper("PARENT02", "User adds md before upload")
        h.pdf("PDFKEY02", "PARENT02", "b.pdf")
        h.select("PARENT01", "PARENT02")
        h.engine.handle("start", None)
        h.markdown("USERMD02", "PARENT02", self.root / "manual.md")
        h.drain()
        output = self.root / "output" / "MD_Liu 等_Linked.md"
        self.assertTrue(output.is_file())
        self.assertEqual(h.state["attach_requests"][0]["mode"], "link")
        self.assertEqual(len(h.state["batches"]), 1, "paper that gained a Markdown is not submitted")
        self.assertIsNone(h.store.job("PARENT02"))

    def _paper_with_everything(self, h):
        h.paper("PARENT01", "Paper with files")
        h.pdf("PDFKEY01", "PARENT01", "a.pdf")
        h.markdown("MDKEY001", "PARENT01", self.root / "MD_Liu 等_Paper.md")
        h.markdown("SUMKEY01", "PARENT01", self.root / "SUM_Liu 等_Paper.md")
        h.state["items"]["NOTEKEY1"] = item("NOTEKEY1", "note", parentItem="PARENT01",
                                            note="<div><h1>AI 论文总结：Paper</h1><p>正文</p></div>")
        h.state["items"]["HTMLKEY1"] = item("HTMLKEY1", "attachment", parentItem="PARENT01", contentType="text/html",
                                            linkMode="imported_url", filename="snapshot.html", title="Snapshot")

    def test_children_listed_like_zotero(self):
        h = self.harness()
        self._paper_with_everything(h)
        h.engine.handle("scan", None)
        children = h.rows()["PARENT01"]["children"]
        self.assertEqual([(c["kind"], c["key"]) for c in children],
                         [("pdf", "PDFKEY01"), ("markdown", "MDKEY001"), ("summary", "SUMKEY01"),
                          ("note", "NOTEKEY1"), ("snapshot", "HTMLKEY1")])
        self.assertEqual(children[3]["title"], "AI 论文总结：Paper")

    def test_trash_markdown_then_convert_again(self):
        h = self.harness()
        h.paper("PARENT01", "Redo me")
        h.pdf("PDFKEY01", "PARENT01", "a.pdf")
        h.select("PARENT01")
        h.engine.handle("start", None)
        h.drain()
        md_key = h.store.job("PARENT01")["result_key"]
        h.engine.handle("trash", ("PARENT01", [{"key": md_key, "kind": "markdown", "name": "MD"}]))
        self.assertTrue(h.state["items"][md_key]["data"]["deleted"], "moved to the Zotero trash")
        self.assertIsNone(h.store.job("PARENT01"), "the old conversion record is cleared")
        self.assertIn({"paper": "PARENT01", "kinds": ["markdown"]}, [d for k, d in h.events if k == "trash_done"])
        self.assertEqual(h.rows()["PARENT01"]["status"], "未勾选")
        h.engine.handle("choice", (["PARENT01"], True))
        h.engine.handle("start", None)
        h.drain()
        self.assertEqual(len(h.state["batches"]), 2, "converted again")
        self.assertEqual(h.rows()["PARENT01"]["status"], "已完成")

    def test_trash_summary_allows_new_summary_and_rejects_foreign_items(self):
        h = self.harness()
        self._paper_with_everything(h)
        h.store.enqueue_summary("PARENT01", "Paper with files")
        h.store.update_summary("PARENT01", stage="done", card={"one_line": "x", "keywords": [], "sections": {}})
        self.assertFalse(h.store.enqueue_summary("PARENT01", "Paper with files"))
        h.engine.handle("trash", ("PARENT01", [{"key": "SUMKEY01", "kind": "summary", "name": "SUM"},
                                               {"key": "NOTEKEY1", "kind": "note", "name": "AI 笔记"}]))
        self.assertTrue(h.state["items"]["SUMKEY01"]["data"]["deleted"])
        self.assertTrue(h.state["items"]["NOTEKEY1"]["data"]["deleted"])
        self.assertNotIn("PARENT01", h.store.summaries())
        self.assertTrue(h.store.enqueue_summary("PARENT01", "Paper with files"), "a new summary can be queued")
        self.assertNotIn("SUMKEY01", [c["key"] for c in h.rows()["PARENT01"]["children"]])
        h.paper("PARENT02", "Someone else")
        with self.assertRaises(ServiceError):
            h.engine.handle("trash", ("PARENT02", [{"key": "PDFKEY01", "kind": "pdf", "name": "a.pdf"}]))
        self.assertFalse(h.state["items"]["PDFKEY01"]["data"].get("deleted"))

    def test_trash_needs_the_new_plugin(self):
        h = self.harness()
        self._paper_with_everything(h)
        h.state["protocol"] = 3
        h.engine.handle("trash", ("PARENT01", [{"key": "MDKEY001", "kind": "markdown", "name": "MD"}]))
        self.assertFalse(h.state["items"]["MDKEY001"]["data"].get("deleted"))
        self.assertEqual([d for k, d in h.events if k == "trash_done"][-1], {"paper": "PARENT01", "outdated": "旧"})
        h.engine.handle("scan", None)
        self.assertNotIn("bridge_outdated", [k for k, d in h.events], "plugin 2.1 still converts and summarises")

    def test_export_copy(self):
        h = self.harness(export_copy=True)
        h.paper("PARENT01", "Copy me")
        h.pdf("PDFKEY01", "PARENT01", "a.pdf")
        h.select("PARENT01")
        h.engine.handle("start", None)
        h.drain()
        self.assertTrue((self.root / "output" / "MD_Liu 等_Copy me.md").is_file())

    def test_attach_failure_resumes_without_new_parse(self):
        h = self.harness()
        h.state["fail_attach"] = 10
        h.paper("PARENT01", "Attach later")
        h.pdf("PDFKEY01", "PARENT01", "a.pdf")
        h.select("PARENT01")
        h.engine.handle("start", None)
        h.drain()
        job = h.store.job("PARENT01")
        self.assertEqual((job["stage"], job["resume_stage"]), ("failed", "attach"))
        h.state["fail_attach"] = 0
        h.engine.handle("retry", None)
        h.drain()
        self.assertEqual(h.store.job("PARENT01")["stage"], "done")
        self.assertEqual(len(h.state["batches"]), 1)

    def test_cloud_failure_and_token_rejection(self):
        h = self.harness()
        h.state["cloud_failure"] = True
        h.paper("PARENT01", "Fails")
        h.pdf("PDFKEY01", "PARENT01", "a.pdf")
        h.select("PARENT01")
        h.engine.handle("start", None)
        h.drain()
        job = h.store.job("PARENT01")
        self.assertEqual((job["stage"], job["resume_stage"]), ("failed", "queued"))
        h.state["cloud_failure"] = False
        h.state["token"] = "other"
        h.engine.handle("retry", None)
        h.drain()
        self.assertFalse(h.engine.watching)
        self.assertIn(("watching", False), h.events)

    def test_old_bridge_blocks_start(self):
        h = self.harness()
        h.state["protocol"] = 2
        with self.assertRaises(ServiceError) as caught:
            h.engine.handle("start", None)
        self.assertIn("版本过旧", str(caught.exception))
        h.engine.handle("scan", None)
        self.assertEqual([d for kind, d in h.events if kind == "bridge_outdated"], ["旧"], "the window is told once")

    def test_old_folder_result_counts_as_existing_markdown(self):
        h = self.harness()
        h.paper("PARENT01", "Old result")
        h.pdf("PDFKEY01", "PARENT01", "a.pdf")
        folder = self.root / "old" / "e409" / "PDFKEY01" / ("f" * 64)
        (folder / "images").mkdir(parents=True)
        (folder / "images" / "a.png").write_bytes(PNG)
        (folder / "full.md").write_text("# 旧结果\n![](images/a.png)\n", encoding="utf-8")
        (folder / "conversion.json").write_text("{}", encoding="utf-8")
        h.markdown("LEGACYMD", "PARENT01", folder / "full.md", link_mode="linked_file")
        h.engine.handle("choice", (["PARENT01"], True))
        h.engine.handle("start", None)
        h.drain()
        self.assertEqual(h.rows()["PARENT01"]["status"], "已有 Markdown")
        self.assertEqual(h.state["batches"], {}, "a paper with any Markdown is never converted again")
        self.assertEqual(h.state["attach_requests"], [])
        self.assertFalse(h.state["items"]["LEGACYMD"]["data"].get("deleted"), "the 1.x result is left as it is")

    def test_unticked_paper_is_never_converted(self):
        h = self.harness()
        h.paper("PARENT01", "Skip me")
        h.pdf("PDFKEY01", "PARENT01", "a.pdf")
        h.engine.handle("choice", (["PARENT01"], True))
        h.engine.handle("choice", (["PARENT01"], False))
        h.engine.handle("start", None)
        h.drain()
        self.assertEqual(h.rows()["PARENT01"]["status"], "未勾选")
        self.assertEqual(h.state["batches"], {})


if __name__ == "__main__":
    unittest.main()
