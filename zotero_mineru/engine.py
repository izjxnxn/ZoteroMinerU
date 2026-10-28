from __future__ import annotations

import copy
import queue
import re
import shutil
import threading
import time
import uuid
from pathlib import Path

from .clients import TRANSLATION, MinerUClient, ServiceError, ZoteroClient, is_markdown_attachment, safe_message
from .config import BRIDGE_MIN_PROTOCOL, BRIDGE_PROTOCOL, BRIDGE_TRASH_PROTOCOL
from .markdown import (check_pdf, fallback_base, join_parts, markdown_filename, markdown_from_zip, page_count,
                       page_ranges, unique_path, write_text)
from .store import ACTIVE, CLOUD, STATES, Store

MAX_CLOUD_JOBS = 3


def translation_label(job: dict) -> str:
    """What the 双语 PDF column says: the real state, not one label for queued, running and waiting."""
    progress = job["progress"] or ""
    if job["stage"] == "failed":
        return "失败，待重试"
    if "插件需要更新" in progress:
        return "待更新插件"
    if job["stage"] == "attach":
        return "添加到 Zotero"
    running = re.match(r"翻译中 (\d+)%", progress)
    if running:
        return f"翻译中 {running[1]}%"
    if progress.startswith(("启动翻译引擎", "测试")):
        return "翻译中"
    return "等待中" if progress.startswith("等待") or job["error"] else "排队中"


def summary_label(job: dict) -> str:
    """The AI 总结 column for an unfinished card; the window turns the paper being read into a progress bar."""
    progress = job["progress"] or ""
    if job["stage"] == "failed":
        return "失败，待重试"
    if job["stage"] == "attach":
        return "添加到 Zotero"
    if "插件需要更新" in progress:
        return "待更新插件"
    if job["error"]:
        return "等待重试"
    return "等待中" if progress.startswith("等待") else "排队中"


def choose_pdf(paper: dict) -> dict | None:
    def rank(pdf):
        translated = any(TRANSLATION.search(pdf.get(field) or "") for field in ("filename", "title"))
        return (translated, not (paper.get("url") and pdf.get("url") == paper["url"]),
                pdf.get("date_added") or "")
    return min(paper["pdfs"], key=rank) if paper["pdfs"] else None


class Engine(threading.Thread):
    def __init__(self, store: Store, settings: dict, token: str, bridge_token: str, work_dir: Path, emit,
                 *, zotero_factory=ZoteroClient, mineru_factory=MinerUClient, stable_seconds: float = 3):
        super().__init__(name="zotero-mineru-worker", daemon=True)
        self.store, self.emit = store, emit
        self.settings = copy.deepcopy(settings)
        self.token, self.bridge_token = token, bridge_token
        self.work_dir = Path(work_dir)
        self.zotero_factory, self.mineru_factory = zotero_factory, mineru_factory
        self.stable_seconds = stable_seconds
        self.commands = queue.Queue()
        self.stopping = threading.Event()
        self.watching = False
        self.bridge_info: dict = {}
        self.papers: list[dict] = []
        # When the last scan started reading Zotero: cards and PDFs attached after that are not in self.papers yet.
        self.scanned_at = 0.0
        self.collections: list[dict] = []
        self.allowed: set[str] = set()
        self.manual: set[str] = set()
        self.observed: dict[str, tuple] = {}
        # Papers queued since the switch was turned on; the switch turns itself off once they have all finished.
        self.batch: set[str] = set()
        self.next_scan = self.next_events = 0.0
        self.event_marker = None
        self.outdated_warned = False
        self._new_clients()

    def _new_clients(self):
        self.zotero = self.zotero_factory(self.settings["zotero_port"], self.bridge_token)
        self.mineru = self.mineru_factory(self.token)

    def command(self, action: str, data=None):
        self.commands.put((action, data))

    def close(self):
        self.stopping.set()
        self.commands.put(("stop", None))

    def log(self, value):
        self.emit("log", safe_message(value, self.token))

    def options(self) -> dict:
        return {key: self.settings[key] for key in ("model", "language", "ocr", "formula", "table")}

    # ------------------------------------------------------------------ main loop
    def run(self):
        self.command("scan")
        while not self.stopping.is_set():
            try:
                action, data = self.commands.get(timeout=0.5)
                if action == "stop":
                    break
                self.handle(action, data)
            except queue.Empty:
                pass
            except Exception as error:
                self.emit("error", safe_message(str(error), self.token))
                self.log(str(error))
            if self.stopping.is_set() or not (self.watching or self.manual):
                continue
            now = time.monotonic()
            if self.watching and now >= self.next_events:
                self.next_events = now + 3
                try:
                    info = self.zotero.bridge("status")
                    marker = (info.get("boot"), info.get("revision"))
                    if marker != self.event_marker:
                        # Items arrive in bursts (parent first, PDF a few seconds later); let them settle briefly.
                        self.next_scan = min(self.next_scan, now + 2)
                        self.event_marker = marker
                except ServiceError:
                    self.emit("connection", "Zotero 暂时离线，等待恢复…")
            if self.watching and now >= self.next_scan:
                try:
                    self.scan(enqueue=True)
                except Exception as error:
                    self.next_scan = time.monotonic() + 15
                    self.log("扫描暂未完成：" + str(error))
            try:
                self.process_one()
                self._maybe_finish()
            except Exception as error:
                self.log("队列处理错误：" + str(error))
        self.emit("finished", None)

    def handle(self, action: str, data):
        if action == "configure":
            self.settings, self.token = copy.deepcopy(data[0]), data[1]
            self._new_clients()
            self.next_scan = 0
        elif action == "scan":
            self.scan(enqueue=self.watching)
        elif action == "probe":
            self.emit("probe", self.mineru.probe())
        elif action == "start":
            if not self.token:
                raise ServiceError("请先在“设置”页填写并保存 MinerU API Token。")
            if not self.settings["collections"]:
                raise ServiceError("请先在左侧勾选分类，并在右侧勾选要转换的论文。")
            self._connect_bridge(required=True)
            self.watching = True
            self.batch = set()
            self.emit("watching", True)
            self.log("转换开关已打开：开始转换勾选的论文，全部完成后开关自动关闭。")
            self.scan(enqueue=True)
        elif action == "pause":
            self.watching = False
            self.manual.clear()
            self.emit("watching", False)
            self.log("转换开关已关闭。已提交到云端的任务会保留，再次打开后从原阶段继续。")
        elif action == "choice":
            keys, enabled = data
            for key in keys:
                self.store.set_selected(key, enabled)
                if not enabled:
                    self.allowed.discard(key)
                    self.manual.discard(key)
            self.scan(enqueue=self.watching)
        elif action == "retry":
            failed = [k for k, j in self.store.jobs().items() if j["stage"] == "failed"]
            count = self.store.retry_failed()
            self.manual.update(failed)
            self.log(f"已重新排队 {count} 个失败任务（从失败前的阶段继续）。")
            self.publish()
        elif action == "publish":
            self.publish()
        elif action == "trash":
            self.trash(*data)

    # ------------------------------------------------------------------ Zotero scan
    def _connect_bridge(self, *, required: bool) -> bool:
        try:
            info = self.zotero.bridge("status")
        except ServiceError as error:
            self.bridge_info = {}
            text = str(error)
            if "认证" in text or "配对" in text:
                text = "桥接插件配对失败：请在本软件点击“保存设置”，若仍失败请重启 Zotero。"
            elif "找不到" in text or "尚未安装" in text:
                text = "Zotero 已连接，但未安装 MinerU 桥接插件（设置页 → 安装/更新 Zotero 插件）。"
            self.emit("connection", text)
            if required:
                raise ServiceError(text) from None
            return False
        if info.get("protocol", 0) < BRIDGE_MIN_PROTOCOL:
            self.bridge_info = {}
            text = "Zotero 中的桥接插件版本过旧，请在设置页点击“安装/更新 Zotero 插件”重新安装。"
            self.emit("connection", text)
            if not self.outdated_warned:
                self.outdated_warned = True
                self.emit("bridge_outdated", info.get("version") or "旧")
            if required:
                raise ServiceError(text)
            return False
        self.bridge_info = info
        self.emit("connection", f"Zotero {info.get('zotero_version', '')} · 插件已连接"
                  + ("" if info.get("protocol", 0) >= BRIDGE_PROTOCOL else "（双语 PDF 需更新插件）"
                     if info.get("protocol", 0) >= BRIDGE_TRASH_PROTOCOL else "（双语 PDF、删除附件需更新插件）"))
        return True

    def _names(self, keys: list[str]) -> dict[str, str]:
        names = {}
        try:
            for start in range(0, len(keys), 300):
                names.update(self.zotero.bridge("names", keys=keys[start:start + 300]).get("names", {}))
        except ServiceError as error:
            self.log("读取 Zotero 命名规则失败，改用作者与标题生成文件名：" + str(error))
        return names

    def scan(self, *, enqueue: bool = False):
        self.emit("busy", "正在读取 Zotero 文献…")
        self.next_scan = time.monotonic() + max(15, self.settings["interval"])
        try:
            bridge = self._connect_bridge(required=enqueue)
            self.collections = self.zotero.collections()
            self.emit("collections", self.collections)
            selected, include_sub = self.settings["collections"], self.settings["include_subcollections"]
            fetched = time.time()
            papers = self.zotero.papers(selected, include_sub, self.collections)
            names = self._names([p["key"] for p in papers]) if bridge and papers else {}
            jobs = self.store.jobs()
            allowed = set()
            for paper in papers:
                if self.stopping.is_set():
                    return
                key = paper["key"]
                pdf = choose_pdf(paper)
                paper.update(pdf_key=pdf["key"] if pdf else "", pdf_name=pdf["filename"] if pdf else "",
                             pdf_href=pdf["href"] if pdf else "", selected=self.store.selected(key),
                             status="", detail="")
                base = names.get(key) or fallback_base(paper["creator"], paper["title"], paper["pdf_name"])
                paper["md_name"] = markdown_filename(base)
                job = jobs.get(key)
                if job:
                    self.store.refresh(paper)
                if paper["markdowns"]:
                    # A Markdown was attached some other way: never convert this paper again.
                    if job and job["stage"] != "done":
                        self.store.remove(key)
                    paper.update(status="已有 Markdown", detail="条目已关联 Markdown，跳过转换")
                    continue
                if not pdf:
                    paper.update(status="暂无 PDF", detail="已勾选，PDF 加入后转换" if paper["selected"]
                                 else "条目下还没有 PDF 附件")
                    continue
                if not paper["selected"]:
                    paper.update(status="未勾选", detail="勾选后打开“开始转换”开关才会转换")
                    continue
                if job and job["stage"] == "done":
                    # Its Markdown was removed in Zotero and the user ticked it again: convert afresh.
                    self.store.remove(key)
                    job = None
                if job:
                    if job["stage"] in ACTIVE:
                        allowed.add(key)
                    continue
                paper.update(status="已勾选", detail="打开“开始转换”开关后开始转换" if not enqueue else "")
                if enqueue and self._stable(paper):
                    self.store.enqueue(paper, self.options())
                    allowed.add(key)
            self.allowed = allowed
            if enqueue:
                self.batch |= allowed
            self.papers, self.scanned_at = papers, fetched
            self.publish(scanned=True)
        finally:
            self.emit("busy", "")

    def _maybe_finish(self):
        """Turn the switch off once every ticked paper of this batch has finished or failed."""
        if not self.watching:
            return
        jobs = self.store.jobs()
        if any(j["stage"] in ACTIVE and key in self.allowed for key, j in jobs.items()):
            return
        if any(p.get("settling") for p in self.papers):
            return  # a ticked PDF is still being written by Zotero; it will be queued on the next scan
        done = sum(jobs.get(key, {}).get("stage") == "done" for key in self.batch)
        failed = sum(jobs.get(key, {}).get("stage") == "failed" for key in self.batch)
        unavailable = sum(1 for p in self.papers if p.get("selected") and p.get("pdf_key") and not p["markdowns"]
                          and p["key"] not in jobs)
        parts = [f"成功 {done} 篇"]
        if failed:
            parts.append(f"失败 {failed} 篇（可点“重试失败”）")
        if unavailable:
            parts.append(f"{unavailable} 篇的 PDF 不在本机或不可用")
        message = ("本批转换已结束：" + "，".join(parts) if self.batch or unavailable else "没有需要转换的勾选论文") \
            + "。转换开关已自动关闭。"
        self.watching = False
        self.batch = set()
        self.emit("watching", False)
        self.log(message)
        self.emit("batch_done", {"done": done, "failed": failed, "message": message})
        self.publish()

    def _stable(self, paper: dict) -> bool:
        """Only submit PDFs that exist locally and have stopped changing (Zotero may still be downloading)."""
        try:
            path = self.zotero.file_path(paper["pdf_key"], paper["pdf_href"])
            if not path.is_file():
                raise ServiceError("等待 PDF 下载到本机（可在 Zotero 中双击条目下载）。", retryable=True)
            signature = check_pdf(path)
        except (ServiceError, OSError) as error:
            paper.update(status="等待 PDF" if getattr(error, "retryable", False) else "PDF 不可用", detail=str(error))
            return False
        observation = self.observed.get(paper["pdf_key"])
        if not observation or observation[:2] != signature:
            self.observed[paper["pdf_key"]] = observation = (*signature, time.monotonic())
        if time.monotonic() - observation[2] < self.stable_seconds:
            self.next_scan = min(self.next_scan, time.monotonic() + self.stable_seconds + 0.5)
            paper.update(status="等待 PDF", detail="PDF 刚刚加入，确认写入完成后自动转换", settling=True)
            return False
        return True

    def publish(self, scanned: bool = False):
        jobs = self.store.jobs()
        summaries = self.store.summaries()
        translations = self.store.translations()
        rows = []
        for paper in self.papers:
            row = dict(paper)
            row["md_display"] = "、".join(m["filename"] or m["title"] for m in paper["markdowns"]) or paper["md_name"]
            summary = summaries.get(paper["key"])
            row["summaries"] = paper.get("summaries") or self._attached_since_scan(
                summary, (summary or {}).get("meta", {}).get("summary_path"))
            if row["summaries"]:
                row.update(summary_status="已有总结", summary_detail="、".join(s["filename"] for s in row["summaries"]))
            elif summary and summary["stage"] == "done" and paper["markdowns"]:
                row.update(summary_status="未总结", summary_detail="上次生成的总结卡片已被删除，可重新生成")
            elif summary:
                row.update(summary_status=summary_label(summary), summary_detail=summary["error"] or summary["progress"])
            else:
                row.update(summary_status="—" if not paper["markdowns"] else "未总结", summary_detail="")
            translation = translations.get(paper["key"])
            row["original_pdf"] = any(not any(TRANSLATION.search(pdf.get(field) or "") for field in ("filename", "title"))
                                      for pdf in paper["pdfs"])
            row["translations"] = paper.get("translations") or self._attached_since_scan(
                translation, (translation or {}).get("file"))
            if row["translations"]:
                warnings = (translation or {}).get("meta", {}).get("warnings") or []
                row.update(translation_status="已有双语" + ("（需检查）" if warnings else ""),
                           translation_detail="、".join(t["filename"] for t in row["translations"])
                           + ("；需检查：" + "；".join(warnings) if warnings else ""))
            elif translation and translation["stage"] != "done":
                row.update(translation_status=translation_label(translation),
                           translation_detail=translation["error"] or translation["progress"])
            elif translation:
                row.update(translation_status="未翻译", translation_detail="上次生成的双语 PDF 已被删除，可重新翻译")
            else:
                row.update(translation_status="未翻译" if row["original_pdf"] else "—",
                           translation_detail="" if row["original_pdf"] or not paper["pdfs"] else "只有译文 PDF，没有原文")
            job = jobs.get(paper["key"])
            if job:
                stage = job["stage"]
                if stage == "done":
                    present = bool(paper["markdowns"])
                    row.update(status="已完成" if present else "已完成（MD 附件已移除）",
                               detail=job["progress"] if present else "如需重新生成，勾选后打开“开始转换”开关",
                               markdown=job["markdown"])
                elif not paper["selected"]:
                    row.update(status="未勾选", detail="已取消勾选，任务暂停")
                elif stage in STATES:
                    row.update(status=STATES[stage], detail=job["error"] or job["progress"])
                    if stage in ACTIVE and not (self.watching or paper["key"] in self.manual):
                        row["detail"] = (row["detail"] + " · " if row["detail"] else "") + "打开“开始转换”开关后继续"
            rows.append(row)
        # "scanned" marks rows that come straight from a Zotero scan (the GUI may then offer to convert new papers).
        self.emit("rows", {"rows": rows, "watching": self.watching, "scanned": scanned})

    def _attached_since_scan(self, job: dict | None, path: str | None) -> list[dict]:
        """A card or PDF the summary/translation worker attached after the last scan read Zotero.

        Without this the finished paper looked as if its file had been deleted ("未总结") until the next scan."""
        if not job or job["stage"] != "done" or not path or job["updated"] < self.scanned_at:
            return []
        return [{"key": job["result_key"], "filename": Path(path).name, "path": path}]

    # ------------------------------------------------------------------ manual actions
    def trash(self, paper_key: str, children: list[dict]):
        """Move a paper's attachments/notes to the Zotero trash; clear records so the work can be redone."""
        try:
            info = self.zotero.bridge("status")
        except ServiceError as error:
            self.emit("trash_done", {"paper": paper_key, "error": str(error)})
            return
        if info.get("protocol", 0) < BRIDGE_TRASH_PROTOCOL:
            self.emit("trash_done", {"paper": paper_key, "outdated": info.get("version") or "旧"})
            return
        result = self.zotero.bridge("trash", parent_key=paper_key, keys=[c["key"] for c in children])
        gone = set(result.get("trashed", []))
        kinds = {c["kind"] for c in children if c["key"] in gone}
        if "markdown" in kinds:
            # Without its Markdown the paper counts as unconverted again; ticking it and switching on converts it.
            self.store.remove(paper_key)
        if "translation" in kinds:
            self.store.remove_translation(paper_key)
        if "summary" in kinds:
            self.store.remove_summary(paper_key)
            try:
                from .summarizer import write_index
                write_index(self.store, Path(self.settings["output_root"]))
            except OSError as error:
                self.log("更新 AI 总结索引失败：" + str(error))
        names = "、".join(c["name"] for c in children if c["key"] in gone)
        self.log(f"已移入 Zotero 回收站：{names}" if names else "所选项目已不在 Zotero 中。")
        self.emit("trash_done", {"paper": paper_key, "kinds": sorted(kinds)})
        self.scan(enqueue=self.watching)

    # ------------------------------------------------------------------ job pipeline
    def process_one(self):
        if self.stopping.is_set():
            return
        permitted = (self.allowed if self.watching else set()) | self.manual
        jobs = [j for j in self.store.jobs().values() if j["stage"] in ACTIVE and j["key"] in permitted]
        cloud = sum(j["stage"] in CLOUD for j in jobs)
        now = time.time()
        ready = [j for j in jobs if j["next_try"] <= now and (j["stage"] != "queued" or cloud < MAX_CLOUD_JOBS)]
        if not ready:
            return
        ready.sort(key=lambda j: (j["stage"] == "queued", j["next_try"], j["created"]))
        job = ready[0]
        try:
            self.advance(job)
        except Exception as error:
            current = self.store.job(job["key"])
            if current is None or current["stage"] not in ACTIVE:
                return
            attempt = job["attempts"] + 1
            message = safe_message(str(error) or type(error).__name__, self.token)
            if isinstance(error, ServiceError) and error.retryable and attempt <= 6:
                self.store.update(job["key"], attempts=attempt, error=message + f"（第 {attempt} 次自动重试）",
                                  next_try=time.time() + min(300, 5 * 2 ** (attempt - 1)))
            else:
                self.store.update(job["key"], stage="failed", resume_stage=getattr(error, "resume_stage", None) or job["stage"],
                                  error=message, attempts=attempt)
                self.manual.discard(job["key"])
            self.log(f"{job['title']}：{message}")
            if isinstance(error, ServiceError) and error.auth:
                self.watching = False
                self.manual.clear()
                self.emit("watching", False)
                self.emit("error", message)
        finally:
            self.publish()

    def _advance_to(self, job: dict, stage: str, **values):
        self.store.update(job["key"], stage=stage, resume_stage=stage, attempts=0, error="", next_try=0, **values)

    def _pdf_path(self, job: dict) -> Path:
        item = self.zotero.item(job["pdf_key"])
        data = item.get("data", {})
        if data.get("deleted") or data.get("parentItem") != job["key"]:
            raise ServiceError("原 PDF 已删除或移到其他条目，重试时将重新选择 PDF。", resume_stage="queued")
        path = self.zotero.file_path(job["pdf_key"], item.get("links", {}).get("enclosure", {}).get("href", ""))
        if not path.is_file():
            raise ServiceError("PDF 暂不在本机，等待文件恢复。", retryable=True)
        check_pdf(path)
        return path

    def advance(self, job: dict):
        stage, key = job["stage"], job["key"]
        if stage == "queued":
            children = [c for c in self.zotero.children(key) if not c["data"].get("deleted")]
            attachments = [c for c in children if c["data"].get("itemType") == "attachment"]
            if any(is_markdown_attachment(c["data"]) for c in attachments):
                self.store.remove(key)
                self.manual.discard(key)
                self.log(f"《{job['title']}》已关联 Markdown，跳过转换。")
                return
            pdfs = [c for c in attachments if c["data"].get("contentType") == "application/pdf"
                    and c["data"].get("linkMode") != "linked_url"]
            if not any(c["key"] == job["pdf_key"] for c in pdfs):
                if not pdfs:
                    raise ServiceError("条目下已没有 PDF 附件。")
                chosen = choose_pdf({"url": "", "pdfs": [{"key": c["key"], "filename": c["data"].get("filename", ""),
                    "title": c["data"].get("title", ""), "url": c["data"].get("url", ""),
                    "date_added": c["data"].get("dateAdded", "")} for c in pdfs]})
                self.store.update(key, pdf_key=chosen["key"], pdf_name=chosen["filename"])
                job = self.store.job(key)
            path = self._pdf_path(job)
            pages = page_count(path)
            ranges = page_ranges(pages)
            stamp = uuid.uuid4().hex[:12]
            parts = [{"name": f"{key}.pdf", "data_id": f"{key}_{stamp}_{n + 1}", "page_ranges": r,
                      "uploaded": False, "zip_url": "", "zip": ""} for n, r in enumerate(ranges)]
            batch, urls = self.mineru.submit(parts, job["options"] or self.options())
            for part, url in zip(parts, urls):
                part["upload_url"] = url
            note = f"（共 {pages} 页，分 {len(parts)} 段解析）" if len(parts) > 1 else ""
            self._advance_to(job, "upload", batch_id=batch, parts=parts, progress="已创建云端任务" + note)
            self.log(f"已提交 MinerU：{job['title']}{note}")
        elif stage == "upload":
            parts = job["parts"]
            rows = self.mineru.results(job["batch_id"])
            path = None
            for part in parts:
                if part["uploaded"]:
                    continue
                row = rows.get(part["data_id"], {})
                if row.get("state") == "failed":
                    raise ServiceError("MinerU 任务失败：" + safe_message(row.get("err_msg", ""), self.token), resume_stage="queued")
                if row.get("state", "waiting-file") == "waiting-file":
                    path = path or self._pdf_path(job)
                    self.mineru.upload(part["upload_url"], path)
                part["uploaded"] = True
                self.store.update(key, parts=parts)
            self._advance_to(job, "parsing", parts=parts, progress="PDF 已上传，等待云端解析")
        elif stage == "parsing":
            parts = job["parts"]
            rows = self.mineru.results(job["batch_id"])
            states, done_pages, total_pages = [], 0, 0
            for part in parts:
                row = rows.get(part["data_id"]) or (next(iter(rows.values())) if len(rows) == 1 and len(parts) == 1 else {})
                state = row.get("state", "waiting-file")
                if state == "failed":
                    raise ServiceError("MinerU 解析失败：" + safe_message(row.get("err_msg", "未知原因"), self.token), resume_stage="queued")
                if state == "done":
                    if not row.get("full_zip_url"):
                        raise ServiceError("MinerU 已完成但未返回结果下载地址。", retryable=True)
                    part["zip_url"] = row["full_zip_url"]
                progress = row.get("extract_progress") or {}
                done_pages += int(progress.get("extracted_pages") or 0)
                total_pages += int(progress.get("total_pages") or 0)
                states.append(state)
            if all(s == "done" for s in states):
                self._advance_to(job, "download", parts=parts, progress="云端解析完成，准备下载")
            elif "waiting-file" in states:
                # A successful PUT may precede MinerU's background upload scan.
                if job["attempts"] >= 24:
                    for part, state in zip(parts, states):
                        if state == "waiting-file":
                            part["uploaded"] = False
                    self.store.update(key, parts=parts)
                    raise ServiceError("PDF 已上传但服务端迟迟未接收，点击“重试失败”将重新上传。", resume_stage="upload")
                self.store.update(key, attempts=job["attempts"] + 1, next_try=time.time() + 5,
                                  progress="PDF 已上传，等待服务端接收")
            else:
                labels = {"pending": "云端排队中", "running": "云端解析中", "converting": "云端格式转换"}
                text = labels.get(next((s for s in states if s != "done"), ""), "云端处理中")
                if total_pages:
                    text += f" · {done_pages}/{total_pages} 页"
                self.store.update(key, progress=text, error="", attempts=0, next_try=time.time() + 5)
        elif stage == "download":
            parts = job["parts"]
            for n, part in enumerate(parts):
                target = self.work_dir / key / f"part{n + 1}.zip"
                if part.get("zip") and Path(part["zip"]).is_file():
                    continue
                self.mineru.download(part["zip_url"], target, self.stopping.is_set)
                part["zip"] = str(target)
                self.store.update(key, parts=parts, progress=f"已下载结果 {n + 1}/{len(parts)}")
            self._advance_to(job, "build", parts=parts, progress="结果已下载")
        elif stage == "build":
            pieces, embedded, missing = [], 0, 0
            for part in job["parts"]:
                archive = Path(part.get("zip") or "")
                if not archive.is_file():
                    self._advance_to(job, "download", parts=[{**p, "zip": ""} for p in job["parts"]])
                    return
                piece, e, m = markdown_from_zip(archive)
                pieces.append((part.get("page_ranges") or "全部", piece))
                embedded, missing = embedded + e, missing + m
            text = join_parts(pieces)
            name = job["md_name"] or markdown_filename(fallback_base(job["creator"], job["title"], job["pdf_name"]))
            output_root = Path(self.settings["output_root"])
            if self.settings["storage_mode"] == "link":
                previous = Path(job["markdown"]) if job["markdown"] else None
                target = previous if previous and previous.parent == output_root else unique_path(output_root, name, key)
            else:
                target = self.work_dir / key / name
            write_text(target, text)
            note = f"内嵌 {embedded} 张图片" + (f"，{missing} 张图片缺失" if missing else "")
            if missing:
                self.log(f"{job['title']}：结果中有 {missing} 张图片未找到，保留原链接。")
            self._advance_to(job, "attach", markdown=str(target), progress="Markdown 已生成（" + note + "）")
        elif stage == "attach":
            markdown = Path(job["markdown"])
            if not markdown.is_file():
                self._advance_to(job, "build", markdown="")
                return
            output_root = Path(self.settings["output_root"]).resolve()
            mode = "link" if markdown.parent.resolve() == output_root else "import"
            result = self.zotero.bridge("attach", parent_key=key, file=str(markdown), mode=mode, title=markdown.stem)
            final = Path(result.get("path") or markdown)
            if result.get("skipped"):
                if mode == "link":
                    markdown.unlink(missing_ok=True)
                progress = "条目中已有 Markdown，未重复添加"
            else:
                progress = "已添加到 Zotero：" + final.name
                if mode == "import" and self.settings["export_copy"]:
                    output_root.mkdir(parents=True, exist_ok=True)
                    copy_path = unique_path(output_root, markdown.name, key)
                    shutil.copy2(markdown, copy_path)
                    progress += "（副本已保存到输出目录）"
            shutil.rmtree(self.work_dir / key, ignore_errors=True)
            self._advance_to(job, "done", result_key=result.get("key", ""), markdown=str(final), progress=progress)
            # Untick finished papers so deleting the Markdown later never triggers a silent re-conversion.
            self.store.set_selected(key, False)
            for paper in self.papers:
                if paper["key"] == key:
                    # Reflect the new attachment in the list until the next Zotero scan.
                    paper["markdowns"].append({"key": result.get("key", ""), "filename": final.name, "title": final.stem,
                                               "path": str(final), "href": final.as_uri(), "link_mode": mode})
            self.manual.discard(key)
            self.log("转换完成：" + job["title"] + " → " + final.name)
            if self.settings.get("summary_auto") and not result.get("skipped"):
                # The summarizer thread picks this up and deep-reads the new Markdown.
                if self.store.enqueue_summary(key, job["title"], again=True):
                    self.emit("summaries", None)
