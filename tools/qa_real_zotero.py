"""Exercise the real XPI in a disposable Zotero profile; MinerU is simulated, the user's library is untouched."""
from __future__ import annotations

import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import threading
import time
import zipfile
from http.server import ThreadingHTTPServer

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tests"))
sys.path.insert(0, str(ROOT / "tools"))

from mock_llm import Handler as MockLLM
from test_core import PNG, FakeService
from test_summary import PAPER, ScriptedClient
from zotero_mineru.clients import MinerUClient, ServiceError, ZoteroClient
from zotero_mineru.config import Config
from zotero_mineru.engine import Engine, choose_pdf
from zotero_mineru.store import Store
from zotero_mineru.summarizer import INDEX_MD, Summarizer
from zotero_mineru.translation import Translator, engine_command

PORT = 23129
FIXTURE_TITLE = "Isolated acceptance PDF: phase-field modeling of brittle fracture in thin shells"
FIXTURE_TEXT = [
    "This document is generated for an isolated acceptance test. No real document is uploaded. The prediction of "
    "fracture in thin-walled structures is decisive for a wide range of applications, and modeling methods such as the "
    "phase-field method usually consider cracks to be constant over the thickness of the shell.",
    "In this contribution, fracture phenomena along the thickness direction of structural elements are addressed with "
    "a phase-field modeling approach. Shell elements describe the displacement field on the midsurface, while solid "
    "elements describe the crack phase-field in the three-dimensional space of the structure.",
    "The governing equations of the model are derived in a consistent manner from a total energy functional and are "
    "compared to existing standard models. The resulting model reduces the computational effort while still being able "
    "to represent arbitrary through-thickness crack evolutions as well as partly broken shells.",
]
HARNESS = r'''
var Harness;
function install() {}
function uninstall() {}
function startup() {
    createFixture().catch(error => Zotero.logError(error));
}
async function paper(title, collection, creators) {
    const item = new Zotero.Item("journalArticle");
    item.setField("title", title);
    item.setField("date", "2024");
    item.setCreators(creators);
    item.setCollections([collection.id]);
    await item.saveTx();
    return item;
}
async function createFixture() {
    await Zotero.initializationPromise;
    await Zotero.uiReadyPromise;
    await Zotero.Libraries.get(Zotero.Libraries.userLibraryID).waitForDataLoad("item");
    const home = Services.env.get("MINERU_ZOTERO_HOME");
    const root = PathUtils.parent(home);
    const collection = new Zotero.Collection();
    collection.name = "MinerU 独立验收分类";
    await collection.saveTx();
    const authors = [{ firstName: "Wei", lastName: "Liu", creatorType: "author" },
                     { firstName: "San", lastName: "Zhang", creatorType: "author" },
                     { firstName: "Si", lastName: "Li", creatorType: "author" }];
    const first = await paper("Phase field modeling: brittle/fracture of glass", collection, authors);
    const pdf = await Zotero.Attachments.importFromFile({ file: PathUtils.join(root, "fixture.pdf"),
        parentItemID: first.id, contentType: "application/pdf" });
    const second = await paper("Paper that already has Markdown", collection, [authors[1]]);
    await Zotero.Attachments.importFromFile({ file: PathUtils.join(root, "fixture.pdf"), parentItemID: second.id, contentType: "application/pdf" });
    await Zotero.Attachments.importFromFile({ file: PathUtils.join(root, "existing.md"), parentItemID: second.id, contentType: "text/markdown" });
    const third = await paper("Legacy folder result", collection, [authors[2]]);
    await Zotero.Attachments.importFromFile({ file: PathUtils.join(root, "fixture.pdf"), parentItemID: third.id, contentType: "application/pdf" });
    const legacy = await Zotero.Attachments.linkFromFile({ file: PathUtils.join(root, "legacy", "e409", "PDFKEY01", "f".repeat(64), "full.md"),
        parentItemID: third.id, title: "旧版（MinerU Markdown）", contentType: "text/markdown" });
    Zotero.Server.Endpoints["/mineru-qa/add-paper"] = class {
        supportedMethods = ["POST"];
        supportedDataTypes = ["application/json"];
        async init(request) {
            const pairing = await IOUtils.readJSON(PathUtils.join(home, "bridge.json"));
            const token = typeof request.headers.get === "function" ? request.headers.get("X-MinerU-Bridge-Token") : request.headers["x-mineru-bridge-token"];
            if (token !== pairing.token) return [403, "text/plain", "denied"];
            // Simulates the connector: the parent appears first, the PDF a moment later.
            const item = await paper("A newly added paper", collection, [{ firstName: "Ann", lastName: "Wu", creatorType: "author" }]);
            setTimeout(() => Zotero.Attachments.importFromFile({ file: PathUtils.join(root, "fixture.pdf"),
                parentItemID: item.id, contentType: "application/pdf" }).catch(e => Zotero.logError(e)), 1500);
            return [200, "application/json", JSON.stringify({ ok: true, key: item.key })];
        }
    };
    Zotero.Server.Endpoints["/mineru-qa/diagnostic"] = class {
        supportedMethods = ["POST"];
        supportedDataTypes = ["application/json"];
        async init(request) {
            return [200, "application/json", JSON.stringify({ errors: Services.console.getMessageArray().slice(-30).map(m => m.message) })];
        }
    };
    await IOUtils.writeJSON(PathUtils.join(root, "fixture-ready.json"),
        { collection: collection.key, first: first.key, pdf: pdf.key, second: second.key, third: third.key, legacy: legacy.key });
}
function shutdown() { delete Zotero.Server.Endpoints["/mineru-qa/add-paper"]; }
'''


def wait_until(predicate, seconds=45):
    end = time.monotonic() + seconds
    while time.monotonic() < end:
        value = predicate()
        if value:
            return value
        time.sleep(0.5)
    raise TimeoutError("Acceptance test timed out")


def markdown_children(client, key):
    return [c for c in client.children(key) if c["data"].get("contentType") == "text/markdown" and not c["data"].get("deleted")]


def main():
    qa = ROOT / "qa" / "real-zotero"
    qa.mkdir(parents=True, exist_ok=True)
    for name in ("profile", "data", "app-state", "output", "legacy", "fixture-ready.json", "acceptance.json", "diagnostic.json"):
        target = (qa / name).resolve()
        assert target.is_relative_to(qa.resolve())
        if target.is_dir():
            shutil.rmtree(target)
        else:
            target.unlink(missing_ok=True)
    profile, data, home = qa / "profile", qa / "data", qa / "app-state"
    for path in (profile, data, home, profile / "extensions"):
        path.mkdir(parents=True, exist_ok=True)
    config = Config(home)
    config.token = "isolated-test-token"
    config.data.update({"output_root": str(qa / "output"), "zotero_port": PORT, "interval": 15, "storage_mode": "import"})
    config.save()
    os.environ["QT_QPA_PLATFORM"] = "offscreen"
    from PySide6.QtCore import QRect, Qt
    from PySide6.QtGui import QFont, QFontDatabase, QGuiApplication, QPainter, QPdfWriter
    app = QGuiApplication([])
    # The offscreen platform has no font database: without a loaded font the PDF would hold no text at all.
    fonts = Path(os.environ.get("WINDIR", "C:/Windows")) / "Fonts"
    family = QFontDatabase.applicationFontFamilies(QFontDatabase.addApplicationFont(str(fonts / "times.ttf")))
    QFontDatabase.addApplicationFont(str(fonts / "timesbd.ttf"))  # a real bold face, not Qt's drawn-twice fake bold
    pdf = QPdfWriter(str(qa / "fixture.pdf"))
    pdf.setResolution(300)
    painter = QPainter(pdf)
    painter.setFont(QFont(family[0] if family else "Times New Roman", 16, QFont.Weight.Bold))
    painter.drawText(QRect(150, 150, 2180, 200), Qt.TextFlag.TextWordWrap, FIXTURE_TITLE)
    painter.setFont(QFont(family[0] if family else "Times New Roman", 10))
    top = 420
    for paragraph in FIXTURE_TEXT:
        box = painter.boundingRect(QRect(150, top, 2180, 2000), Qt.TextFlag.TextWordWrap, paragraph)
        painter.drawText(box, Qt.TextFlag.TextWordWrap, paragraph)
        top = box.bottom() + 60
    painter.end()
    del pdf, app
    (qa / "existing.md").write_text("# 用户自己的笔记\n", encoding="utf-8")
    legacy = qa / "legacy" / "e409" / "PDFKEY01" / ("f" * 64)
    (legacy / "images").mkdir(parents=True)
    (legacy / "images" / "a.png").write_bytes(PNG)
    (legacy / "full.md").write_text("# 旧版结果\n\n![](images/a.png)\n", encoding="utf-8")
    (legacy / "conversion.json").write_text("{}", encoding="utf-8")
    preferences = {
        "extensions.zotero.dataDir": str(data), "extensions.zotero.useDataDir": True,
        "extensions.zotero.httpServer.enabled": True, "extensions.zotero.httpServer.port": PORT,
        "extensions.zotero.httpServer.localAPI.enabled": True, "extensions.zotero.firstRun2": False,
        "extensions.zotero.sync.autoSync": False, "extensions.zotero.automaticScraperUpdates": False,
        "extensions.zotero.autoRecognizeFiles": False, "extensions.autoDisableScopes": 0,
        "extensions.startupScanScopes": 15, "xpinstall.signatures.required": False,
        "browser.shell.checkDefaultBrowser": False, "intl.locale.requested": "zh-CN",
        "extensions.zoteroWinWordIntegration.skipInstallation": True,
        "extensions.zoteroOpenOfficeIntegration.skipInstallation": True,
        "extensions.update.enabled": False,
    }
    (profile / "user.js").write_text("\n".join(f"user_pref({json.dumps(k)}, {json.dumps(v)});" for k, v in preferences.items()), encoding="utf-8")
    shutil.copy2(ROOT / "resources/mineru-zotero-bridge.xpi", profile / "extensions/mineru-bridge@local.zotero.xpi")
    with zipfile.ZipFile(profile / "extensions/mineru-qa@local.zotero.xpi", "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("manifest.json", json.dumps({"manifest_version": 2, "name": "MinerU Isolated QA", "version": "2.0",
            "applications": {"zotero": {"id": "mineru-qa@local.zotero", "update_url": "https://updates.invalid/mineru-qa.json", "strict_min_version": "9.0", "strict_max_version": "9.*"}}}))
        zf.writestr("bootstrap.js", HARNESS)
    executable = next((p for p in [Path("C:/Program Files/Zotero/zotero.exe"), Path("D:/SoftCenters/Zotero/zotero.exe"),
                                   Path(os.environ.get("LOCALAPPDATA", "")) / "Zotero/zotero.exe"] if p.is_file()), None)
    if executable is None:
        raise RuntimeError("Zotero executable not found")
    env = dict(os.environ, MINERU_ZOTERO_HOME=str(home))
    startup = subprocess.STARTUPINFO()
    startup.dwFlags |= subprocess.STARTF_USESHOWWINDOW
    startup.wShowWindow = 0
    debug = (qa / "zotero-debug.log").open("w", encoding="utf-8")
    process = subprocess.Popen([str(executable), "-no-remote", "-profile", str(profile), "-ZoteroDebugText"],
        env=env, startupinfo=startup, stdout=debug, stderr=subprocess.STDOUT)
    engine = server = store = None
    client = ZoteroClient(PORT, config.pairing["token"])
    try:
        ready_path = qa / "fixture-ready.json"
        print("Waiting for isolated Zotero profile to initialize...", flush=True)
        wait_until(ready_path.is_file, 90)
        ready = json.loads(ready_path.read_text(encoding="utf-8"))
        print("Real Zotero fixture ready:", ready, flush=True)
        bridge = client.bridge("status")
        assert bridge["protocol"] == 5, bridge
        names = client.bridge("names", keys=[ready["first"]])["names"]
        print("Zotero names:", names, flush=True)
        config.data["collections"] = [ready["collection"]]
        config.save()
        state = {"token": config.token, "batches": {}, "uploads": [], "upload_content_types": []}
        server = ThreadingHTTPServer(("127.0.0.1", 0), FakeService)
        server.state = state
        threading.Thread(target=server.serve_forever, daemon=True).start()
        store = Store(home / "jobs-v2.sqlite3")
        logs, rows = [], {}

        def event(kind, value):
            if kind in ("log", "error"):
                logs.append({"kind": kind, "message": value})
                print(kind, value, flush=True)
            elif kind == "rows":
                rows.update({r["key"]: r for r in value["rows"]})
        engine = Engine(store, config.data, config.token, config.pairing["token"], config.work_dir, event,
            mineru_factory=lambda token: MinerUClient(token, f"http://127.0.0.1:{server.server_port}", allow_local=True))
        engine.start()
        # Nothing converts until papers are ticked and the switch is turned on.
        time.sleep(2)
        assert state["batches"] == {}, "listing a collection must not convert anything"
        store.set_selected(ready["first"], True)
        engine.command("start")
        wait_until(lambda: (store.job(ready["first"]) or {}).get("stage") == "done", 60)
        first_md = markdown_children(client, ready["first"])
        assert len(first_md) == 1, first_md
        md_data = first_md[0]["data"]
        expected_name = "MD_" + names[ready["first"]] + ".md"
        assert md_data["filename"] == expected_name, (md_data["filename"], expected_name)
        assert md_data["linkMode"] == "imported_file"
        stored = client.file_path(first_md[0]["key"], first_md[0]["links"]["enclosure"]["href"])
        text = stored.read_text(encoding="utf-8")
        assert "data:image/png;base64," in text and "](images/" not in text
        assert not any(p.is_dir() for p in stored.parent.iterdir()), "no image folder beside the Markdown"
        assert store.job(ready["second"]) is None and len(markdown_children(client, ready["second"])) == 1
        assert rows[ready["second"]]["status"] == "已有 Markdown"
        assert rows[ready["third"]]["status"] == "已有 Markdown", "a folder-based 1.x result is left as it is"

        # AI summary: a SUM_ card and a child note are added next to the Markdown in real Zotero.
        stored.write_text(PAPER, encoding="utf-8")
        summarizer = Summarizer(store, {**config.data, "summary_part_tokens": 4000}, {"deepseek": "sk-isolated-test"},
                                config.pairing["token"], config.work_dir, event, llm_factory=lambda *args: ScriptedClient())
        summarizer.start()
        summarizer.command("enqueue", [(ready["first"], "first")])
        try:
            wait_until(lambda: store.summaries().get(ready["first"], {}).get("stage") in ("done", "failed"), 60)
        finally:
            summarizer.close()
            summarizer.join(timeout=10)
        summary_job = store.summaries()[ready["first"]]
        assert summary_job["stage"] == "done", summary_job["error"]
        children = client.children(ready["first"])
        sum_children = [c for c in children if c["data"].get("filename", "").startswith("SUM_")]
        note_children = [c for c in children if c["data"].get("itemType") == "note"]
        assert len(sum_children) == 1 and len(note_children) == 1, children
        assert len(markdown_children(client, ready["first"])) == 2  # full text + card, both text/markdown
        assert "研究背景" in note_children[0]["data"]["note"]
        assert "zotero://open-pdf/library/items/" + ready["pdf"] in note_children[0]["data"]["note"], "PDF link kept in the note"
        card_file = client.file_path(sum_children[0]["key"], sum_children[0]["links"]["enclosure"]["href"])
        card_text = card_file.read_text(encoding="utf-8")
        assert f'pdf_zotero_key: "{ready["pdf"]}"' in card_text and "[全文 Markdown](<../" in card_text, card_text[:800]
        assert (card_file.parent.parent / Path(card_text.split("[全文 Markdown](<../", 1)[1].split(">)", 1)[0])).is_file(),             "relative link from the card reaches the full-text Markdown"
        assert (Path(config.data["output_root"]) / INDEX_MD).is_file()

        # Deleting the card and its note from the app uses Zotero's trash; the summary can then be made again.
        sum_key, note_key = sum_children[0]["key"], note_children[0]["key"]
        engine.command("trash", (ready["first"], [{"key": sum_key, "kind": "summary", "name": "SUM"},
                                                  {"key": note_key, "kind": "note", "name": "AI 笔记"}]))
        wait_until(lambda: client.item(sum_key)["data"].get("deleted"), 20)
        assert client.item(note_key)["data"].get("deleted") and ready["first"] not in store.summaries()
        assert len(markdown_children(client, ready["first"])) == 1, "only the full-text Markdown is left"
        summarizer = Summarizer(store, {**config.data, "summary_part_tokens": 4000}, {"deepseek": "sk-isolated-test"},
                                config.pairing["token"], config.work_dir, event, llm_factory=lambda *args: ScriptedClient())
        summarizer.start()
        summarizer.command("enqueue", [(ready["first"], "first")])
        try:
            wait_until(lambda: store.summaries().get(ready["first"], {}).get("stage") in ("done", "failed"), 60)
        finally:
            summarizer.close()
            summarizer.join(timeout=10)
        assert store.summaries()[ready["first"]]["stage"] == "done", store.summaries()[ready["first"]]["error"]
        redone = [c for c in client.children(ready["first"]) if c["data"].get("filename", "").startswith("SUM_")
                  and not c["data"].get("deleted")]
        assert len(redone) == 1 and redone[0]["key"] != sum_key, "a new card replaced the deleted one"

        # Bilingual PDF: the real translation engine (BabelDocWorker) with a local stand-in model, attached by the
        # real plugin as a PDF; the original stays the PDF that conversion and translation use.
        llm = ThreadingHTTPServer(("127.0.0.1", 0), MockLLM)
        threading.Thread(target=llm.serve_forever, daemon=True).start()
        translate_settings = {**config.data, "llm_provider": "custom", "llm_models": {"custom": "mock"},
                              "llm_bases": {"custom": f"http://127.0.0.1:{llm.server_port}/v1"}}
        translator = Translator(store, translate_settings, {"custom": "sk-isolated-test"}, config.pairing["token"],
                                config.work_dir, home, event)
        print("Translation engine:", engine_command(), flush=True)
        translator.start()
        translator.command("enqueue", [(ready["first"], "first")])
        try:
            wait_until(lambda: store.translations().get(ready["first"], {}).get("stage") in ("done", "failed"), 300)
        finally:
            translator.close()
            translator.join(timeout=10)
            llm.shutdown()
            llm.server_close()
        translation_job = store.translations()[ready["first"]]
        assert translation_job["stage"] == "done", translation_job["error"]
        bilingual = [c for c in client.children(ready["first"]) if c["data"].get("filename", "").startswith("双语_")]
        assert len(bilingual) == 1, client.children(ready["first"])
        assert bilingual[0]["data"]["contentType"] == "application/pdf" and bilingual[0]["data"]["linkMode"] == "imported_file"
        assert bilingual[0]["data"]["filename"] == "双语_" + names[ready["first"]] + ".pdf", bilingual[0]["data"]["filename"]
        bilingual_file = client.file_path(bilingual[0]["key"], bilingual[0]["links"]["enclosure"]["href"])
        assert bilingual_file.read_bytes().startswith(b"%PDF")
        assert not translation_job["meta"]["warnings"], translation_job["meta"]["warnings"]
        engine.command("scan")
        wait_until(lambda: rows.get(ready["first"], {}).get("translation_status") == "已有双语", 20)
        assert choose_pdf(rows[ready["first"]])["key"] == ready["pdf"], "the original PDF stays the one converted"

        # A second attach request for a paper with Markdown never adds a duplicate.
        duplicate = config.work_dir / "dup" / "MD_dup.md"
        duplicate.parent.mkdir(parents=True, exist_ok=True)
        duplicate.write_text("# dup", encoding="utf-8")
        result = client.bridge("attach", parent_key=ready["first"], file=str(duplicate), mode="import", title="dup", replace_key="")
        assert result["skipped"] and len(markdown_children(client, ready["first"])) == 2
        try:
            client.bridge("eval", code="throw 1")
        except ServiceError:
            pass
        else:
            raise AssertionError("Unexpected arbitrary operation accepted")

        # The switch closed itself after the first batch; a paper added later is listed but not converted.
        assert not engine.watching, "switch closes itself once the ticked papers are done"
        added = client.request("/mineru-qa/add-paper", payload={}, headers={"X-MinerU-Bridge-Token": config.pairing["token"]})

        def listed_with_pdf():
            engine.command("scan")
            time.sleep(1)
            return rows.get(added["key"], {}).get("pdf_key")
        wait_until(listed_with_pdf, 30)
        assert rows[added["key"]]["status"] == "未勾选" and store.job(added["key"]) is None
        # The user confirms the reminder: the paper is ticked and the switch turned on for one more batch.
        engine.command("choice", ([added["key"]], True))
        engine.command("start")
        wait_until(lambda: (store.job(added["key"]) or {}).get("stage") == "done", 60)
        wait_until(lambda: not engine.watching, 20)
        new_md = markdown_children(client, added["key"])
        assert len(new_md) == 1 and new_md[0]["data"]["filename"].startswith("MD_")

        third_md = markdown_children(client, ready["third"])
        assert [c["key"] for c in third_md] == [ready["legacy"]], third_md
        assert not client.item(ready["legacy"])["data"].get("deleted"), "the 1.x result is never touched"
        assert len(state["batches"]) == 2, "only the two papers without Markdown were sent to MinerU"
        evidence = {"real_zotero_version": bridge["zotero_version"], "isolated_profile": str(profile),
            "first_markdown": md_data["filename"], "first_markdown_bytes": len(text.encode()),
            "new_paper_markdown": new_md[0]["data"]["filename"], "kept_1x_markdown": third_md[0]["data"]["filename"],
            "skipped_existing": ready["second"], "mineru_batches": len(state["batches"]),
            "summary_card": sum_children[0]["data"]["filename"], "summary_note": note_children[0]["key"],
            "bilingual_pdf": bilingual[0]["data"]["filename"], "bilingual_pdf_bytes": bilingual_file.stat().st_size,
            "translation": translation_job["progress"],
            "mineru": "local simulated HTTP service; no external PDF upload", "logs": logs}
        (qa / "acceptance.json").write_text(json.dumps(evidence, ensure_ascii=False, indent=2), encoding="utf-8")
        print("ACCEPTANCE PASSED", json.dumps({k: v for k, v in evidence.items() if k != "logs"}, ensure_ascii=False), flush=True)
    except Exception:
        try:
            diagnostic = client.request("/mineru-qa/diagnostic", payload={})
            (qa / "diagnostic.json").write_text(json.dumps(diagnostic, ensure_ascii=False, indent=2), encoding="utf-8")
        except Exception as error:
            print("Diagnostic unavailable:", str(error), flush=True)
        raise
    finally:
        if engine:
            engine.close()
            engine.join(timeout=10)
        if server:
            server.shutdown()
            server.server_close()
        if store and (not engine or not engine.is_alive()):
            store.close()
        process.terminate()
        try:
            process.wait(timeout=10)
        except subprocess.TimeoutExpired:
            process.kill()
        # Zotero's Windows launcher spawns a second process; match this exact QA profile only.
        escaped_profile = str(profile.resolve()).replace("'", "''")
        escaped_exe = str(executable).replace("'", "''")
        cleanup = ("Get-CimInstance Win32_Process -Filter \"name = 'zotero.exe'\" | "
            f"Where-Object {{ $_.ExecutablePath -eq '{escaped_exe}' -and $_.CommandLine.Contains('{escaped_profile}') }} | "
            "ForEach-Object { Stop-Process -Id $_.ProcessId -Force }")
        subprocess.run(["powershell", "-NoProfile", "-Command", cleanup], check=True)
        debug.close()


if __name__ == "__main__":
    main()
