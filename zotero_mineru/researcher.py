"""Background worker for collection reviews and new-paper tracking, run on demand or on a schedule.

Reviews go bottom-up through the collections the user chose (each with all its sub-collections): a collection is
re-reviewed only when its papers, their summary cards or a sub-collection's review changed. Files are written to
<output folder>/研究地图/<collection path>/: RES_<name>.md (review), MAP_<name>.html (research map), TRK_<name>.md
(tracking), plus 研究地图索引.md and research_index.jsonl at the top for AI agents.
"""
from __future__ import annotations

import copy
import hashlib
import json
import os
import queue
import threading
import time
from datetime import date, datetime, timedelta
from pathlib import Path

from .clients import SKIP_TYPES, ServiceError, ZoteroClient, file_url_to_path, is_markdown_attachment, \
    is_summary_attachment, safe_message
from .llm import PROVIDERS, LLMClient
from .markdown import valid_filename, write_text
from .research import (PROMPT_VERSION, SYSTEM, card_body, child_block, estimate_tokens, paper_block,
                       paper_label, render_map, render_markdown, review)
from .scholar import ScholarClient, normalize_doi, normalize_title, short_id
from .tracking import render_tracking, track

RESEARCH_DIR = "研究地图"
INDEX_MD, INDEX_JSONL = "研究地图索引.md", "research_index.jsonl"
DAY = 86400
REFS_MAX_AGE = 120 * DAY


def safe_name(name: str, limit: int = 40) -> str:
    return valid_filename(name)[:limit].rstrip(" .") or "_"


def relative(target: Path, start: Path) -> str:
    try:
        return os.path.relpath(target, start).replace("\\", "/")
    except ValueError:
        return target.resolve().as_uri()


def research_model(settings: dict) -> tuple[str, str, str]:
    """(provider, model, base) for reviews: the research choice, falling back to the AI summary settings."""
    provider = settings.get("research_provider") or settings["llm_provider"]
    if provider not in PROVIDERS:
        provider = settings["llm_provider"]
    model = settings.get("research_model") or settings["llm_models"].get(provider, "")
    return provider, model, settings["llm_bases"].get(provider, "")


class Researcher(threading.Thread):
    def __init__(self, store, settings: dict, keys: dict, scholar_keys: dict, bridge_token: str, emit,
                 *, zotero_factory=ZoteroClient, llm_factory=LLMClient, scholar_factory=ScholarClient):
        super().__init__(name="zotero-mineru-researcher", daemon=True)
        self.store, self.emit = store, emit
        self.settings, self.keys, self.scholar_keys = copy.deepcopy(settings), dict(keys), dict(scholar_keys)
        self.bridge_token = bridge_token
        self.zotero_factory, self.llm_factory, self.scholar_factory = zotero_factory, llm_factory, scholar_factory
        self.commands = queue.Queue()
        self.stopping = threading.Event()
        self.cancelled = threading.Event()
        self.running = ""
        self.last_check = 0.0
        self.progress = ""

    # ---------------------------------------------------------------- control
    def command(self, action: str, data=None):
        self.commands.put((action, data))

    def cancel(self):
        self.cancelled.set()

    def close(self):
        self.stopping.set()
        self.cancelled.set()
        self.commands.put(("stop", None))

    def log(self, value):
        provider = research_model(self.settings)[0]
        secrets = [self.keys.get(provider, ""), *self.scholar_keys.values()]
        text = str(value)
        for secret in secrets:
            text = safe_message(text, secret) if secret else text
        self.emit("log", text)

    def step(self, text: str):
        self.progress = text
        self.emit("research_progress", text)

    def run(self):
        self.publish()
        while not self.stopping.is_set():
            try:
                action, data = self.commands.get(timeout=1)
            except queue.Empty:
                action, data = None, None
            if action == "stop":
                break
            try:
                if action:
                    self.handle(action, data)
                if time.time() - self.last_check >= 60:
                    self.last_check = time.time()
                    self.scheduled()
            except Exception as error:  # a worker must survive any single failure
                self.log("研究综述任务出错：" + str(error))
                self.running = ""
                self.publish()

    def handle(self, action: str, data):
        if action == "configure":
            self.settings, self.keys, self.scholar_keys = copy.deepcopy(data[0]), dict(data[1]), dict(data[2])
            self.publish()
        elif action == "publish":
            self.publish()
        elif action == "estimate":
            self.emit("research_estimate", self.guarded("estimate", lambda: self.review_all(estimate=True)))
        elif action == "review":
            self.guarded("review", lambda: self.review_all(force_keys=set(data or ())))
        elif action == "track":
            self.guarded("track", self.track_all)
        elif action == "both":
            if self.guarded("review", self.review_all) is not None and self.settings.get("tracking_enabled", True):
                self.guarded("track", self.track_all)

    def guarded(self, kind: str, work):
        """Run one job with the running flag set; errors are logged and retried later by the schedule."""
        self.cancelled.clear()
        self.running = kind
        self.publish()
        try:
            return work()
        except ServiceError as error:
            self.log(("研究综述" if kind != "track" else "新文献追踪") + "未完成：" + str(error))
            self.store.set_value(f"{kind}_retry", time.time() + 3600)
            return None
        finally:
            self.running = ""
            self.step("")
            self.publish()

    def scheduled(self):
        """Run what is due: reviews every N days (only changed collections are re-read), tracking every M days."""
        s = self.settings
        if not s.get("research_enabled") or not s.get("research_roots") or self.running:
            return
        now = time.time()
        due = lambda kind, days: (now - float(self.store.get_value(f"{kind}_last", "0") or 0) >= days * DAY  # noqa: E731
                                  and now >= float(self.store.get_value(f"{kind}_retry", "0") or 0))
        if due("review", s.get("research_interval_days", 7)):
            self.log("到达研究综述的定期更新时间，开始检查各分类是否有变化…")
            if self.guarded("review", self.review_all) is None:
                return
        if s.get("tracking_enabled", True) and due("track", s.get("tracking_interval_days", 7)):
            self.log("到达新文献追踪的定期执行时间，开始追踪…")
            self.guarded("track", self.track_all)

    # ---------------------------------------------------------------- collections and papers
    def llm(self):
        provider, model, base = research_model(self.settings)
        if provider != "ollama" and not self.keys.get(provider):
            raise ServiceError(f"请先在“设置 → AI 论文总结”中填写 {PROVIDERS[provider][0]} 的 API Key。")
        return self.llm_factory(provider, self.keys.get(provider, ""), model, base)

    def scholar(self):
        return self.scholar_factory(self.scholar_keys.get("s2", ""), self.scholar_keys.get("openalex", ""),
                                    cancelled=self.cancelled.is_set,
                                    on_wait=lambda seconds, reason: self.step(f"{reason}，{seconds:.0f} 秒后重试…"))

    def tree(self, zotero):
        collections = zotero.collections()
        by_key = {c["key"]: c for c in collections}
        children: dict[str, list[str]] = {}
        for c in sorted(collections, key=lambda c: c["data"]["name"].casefold()):
            children.setdefault(c["data"].get("parentCollection") or "", []).append(c["key"])
        roots = [k for k in self.settings.get("research_roots", []) if k in by_key]
        order: list[str] = []

        def visit(key):  # post-order: sub-collections before their parent
            for child in children.get(key, []):
                visit(child)
            if key not in order:
                order.append(key)
        for root in roots:
            visit(root)
        scope = set(order)

        def path(key):
            names, node = [], by_key.get(key)
            while node is not None:
                names.append(node["data"]["name"])
                node = by_key.get(node["data"].get("parentCollection") or "")
            return list(reversed(names))
        return by_key, {k: [c for c in v if c in scope] for k, v in children.items()}, order, path

    def folder(self, names: list[str]) -> Path:
        return Path(self.settings["output_root"]) / RESEARCH_DIR / Path(*[safe_name(n) for n in names])

    def papers(self, zotero, key: str) -> list[dict]:
        """Regular items directly in a collection, with their summary card, full text and abstract."""
        rows = zotero.rows(f"collections/{key}/items")
        papers: dict[str, dict] = {}
        for item in rows:
            data = item.get("data", {})
            if data.get("deleted") or data.get("itemType") in SKIP_TYPES or data.get("parentItem"):
                continue
            year = str(item.get("meta", {}).get("parsedDate") or data.get("date") or "")[:4]
            venue = next((data.get(f) for f in ("publicationTitle", "proceedingsTitle", "bookTitle", "university",
                                                 "institution", "publisher", "websiteTitle") if data.get(f)), "")
            papers[item["key"]] = {"key": item["key"], "title": (data.get("title") or "").strip() or "（无标题）",
                                   "creator": item.get("meta", {}).get("creatorSummary", ""),
                                   "year": year if year.isdigit() else "", "venue": venue,
                                   "doi": normalize_doi(data.get("DOI") or (data.get("extra") or "")),
                                   "abstract": (data.get("abstractNote") or "").strip(),
                                   "card": "", "card_path": "", "markdown_path": ""}
        for item in rows:
            data = item.get("data", {})
            parent = papers.get(data.get("parentItem", ""))
            if not parent or data.get("deleted") or data.get("itemType") != "attachment":
                continue
            path = file_url_to_path(item.get("links", {}).get("enclosure", {}).get("href", "")) \
                or (Path(data["path"]) if data.get("path") else None)
            if not path or not path.is_file():
                continue
            if is_summary_attachment(data) and not parent["card_path"]:
                parent["card_path"] = str(path)
                try:
                    parent["card"] = card_body(path.read_text(encoding="utf-8", errors="replace"))
                except OSError:
                    parent["card_path"] = ""
            elif is_markdown_attachment(data) and not parent["markdown_path"]:
                parent["markdown_path"] = str(path)
        result = sorted(papers.values(), key=lambda p: (p["year"] or "9999", p["creator"], p["title"]))
        for n, paper in enumerate(result, 1):
            paper["code"] = f"P{n}"
            paper["source"] = "card" if paper["card"] else "abstract" if paper["abstract"] else "meta"
            paper["label"] = paper_label(paper)
        return result

    def references(self, scholar, papers: list[dict], notes: list[str]):
        """OpenAlex id and reference list of each paper with a DOI (free lookups, cached for months)."""
        for paper in papers:
            paper.setdefault("openalex_id", "")
            paper.setdefault("refs", [])
            if not paper["doi"]:
                continue
            cached = self.store.cached("oa:" + paper["doi"], REFS_MAX_AGE)
            if cached is None and not notes:
                try:
                    work = scholar.openalex_work(paper["doi"]) or {}
                    cached = {"id": short_id(work.get("id")), "refs": [short_id(r) for r in work.get("referenced_works") or []],
                              "count": work.get("cited_by_count")}
                    self.store.cache("oa:" + paper["doi"], cached)
                except ServiceError as error:
                    notes.append(str(error))
                    self.log("获取论文引用关系失败（本次综述不含引用关系）：" + str(error))
            if cached:
                paper["openalex_id"], paper["refs"] = cached["id"], cached["refs"]
                paper["openalex_count"] = cached.get("count")

    @staticmethod
    def fingerprint(papers: list[dict], children: list[dict], lang: str) -> str:
        material = [PROMPT_VERSION, lang,
                    [(p["key"], p["title"], p["year"], p["source"], hashlib.sha1((p["card"] or p["abstract"]).encode()).hexdigest())
                     for p in papers],
                    # A sub-collection's new review (not only new papers below) is new material for the parent.
                    [(c["key"], hashlib.sha1(json.dumps(c["report"], ensure_ascii=False, sort_keys=True).encode()).hexdigest())
                     for c in children]]
        return hashlib.sha256(json.dumps(material, ensure_ascii=False).encode()).hexdigest()

    # ---------------------------------------------------------------- reviews
    def review_all(self, *, estimate: bool = False, force_keys: set[str] | None = None):
        zotero = self.zotero_factory(self.settings["zotero_port"], self.bridge_token)
        by_key, children, order, path_of = self.tree(zotero)
        if not order:
            raise ServiceError("请先在“研究综述”页勾选要综述的分类。")
        lang = self.settings.get("summary_language", "zh")
        records = self.store.research()
        nodes: dict[str, dict] = {}
        plan = []
        client = None if estimate else self.llm()
        scholar = None if estimate else self.scholar()
        reread = int(self.settings.get("research_reread", 6) or 0)
        notes: list[str] = []
        usage = {"input": 0, "output": 0, "calls": 0}
        done = 0
        for n, key in enumerate(order, 1):
            if self.cancelled.is_set():
                self.log("已停止研究综述；已完成的分类会保留。")
                break
            names = path_of(key)
            name = names[-1]
            self.step(f"读取分类 {n}/{len(order)}：{name}")
            papers = self.papers(zotero, key)
            kids = [nodes[c] for c in children.get(key, []) if c in nodes]
            below = set().union(*(c["subtree"] for c in kids)) if kids else set()
            direct = [p for p in papers if p["key"] not in below]
            if kids and len(direct) < len(papers):
                for m, paper in enumerate(direct, 1):
                    paper["code"] = f"P{m}"
            subtree = below | {p["key"] for p in papers}
            ready = [c for c in kids if c["report"]]
            record = records.get(key, {})
            fingerprint = self.fingerprint(direct, ready, lang)
            node = {"key": key, "name": name, "path": "/".join(names), "names": names, "subtree": subtree,
                    "paper_total": len(subtree), "fingerprint": fingerprint, "report": None, "papers": direct, "record": record}
            nodes[key] = node
            if not direct and not ready:
                continue
            files = record.get("files", {})
            fresh = (record.get("fingerprint") == fingerprint and record.get("status") == "done"
                     and Path(files.get("review", "")).is_file() and key not in (force_keys or set())
                     and not any(c.get("stale") for c in ready))
            if fresh:
                node["report"], node["papers"] = record["report"], record["papers"]
                continue
            node["stale"] = True  # its parent must be redone too (decided here already when only estimating)
            if estimate:
                material = sum(estimate_tokens(paper_block(p, lang)) for p in direct) \
                    + sum(estimate_tokens(child_block(f"C{i}", c)) for i, c in enumerate(ready, 1)
                          if c["report"] is not None) + 3000
                checks = min(reread, sum(bool(p["markdown_path"]) for p in direct))
                plan.append({"key": key, "name": name, "papers": len(direct), "children": len(ready),
                             "input": material + (checks * 3500 + 6000 if checks else 0), "calls": 1 + bool(checks)})
                node["report"] = record.get("report") or {"keywords": []}
                continue
            self.references(scholar, direct, notes)
            self.step(f"综述 {n}/{len(order)}：{name}")
            self.log(f"开始综述分类“{name}”：{len(direct)} 篇论文" + (f"、{len(ready)} 个子分类" if ready else "")
                     + f"（{client.display}）…")

            def call(prompt):
                if self.cancelled.is_set():
                    raise ServiceError("已停止。")
                answer, used = client.chat(SYSTEM, prompt)
                usage["input"] += used["input"]
                usage["output"] += used["output"]
                usage["calls"] += 1
                return answer
            previous = record.get("status", "")
            self.store.save_research(key, name=name, path=node["path"], status="running", error="")
            try:
                report, info = review(call, path=node["path"], lang=lang, papers=direct, children=[
                    {"key": c["key"], "name": c["name"], "report": c["report"], "paper_total": c["paper_total"]} for c in ready],
                    reread=reread, log=self.step)
            except ServiceError as error:
                if self.cancelled.is_set():
                    self.store.save_research(key, status=previous, error="")
                    continue  # the loop stops at its next check
                self.store.save_research(key, status="failed", error=str(error))
                self.log(f"分类“{name}”综述失败：{error}")
                if error.auth:
                    raise
                continue
            info["paper_total"] = len(subtree)
            node["report"] = report
            files = self.write_review(node, ready, nodes, report, info, lang, client.display, by_key, set(order))
            self.store.save_research(key, name=name, path=node["path"], fingerprint=fingerprint, report=report,
                                     papers=[{k: p.get(k, "") for k in ("key", "code", "title", "creator", "year", "venue", "doi",
                                                                         "openalex_id", "refs", "source", "card_path",
                                                                         "markdown_path")} for p in direct],
                                     info=info, files=files, model=client.display, status="done", error="",
                                     generated=time.time())
            done += 1
            self.log(f"分类“{name}”综述完成：{Path(files['review']).name}")
            self.publish()
        if estimate:
            return {"collections": plan, "input": sum(p["input"] for p in plan), "calls": sum(p["calls"] for p in plan),
                    "scope": len(order), "model": " · ".join(research_model(self.settings)[:2])}
        if not self.cancelled.is_set():
            self.store.set_value("review_last", time.time())
            self.store.set_value("review_retry", 0)
        self.write_index(order, path_of)
        self.log(f"研究综述完成：更新 {done} 个分类，其余 {len([k for k in order if nodes.get(k, {}).get('report')]) - done} 个无变化未重算"
                 + (f"；共调用模型 {usage['calls']} 次，输入 {usage['input']} / 输出 {usage['output']} tokens" if usage["calls"] else "") + "。")
        return done

    def write_review(self, node, ready, nodes, report, info, lang, model, by_key, scope: set[str]) -> dict:
        folder = self.folder(node["names"])
        name = safe_name(node["name"])
        review_path, map_path, tracking_path = folder / f"RES_{name}.md", folder / f"MAP_{name}.html", folder / f"TRK_{name}.md"
        old = node["record"].get("files", {})
        if old.get("review") and Path(old["review"]).resolve() != review_path.resolve():
            self.remove_files(old)  # the collection was renamed or moved
        links = {"tracking": relative(tracking_path, folder)}
        if self.settings.get("research_html", True):
            links["map"] = relative(map_path, folder)
        parent = by_key[node["key"]]["data"].get("parentCollection") or ""
        if parent in scope:  # reviewed later in this run (parents come after their sub-collections)
            parent_names = node["names"][:-1]
            links["parent"] = relative(self.folder(parent_names) / f"RES_{safe_name(parent_names[-1])}.md", folder)
        for child in ready:
            links["child:" + child["key"]] = relative(self.folder(child["names"]) / f"RES_{safe_name(child['name'])}.md", folder)
        collection = {"key": node["key"], "name": node["name"], "path": node["path"], "paper_total": node["paper_total"]}
        child_rows = [{"key": c["key"], "name": c["name"], "report": c["report"], "paper_total": c["paper_total"]} for c in ready]
        write_text(review_path, render_markdown(collection, report, node["papers"], child_rows, lang=lang, model=model,
                                                info=info, links=links))
        files = {"review": str(review_path), "tracking": str(tracking_path)}
        if self.settings.get("research_html", True):
            write_text(map_path, self.map_page(node, ready, nodes, report, collection, lang, review_path.name))
            files["map"] = str(map_path)
        return files

    def map_page(self, node, ready, nodes, report, collection, lang, review_file) -> str:
        codes = {p["code"]: p["key"] for p in node["papers"]}
        group_of = {}
        if ready:
            for child in ready:
                for paper in self.subtree_papers(child):
                    group_of.setdefault(paper["key"], (child["name"], paper))
            for paper in node["papers"]:
                group_of.setdefault(paper["key"], ("本分类直接论文" if lang == "zh" else "Filed here", paper))
        else:
            for paper in node["papers"]:
                branch = next((b["name"] for b in report["branches"] if paper["code"] in b["papers"]
                               or any(paper["code"] in c["papers"] for c in b["children"])), "未归类" if lang == "zh" else "Other")
                group_of[paper["key"]] = (branch, paper)
        entries = list(group_of.items())[:400]
        nodes_out = [{"id": key, "code": paper.get("code", "") if not ready else "", "label": paper_label(paper),
                      "title": paper.get("title", ""), "year": paper.get("year", ""), "group": group,
                      "href": f"zotero://select/library/items/{key}"} for key, (group, paper) in entries]
        by_openalex = {paper.get("openalex_id"): key for key, (_, paper) in entries if paper.get("openalex_id")}
        edges = sorted({(key, by_openalex[ref]) for key, (_, paper) in entries for ref in paper.get("refs") or []
                        if ref in by_openalex and by_openalex[ref] != key})
        inherit = [(codes[l["from"]], codes[l["to"]]) for l in report["links"] if l["from"] in codes and l["to"] in codes]
        return render_map(collection, report, nodes_out, edges, inherit, lang=lang, report_file=review_file)

    def subtree_papers(self, node) -> list[dict]:
        """All papers below a collection, taken from the stored reviews of it and its sub-collections."""
        result = {}
        for record in self.store.research().values():
            for paper in record.get("papers", []):
                if paper["key"] in node["subtree"]:
                    result.setdefault(paper["key"], paper)
        return list(result.values())

    @staticmethod
    def remove_files(files: dict):
        for path in files.values():
            target = Path(path)
            if target.name.startswith(("RES_", "MAP_", "TRK_")) and target.is_file():
                try:
                    target.unlink()
                    if not any(target.parent.iterdir()):
                        target.parent.rmdir()
                except OSError:
                    pass

    def write_index(self, order: list[str], path_of):
        """Top-level index for AI agents: the collection tree with each review's keywords and overview."""
        root = Path(self.settings["output_root"]) / RESEARCH_DIR
        records = self.store.research()
        entries = []
        for key in sorted(order, key=lambda k: path_of(k)):
            record = records.get(key)
            if not record or record.get("status") != "done":
                continue
            names = path_of(key)
            report = record["report"]
            entries.append({"collection": names[-1], "collection_key": key, "path": "/".join(names), "depth": len(names) - 1,
                            "papers_total": record["info"].get("paper_total", 0), "keywords": report.get("keywords", []),
                            "overview": report.get("overview", ""), "review_file": record["files"].get("review", ""),
                            "map_file": record["files"].get("map", ""), "tracking_file": record["files"].get("tracking", ""),
                            "generated_at": datetime.fromtimestamp(record["generated"]).strftime("%Y-%m-%d %H:%M"),
                            "zotero_link": f"zotero://select/library/collections/{key}"})
        lines = ["# Zotero 研究地图索引", "",
                 "供研究者和 AI 助手检索：先在本索引中按分类、关键词和概述找到相关方向，打开该分类的研究综述（RES_），"
                 "再按综述中的论文清单读 AI 总结卡片（SUM_），只有需要细节时才读 Markdown 全文（MD_）。",
                 f"共 {len(entries)} 个分类，更新于 {datetime.now():%Y-%m-%d %H:%M}。", ""]
        listed = {tuple(path_of(e["collection_key"])) for e in entries}
        for e in entries:
            first = e["overview"].split("。")[0][:160]
            link = relative(Path(e["review_file"]), root) if e["review_file"] else ""
            # Nest under the listed ancestors only: absolute depth would indent a deep collection into a code block.
            names = path_of(e["collection_key"])
            level = sum(tuple(names[:n]) in listed for n in range(1, len(names)))
            lines.append("  " * level + f"- [{e['collection']}](<{link}>)（{e['papers_total']} 篇）："
                         + "；".join(e["keywords"][:8]) + (f" —— {first}" if first else ""))
        root.mkdir(parents=True, exist_ok=True)
        write_text(root / INDEX_MD, "\n".join(lines) + "\n")
        write_text(root / INDEX_JSONL, "".join(json.dumps(e, ensure_ascii=False) + "\n" for e in entries))

    # ---------------------------------------------------------------- tracking
    def track_all(self):
        zotero = self.zotero_factory(self.settings["zotero_port"], self.bridge_token)
        by_key, children, order, path_of = self.tree(zotero)
        if not order:
            raise ServiceError("请先在“研究综述”页勾选要综述的分类。")
        client, scholar = self.llm(), self.scholar()
        lang = self.settings.get("summary_language", "zh")
        library = [i for i in zotero.rows("items") if i.get("data", {}).get("itemType") not in SKIP_TYPES]
        library_dois = {normalize_doi(i["data"].get("DOI")) for i in library} - {""}
        library_titles = {normalize_title(i["data"].get("title")) for i in library} - {""}
        records = self.store.research()
        window = int(self.settings.get("tracking_window_days", 180))
        since = (date.today() - timedelta(days=window)).isoformat()
        latest: dict[str, list[dict]] = {}
        subtree: dict[str, set] = {}
        total = 0
        for n, key in enumerate(order, 1):
            if self.cancelled.is_set():
                self.log("已停止新文献追踪。")
                break
            names = path_of(key)
            name = names[-1]
            papers = self.papers(zotero, key)
            below = set().union(*(subtree[c] for c in children.get(key, []) if c in subtree)) if children.get(key) else set()
            subtree[key] = below | {p["key"] for p in papers}
            seeds = [p for p in papers if p["key"] not in below]
            state = self.store.tracking(key)
            runs = state["runs"]
            if seeds:
                self.step(f"追踪 {n}/{len(order)}：{name}")
                notes: list[str] = []
                self.references(scholar, seeds, notes)
                report = records.get(key, {}).get("report") or {}
                profile = {"profile": (report.get("search") or {}).get("profile", "") or report.get("overview", "")[:600],
                           "queries": (report.get("search") or {}).get("queries", []), "keywords": report.get("keywords", [])}

                def llm_call(system, prompt):
                    if self.cancelled.is_set():
                        raise ServiceError("已停止。")
                    return client.chat(system, prompt)[0]
                try:
                    result = track(scholar, seeds, path="/".join(names), profile=profile, since=since,
                                   library_dois=library_dois, library_titles=library_titles, seen=set(state["seen"]),
                                   cache=state["cache"], llm_call=llm_call, lang=lang,
                                   budget=int(self.settings.get("tracking_budget", 15)), min_score=int(self.settings.get("tracking_min_score", 6)),
                                   keyword_search=bool(self.scholar_keys.get("openalex")), log=self.step)
                except ServiceError as error:
                    self.store.save_tracking(key, error=str(error))
                    self.log(f"分类“{name}”新文献追踪失败：{error}")
                    if error.auth:
                        raise
                    continue
                run = {"date": date.today().isoformat(), "since": since, "stats": result["stats"],
                       "notes": result["notes"] + notes[:1], "items": result["items"]}
                if runs and runs[0]["date"] == run["date"]:  # a second run the same day adds to that day's list
                    known = {i["key"] for i in result["items"]}
                    run["items"] = result["items"] + [i for i in runs[0]["items"] if i["key"] not in known]
                    runs = runs[1:]
                runs = [run] + runs[:11]
                seen = (state["seen"] + result["reviewed"])[-5000:]
                self.store.save_tracking(key, runs=runs, seen=seen, cache=result["cache"], last_run=time.time(), error="")
                total += len(result["items"])
                self.log(f"分类“{name}”新文献追踪：候选 {result['stats']['candidates']} 篇，推荐 {len(result['items'])} 篇。")
            latest[key] = runs[0]["items"] if runs else []
            kids = [(path_of(c)[-1], relative(self.folder(path_of(c)) / f"TRK_{safe_name(path_of(c)[-1])}.md", self.folder(names)),
                     latest.get(c, [])) for c in children.get(key, [])]
            if not runs and not any(items for _, _, items in kids):
                continue
            review_file = records.get(key, {}).get("files", {}).get("review", "")
            folder = self.folder(names)
            write_text(folder / f"TRK_{safe_name(name)}.md", render_tracking(
                {"key": key, "name": name, "path": "/".join(names)}, runs[:6], lang=lang,
                review_file=relative(Path(review_file), folder) if review_file else "", children=kids))
            merged = {i["key"]: i for _, _, items in kids for i in items}
            merged.update({i["key"]: i for i in latest[key]})
            latest[key] = sorted(merged.values(), key=lambda i: i["score"], reverse=True)[:30]
            self.publish()
        if not self.cancelled.is_set():
            self.store.set_value("track_last", time.time())
            self.store.set_value("track_retry", 0)
        self.log(f"新文献追踪完成：本次共推荐 {total} 篇新论文（{scholar.requests} 次文献数据库请求）。")
        return total

    # ---------------------------------------------------------------- status for the window
    def publish(self):
        records = self.store.research()
        rows = []
        for key, record in records.items():
            state = self.store.tracking(key)
            latest = state["runs"][0] if state["runs"] else {}
            rows.append({"key": key, "name": record["name"], "path": record["path"], "status": record["status"],
                         "error": record["error"], "generated": record["generated"], "files": record["files"],
                         "papers_total": record["info"].get("paper_total", 0),
                         "coverage": {k: sum(p.get("source") == k for p in record["papers"]) for k in ("card", "abstract", "meta")},
                         "tracked": latest.get("date", ""), "new_papers": len(latest.get("items", [])),
                         "tracking_error": state["error"]})
        times = {name: float(self.store.get_value(name, "0") or 0) for name in ("review_last", "track_last")}
        self.emit("research", {"rows": rows, "running": self.running, "progress": self.progress, **times})
