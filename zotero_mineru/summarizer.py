"""Background worker that turns a paper's Markdown into an AI summary card attached beside it in Zotero."""
from __future__ import annotations

import copy
import json
import math
import os
import queue
import shutil
import threading
import time
from datetime import datetime
from pathlib import Path

from .clients import ServiceError, ZoteroClient, file_url_to_path, is_markdown_attachment, is_summary_attachment, safe_message
from .config import BRIDGE_MIN_PROTOCOL
from .llm import LLMClient
from .markdown import fallback_base, markdown_filename, unique_path, write_text
from .summary import Cancelled, card_words, fit_card, note_html, render_card, summarize

AUTHOR_TYPES = {"author", "inventor", "programmer", "artist", "presenter", "director", "interviewee", "cartographer"}
INDEX_MD, INDEX_JSONL = "AI总结索引.md", "ai_summaries.jsonl"
OLD_PLUGIN = "Zotero 中的插件需要更新（设置页 → 安装/更新 Zotero 插件），更新后自动继续"


def paper_meta(item: dict, markdown: Path) -> dict:
    data = item.get("data", {})
    creators = data.get("creators", [])
    authors = [c.get("name") or f"{c.get('firstName', '')} {c.get('lastName', '')}".strip()
               for c in creators if c.get("creatorType") in AUTHOR_TYPES] or \
              [c.get("name") or f"{c.get('firstName', '')} {c.get('lastName', '')}".strip() for c in creators]
    date = item.get("meta", {}).get("parsedDate") or data.get("date", "")
    journal = next((data.get(f) for f in ("publicationTitle", "proceedingsTitle", "bookTitle", "university",
                                           "institution", "publisher", "websiteTitle") if data.get(f)), "")
    return {"key": item.get("key", ""), "title": data.get("title", ""), "authors": [a for a in authors if a],
            "creator": item.get("meta", {}).get("creatorSummary", ""), "year": str(date)[:4], "journal": journal,
            "doi": data.get("DOI", ""), "url": data.get("url", ""),
            "markdown_name": markdown.name, "markdown_path": str(markdown)}


def attachment_links(children: list[dict], item: dict) -> dict:
    """Keys and paths of the paper's PDF and full-text Markdown, so the card can point back to both."""
    from .engine import choose_pdf
    info = {}
    pdfs, markdown = [], None
    for child in children:
        data = child["data"]
        path = file_url_to_path(child.get("links", {}).get("enclosure", {}).get("href", "")) \
            or (Path(data["path"]) if data.get("path") else None)
        if data.get("contentType") == "application/pdf" and data.get("linkMode") != "linked_url":
            pdfs.append({"key": child["key"], "filename": data.get("filename") or (path.name if path else ""),
                         "title": data.get("title", ""), "url": data.get("url", ""), "date_added": data.get("dateAdded", ""),
                         "path": str(path or "")})
        elif markdown is None and is_markdown_attachment(data) and path and path.is_file():
            markdown = (child["key"], path)
    pdf = choose_pdf({"url": item.get("data", {}).get("url", ""), "pdfs": pdfs})
    if pdf:
        info.update(pdf_key=pdf["key"], pdf_name=pdf["filename"], pdf_path=pdf["path"])
    if markdown:
        info.update(markdown_key=markdown[0], markdown_name=markdown[1].name, markdown_path=str(markdown[1]))
    return info


def relative_markdown(meta: dict, card_folder: Path | None) -> str:
    """Path from the card to the full-text Markdown (both sit in Zotero's storage folder, or both in the output folder)."""
    target = Path(meta.get("markdown_path") or "")
    if not target.name:
        return ""
    if card_folder is None:  # imported: the card will live in storage/<new key>/
        return f"../{target.parent.name}/{target.name}" if target.parent.parent.name.lower() == "storage" else target.as_uri()
    try:
        return os.path.relpath(target, card_folder).replace("\\", "/")
    except ValueError:  # different drive
        return target.as_uri()


def write_index(store, output_root: Path) -> None:
    """A compact library-wide index an AI agent can scan first, before opening single cards or full texts."""
    entries = []
    for job in store.summaries().values():
        if job["stage"] != "done" or not job["card"] or not Path(job["meta"].get("summary_path", "")).name.upper().startswith("SUM_"):
            continue
        meta, card = job["meta"], job["card"]
        entries.append({"zotero_key": job["key"], "title": meta.get("title", ""), "authors": meta.get("authors", []),
                        "year": meta.get("year", ""), "journal": meta.get("journal", ""), "doi": meta.get("doi", ""),
                        "summary": card.get("one_line", ""), "keywords": card.get("keywords", []),
                        "summary_file": meta.get("summary_path", ""), "fulltext_markdown": meta.get("markdown_path", ""),
                        "pdf_file": meta.get("pdf_path", ""), "word_count": card_words(card),
                        "zotero_link": f"zotero://select/library/items/{job['key']}",
                        "pdf_link": f"zotero://open-pdf/library/items/{meta['pdf_key']}" if meta.get("pdf_key") else ""})
    entries.sort(key=lambda e: (e["year"] or "0000", e["title"]), reverse=True)
    output_root.mkdir(parents=True, exist_ok=True)
    lines = ["# Zotero 文献 AI 总结索引", "",
             "供 AI 助手检索：先在本索引中按关键词和一句话总结筛选相关论文，再打开对应的总结文件；需要细节时再读 Markdown 全文。",
             f"共 {len(entries)} 篇，更新于 {datetime.now():%Y-%m-%d %H:%M}。", ""]
    for e in entries:
        lines += [f"## {e['title']}",
                  f"- 作者：{'; '.join(e['authors'][:6])} ｜ 年份：{e['year']} ｜ 来源：{e['journal']}",
                  f"- 一句话总结：{e['summary']}", f"- 关键词：{'；'.join(e['keywords'])}",
                  f"- 总结文件：{e['summary_file']}", f"- 全文 Markdown：{e['fulltext_markdown']}",
                  f"- 论文 PDF：{e['pdf_file']}", f"- Zotero：{e['zotero_link']}", ""]
    for name, content in ((INDEX_MD, "\n".join(lines)),
                          (INDEX_JSONL, "".join(json.dumps(e, ensure_ascii=False) + "\n" for e in entries))):
        temporary = output_root / ("." + name + ".tmp")
        temporary.write_text(content, encoding="utf-8")
        os.replace(temporary, output_root / name)


class Summarizer(threading.Thread):
    def __init__(self, store, settings: dict, keys: dict, bridge_token: str, work_dir: Path, emit,
                 *, zotero_factory=ZoteroClient, llm_factory=LLMClient):
        super().__init__(name="zotero-mineru-summarizer", daemon=True)
        self.store, self.emit = store, emit
        self.settings, self.keys = copy.deepcopy(settings), dict(keys)
        self.bridge_token = bridge_token
        self.work_dir = Path(work_dir) / "summaries"
        self.zotero_factory, self.llm_factory = zotero_factory, llm_factory
        self.zotero = zotero_factory(self.settings["zotero_port"], bridge_token)
        self.commands = queue.Queue()
        self.stopping = threading.Event()
        # Key of the paper whose full text the model is reading now ("" when idle); read by the window.
        self.reading = ""
        # (model calls finished, calls planned, when the current one started) for that paper's progress bar.
        self.reading_steps = (0, 1, time.monotonic())
        # Running estimate of how long one model call takes; the model reports nothing while it writes.
        self.call_seconds = 60.0

    def command(self, action: str, data=None):
        self.commands.put((action, data))

    def reading_percent(self) -> int | None:
        """A rough percentage for the paper being read: finished calls plus time spent in the current one."""
        if not self.reading:
            return None
        done, total, started = self.reading_steps
        current = min(0.95, 1 - math.exp(-1.5 * (time.monotonic() - started) / max(self.call_seconds, 1)))
        return max(1, min(99, int(100 * (done + current) / max(total, 1))))

    def _step(self, done: int, total: int):
        finished, _, started = self.reading_steps
        now = time.monotonic()
        if done > finished:  # a call has just returned: learn how long calls take with this model
            self.call_seconds = 0.6 * self.call_seconds + 0.4 * (now - started) / (done - finished)
        self.reading_steps = (done, total, now)

    def cancel(self, *, current: bool = False) -> int:
        """Called from the window and effective at once, even while this thread waits for the model.

        Queued papers are dropped; with `current` the paper being read is dropped too and makes no further model
        calls (a request already sent cannot be recalled). Returns the number of queued papers dropped."""
        jobs = self.store.summaries()
        reading = self.reading if jobs.get(self.reading, {}).get("stage") == "queued" else ""
        title = jobs[reading]["title"] if reading else ""
        dropped = self.store.cancel_summaries(keep="" if current else reading)
        queued = dropped - (1 if current and reading else 0)
        if current and reading:
            self.log("已停止 AI 总结：" + (f"取消排队中的 {queued} 篇；" if queued else "")
                     + f"正在阅读的《{title}》不再继续调用大模型。")
        else:
            self.log(f"已取消 {queued} 篇排队中的 AI 总结" + (f"；正在阅读的《{title}》会写完并加入 Zotero。" if reading else "。"))
        self.emit("summaries", None)
        return queued

    def close(self):
        self.stopping.set()
        self.commands.put(("stop", None))

    def log(self, value):
        self.emit("log", safe_message(value, self.keys.get(self.settings["llm_provider"], "")))

    def client(self):
        provider = self.settings["llm_provider"]
        return self.llm_factory(provider, self.keys.get(provider, ""), self.settings["llm_models"].get(provider, ""),
                                self.settings["llm_bases"].get(provider, ""))

    def repair(self):
        """Re-attach cards that an outdated Zotero plugin silently refused (2.1–2.3 marked them done)."""
        for job in self.store.summaries().values():
            path = job["meta"].get("summary_path", "")
            if job["stage"] == "done" and job["card"] and path and not Path(path).name.upper().startswith("SUM_"):
                self.store.update_summary(job["key"], stage="attach", resume_stage="attach", file="", next_try=0,
                                          progress="总结已生成但尚未加入 Zotero，正在补加（不再调用大模型）")
            elif job["stage"] == "done" and job["card"] and job["result_key"] and "pdf_key" not in job["meta"]:
                # Cards from 2.1–2.3.1 lack the PDF/full-text links: rebuild from the saved text and replace the file.
                self.store.update_summary(job["key"], stage="attach", resume_stage="attach", file="", next_try=0,
                                          meta=dict(job["meta"], replace_key=job["result_key"]),
                                          progress="更新总结卡片：加入与条目、PDF、全文 Markdown 的关联（不再调用大模型）")

    def _plugin_ready(self, key: str) -> bool:
        """Summary cards need the 2.1 plugin; an older one would drop them. Wait instead of spending model tokens."""
        try:
            ready = self.zotero.bridge("status").get("protocol", 0) >= BRIDGE_MIN_PROTOCOL
        except ServiceError as error:
            self.store.update_summary(key, progress="等待连接 Zotero：" + str(error), next_try=time.time() + 30)
            return False
        if not ready:
            self.store.update_summary(key, progress=OLD_PLUGIN, next_try=time.time() + 30)
        return ready

    def run(self):
        try:
            self.repair()
        except Exception as error:
            self.log("检查 AI 总结记录失败：" + str(error))
        while not self.stopping.is_set():
            try:
                action, data = self.commands.get(timeout=0.5)
                if action == "stop":
                    break
                self.handle(action, data)
            except queue.Empty:
                pass
            except Exception as error:
                self.emit("llm_probe", safe_message(str(error)))
            try:
                self.process_one()
            except Exception as error:
                self.log("AI 总结队列错误：" + str(error))

    def handle(self, action: str, data):
        if action == "configure":
            self.settings, self.keys = copy.deepcopy(data[0]), dict(data[1])
            self.zotero = self.zotero_factory(self.settings["zotero_port"], self.bridge_token)
        elif action == "enqueue":
            # The window only offers papers without a card, so an earlier finished summary was deleted: redo it.
            count = sum(self.store.enqueue_summary(key, title, again=True) for key, title in data)
            self.log(f"已将 {count} 篇论文加入 AI 总结队列（{self.settings['llm_provider']}，"
                     f"{'精读全文' if self.settings['summary_depth'] == 'deep' else '快速通读'}）。")
            self.emit("summaries", None)
        elif action == "probe":
            self.emit("llm_probe", self.client().probe())
        elif action == "models":
            models = self.client().models()
            self.emit("llm_models", models)
            self.emit("llm_probe", f"已获取 {len(models)} 个模型。")

    def process_one(self):
        if self.stopping.is_set():
            return
        now = time.time()
        jobs = [j for j in self.store.summaries().values() if j["stage"] in ("queued", "attach") and j["next_try"] <= now]
        if not jobs:
            return
        job = min(jobs, key=lambda j: (j["stage"] != "attach", j["created"]))
        self.reading_steps = (0, 1, time.monotonic())
        self.reading = job["key"] if job["stage"] == "queued" else ""
        try:
            self.advance(job)
        except Cancelled:
            return  # stopped in the window, which already removed the job and logged it
        except Exception as error:
            current = self.store.summaries().get(job["key"])
            if not current or current["stage"] not in ("queued", "attach"):
                return
            attempt = job["attempts"] + 1
            message = safe_message(str(error) or type(error).__name__, self.keys.get(self.settings["llm_provider"], ""))
            if isinstance(error, ServiceError) and error.retryable and attempt <= 3:
                self.store.update_summary(job["key"], attempts=attempt, error=message, next_try=time.time() + 60 * attempt)
            else:
                self.store.update_summary(job["key"], stage="failed", resume_stage=job["stage"], error=message, attempts=attempt)
            self.log(f"AI 总结失败：{job['title']}：{message}")
        finally:
            self.reading = ""
            self.emit("summaries", None)

    def _progress(self, key: str, text: str):
        self.store.update_summary(key, progress=text)
        self.emit("summaries", None)

    def advance(self, job: dict):
        key = job["key"]
        if job["stage"] == "queued":
            provider = self.settings["llm_provider"]
            if not self.keys.get(provider) and provider != "ollama":
                self.store.update_summary(key, progress="等待在设置中填写大模型 API Key", next_try=time.time() + 30)
                return
            if not self._plugin_ready(key):
                return
            item = self.zotero.item(key)
            children = [c for c in self.zotero.children(key)
                        if c["data"].get("itemType") == "attachment" and not c["data"].get("deleted")]
            if any(is_summary_attachment(c["data"]) for c in children):
                self.store.update_summary(key, stage="done", progress="条目已有 AI 总结，跳过")
                self.store.set_selected(key, False)
                self.emit("summary_attached", key)  # the list may not know this card yet
                return
            markdown = None
            for child in children:
                if is_markdown_attachment(child["data"]):
                    path = file_url_to_path(child.get("links", {}).get("enclosure", {}).get("href", "")) \
                        or (Path(child["data"]["path"]) if child["data"].get("path") else None)
                    if path and path.is_file():
                        markdown = path
                        break
            if markdown is None:
                raise ServiceError("条目还没有可读取的 Markdown 全文，请先转换。")
            meta = dict(paper_meta(item, markdown), **attachment_links(children, item))
            client = self.client()
            self._progress(key, f"正在阅读（{client.display}）…")
            card, usage = summarize(client, meta, markdown.read_text(encoding="utf-8", errors="replace"),
                                    lang=self.settings["summary_language"], depth=self.settings["summary_depth"],
                                    budget=self.settings["summary_budget"], part_tokens=self.settings["summary_part_tokens"],
                                    max_output=self.settings["summary_max_output"] or None,
                                    max_words=self.settings.get("summary_max_words", 3000),
                                    vision=self.settings.get("summary_vision", False),
                                    vision_max=self.settings.get("summary_vision_max", 30), base_dir=markdown.parent,
                                    log=lambda text: self._progress(key, text), step=self._step,
                                    cancelled=lambda: key not in self.store.summaries())
            meta["figures_viewed"] = usage.get("images", 0)
            seen = f"，看图 {meta['figures_viewed']} 张" if meta["figures_viewed"] else ""
            if key not in self.store.summaries():
                return  # cancelled while the model was reading
            stem = markdown.stem
            base = stem[3:] if stem.upper().startswith("MD_") else fallback_base(meta["creator"], meta["title"], markdown.name)
            name = markdown_filename(base, prefix="SUM_")
            output_root = Path(self.settings["output_root"])
            if self.settings["storage_mode"] == "link":
                target = unique_path(output_root, name, key)
            else:
                target = self.work_dir / key / name
            meta["markdown_rel"] = relative_markdown(meta, target.parent if self.settings["storage_mode"] == "link" else None)
            write_text(target, render_card(meta, card, self.settings["summary_language"], client.display))
            self.store.update_summary(key, stage="attach", resume_stage="attach", file=str(target), card=card, meta=meta,
                                      model=client.display, attempts=0, error="", next_try=0,
                                      progress=f"已生成（{usage['calls']} 次调用{seen}，输入约 {usage['input']} / 输出 {usage['output']} tokens）")
            self.log(f"AI 总结已生成：{meta['title']}（{client.display}，{usage['calls']} 次调用{seen}，"
                     f"输入 {usage['input']} / 输出 {usage['output']} tokens）")
        elif job["stage"] == "attach":
            if not self._plugin_ready(key):
                return
            output_root = Path(self.settings["output_root"])
            target = Path(job["file"]) if job["file"] else None
            if not (target and target.is_file()):
                if not job["card"]:
                    self.store.update_summary(key, stage="queued", resume_stage="queued", file="")
                    return
                # The card text is kept in the task database, so the file can be rebuilt without calling the model.
                if "pdf_key" not in job["meta"]:
                    children = [c for c in self.zotero.children(key)
                                if c["data"].get("itemType") == "attachment" and not c["data"].get("deleted")]
                    job["meta"].update(attachment_links(children, self.zotero.item(key)))
                limit = self.settings.get("summary_max_words", 3000)
                if limit and card_words(job["card"]) > limit:
                    try:
                        client = self.client()
                        job["card"] = fit_card(lambda prompt: client.chat("你是严谨的科研文献编辑。", prompt)[0], job["card"],
                                               self.settings["summary_language"], limit, lambda text: self._progress(key, text))
                    except ServiceError:
                        job["card"] = fit_card(None, job["card"], self.settings["summary_language"], limit)
                    self.store.update_summary(key, card=job["card"])
                markdown = job["meta"].get("markdown_name", "")
                stem = Path(markdown).stem
                base = stem[3:] if stem.upper().startswith("MD_") else \
                    fallback_base(job["meta"].get("creator", ""), job["meta"].get("title", ""), markdown)
                name = markdown_filename(base, prefix="SUM_")
                target = unique_path(output_root, name, key) if self.settings["storage_mode"] == "link" \
                    else self.work_dir / key / name
                job["meta"]["markdown_rel"] = relative_markdown(
                    job["meta"], target.parent if self.settings["storage_mode"] == "link" else None)
                self.store.update_summary(key, meta=job["meta"])
                write_text(target, render_card(job["meta"], job["card"], self.settings["summary_language"], job["model"]))
            mode = "link" if target.parent.resolve() == output_root.resolve() else "import"
            lang = self.settings["summary_language"]
            replace = job["meta"].get("replace_key", "")
            # When refreshing an existing card the earlier note stays; do not add a second one.
            note = note_html(job["meta"], job["card"], lang) if self.settings["summary_note"] and not replace else ""
            result = self.zotero.bridge("attach", kind="summary", parent_key=key, file=str(target), mode=mode,
                                        title=target.stem, replace_key=replace, note_html=note)
            final = Path(result.get("path") or target)
            if not final.name.upper().startswith("SUM_"):
                # Only an outdated plugin answers a summary request with the paper's Markdown.
                self.store.update_summary(key, file=str(target), progress=OLD_PLUGIN, next_try=time.time() + 30)
                return
            meta = dict(job["meta"], summary_path=str(final))
            meta.pop("replace_key", None)
            progress = "条目已有 AI 总结，未重复添加" if result.get("skipped") else "已添加到 Zotero：" + final.name
            if not result.get("skipped") and mode == "import" and self.settings["export_copy"]:
                output_root.mkdir(parents=True, exist_ok=True)
                shutil.copy2(target, unique_path(output_root, target.name, key))
            if mode == "import":
                shutil.rmtree(self.work_dir / key, ignore_errors=True)
            self.store.update_summary(key, stage="done", result_key=result.get("key", ""),
                                      note_key=result.get("note_key", "") or job["note_key"],
                                      meta=meta, progress=progress, error="")
            self.store.set_selected(key, False)
            try:
                write_index(self.store, output_root)
            except OSError as error:
                self.log("更新 AI 总结索引失败：" + str(error))
            self.log("AI 总结已添加到 Zotero：" + final.name)
            self.emit("summary_attached", key)
