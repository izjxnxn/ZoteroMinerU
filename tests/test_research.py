"""Collection reviews (bottom-up), rendering, new-paper tracking and the scheduled worker."""
from __future__ import annotations

import copy
import json
import re
import tempfile
import threading
import time
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

import requests

from test_core import Harness, item
from zotero_mineru.clients import ZoteroClient
from zotero_mineru.config import DEFAULTS, Config
from zotero_mineru.llm import ContextTooLong
from zotero_mineru.research import (card_body, excerpt, expand_codes, mermaid_text, normalize_report, parse_json,
                                    render_markdown, review)
from zotero_mineru.researcher import INDEX_JSONL, INDEX_MD, RESEARCH_DIR, Researcher
from zotero_mineru.scholar import (ScholarClient, center, mean_vector, mmr_select, openalex_abstract, self_threshold,
                                   unit)
from zotero_mineru.tracking import SYSTEM as TRACK_SYSTEM, candidate_key, render_tracking, track


def report_json(codes, *, overview="概述：研究实体壳相场断裂。", children=()):
    first = list(children) or list(codes)
    return json.dumps({
        "overview": overview, "keywords": ["相场（phase field）", "实体壳（solid shell）"],
        "objects": [{"name": "薄壁壳", "type": "结构", "details": "厚度 1 mm", "papers": first[:2]}],
        "branches": [{"name": "热弹性断裂", "summary": "热-力耦合", "papers": first[:1],
                      "children": [{"name": "功能梯度材料", "summary": "FGM", "papers": first[:1]}]},
                     {"name": "大变形", "summary": "几何非线性", "papers": first[1:] + ["P99"]}],
        "lineage": [{"period": "2017–2020", "stage": "起步", "summary": "建立模型", "advance": "首次提出", "papers": first[:1]},
                    {"period": "2021–2023", "stage": "拓展", "summary": "热耦合", "advance": "加入温度场", "papers": first[1:]}],
        "links": [{"from": first[0], "to": first[-1], "relation": "后者在前者基础上加入热耦合"}] if len(first) > 1 else [],
        "development": "- 2017 年起步", "progress": "- 最新水平", "gaps": ["疲劳"], "directions": ["多场耦合"],
        "search": {"queries": ["solid shell phase field fracture"], "profile": "Phase-field fracture of solid shells."}},
        ensure_ascii=False)


class ScriptedModel:
    """Answers review, check and scoring prompts with valid JSON, recording every prompt."""

    display = "测试模型 · test-1"
    sees_images = False

    def __init__(self):
        self.prompts = []

    def chat(self, system, prompt, **_):
        self.prompts.append((system, prompt))
        if system == TRACK_SYSTEM:
            rows = []
            for number, title in re.findall(r"\[T(\d+)\][^\n]*\n标题：([^\n]*)", prompt):
                rows.append({"id": f"T{number}", "score": 9 if "Relevant" in title else 2,
                             "reason": "研究对象相同", "summary": "研究了壳的相场断裂"})
            return json.dumps(rows, ensure_ascii=False), {"input": 10, "output": 5}
        if "逐项核对初稿" in prompt:
            draft = json.loads(re.search(r"<draft>\n(.*)\n</draft>", prompt, re.DOTALL).group(1))
            draft["overview"] = "核对后的概述。" + draft["overview"]
            return json.dumps(draft, ensure_ascii=False), {"input": 10, "output": 5}
        children = sorted(set(re.findall(r"^\[(C\d+)\]", prompt, re.MULTILINE)))
        codes = sorted(set(re.findall(r"^\[(P\d+)\]", prompt, re.MULTILINE)), key=lambda c: int(c[1:]))
        if "（G 编号）" in prompt:
            codes = sorted(set(re.findall(r"^(P\d+)：", prompt, re.MULTILINE)), key=lambda c: int(c[1:]))
        return "```json\n" + report_json(codes, children=children) + "\n```", {"input": 10, "output": 5}


class FakeScholar:
    batch_ok, s2_fast, s2_down = True, True, False

    def __init__(self):
        self.calls, self.requests = [], 0
        self.papers, self.citations, self.works = {}, {}, {}
        self.recommendations, self.citing_rows, self.search_rows = [], [], []

    def s2_papers(self, ids, fields):
        self.calls.append(("papers", tuple(ids)))
        return [copy.deepcopy(self.papers.get(i)) for i in ids]

    def s2_citations(self, paper_id, fields=None):
        self.calls.append(("citations", paper_id))
        return copy.deepcopy(self.citations.get(paper_id, []))

    def s2_recommend(self, positive, negative=(), limit=100, fields=None):
        self.calls.append(("recommend", tuple(positive)))
        return copy.deepcopy(self.recommendations)

    def openalex_work(self, doi):
        self.calls.append(("work", doi))
        return copy.deepcopy(self.works.get(doi))

    def openalex_works(self, *, filter, search="", per_page=100, pages=1):
        self.calls.append(("works", filter, search))
        return copy.deepcopy(self.citing_rows if "cites:" in filter else self.search_rows)


def s2_row(paper_id, title, date, *, doi="", vector=None, abstract="", count=3):
    row = {"paperId": paper_id, "title": title, "publicationDate": date, "year": int(date[:4]), "venue": "Thin-Walled Structures",
           "externalIds": {"DOI": doi} if doi else {}, "citationCount": count, "abstract": abstract}
    if vector is not None:
        row["embedding"] = {"vector": vector}
    return row


class PureFunctionTests(unittest.TestCase):
    def test_json_answers_are_tolerated_and_normalised(self):
        data = parse_json("说明文字```json\n{\"overview\": \"x\", \"branches\": [{\"name\": \"a\", \"papers\": [\"P1\", \"P7\"],},],}\n```")
        report = normalize_report(data, {"P1", "P2"})
        self.assertEqual(report["branches"][0]["papers"], ["P1"], "codes of papers that do not exist are dropped")
        report = normalize_report(json.loads(report_json(["P1", "P2"])), {"P1", "P2"})
        self.assertEqual(report["links"], [{"from": "P1", "to": "P2", "relation": "后者在前者基础上加入热耦合"}])
        self.assertEqual(report["branches"][1]["papers"], ["P2"])
        same_year = normalize_report({"overview": "x", "lineage": [{"period": "2017–2017", "stage": "a"}]}, set())
        self.assertEqual(same_year["lineage"][0]["period"], "2017")

    def test_mermaid_labels_and_code_expansion(self):
        self.assertEqual(mermaid_text("Model (A): [x]; y \"z\""), "Model （A）： ［x］； y 'z'")
        self.assertTrue(len(mermaid_text("x" * 100, 20)) <= 20)
        text = expand_codes("P1 与 P2（已注明）及 C1", {"P1": "Liu 等 2022", "P2": "Wu 2020", "C1": "实体壳"})
        self.assertEqual(text, "P1（Liu 等 2022） 与 P2（已注明）及 C1（实体壳）")
        self.assertEqual(expand_codes("向P1提出，P1与P2把", {"P1": "Liu 2022", "P2": "Wu 2020"}),
                         "向P1（Liu 2022）提出，P1与P2（Wu 2020）把", "codes against Chinese text; only the first mention")
        self.assertEqual(expand_codes("C3S 与 GPC1", {"C3": "x", "C1": "y"}), "C3S 与 GPC1", "not inside other words")

    def test_card_body_drops_front_matter_and_links(self):
        card = "---\ntitle: \"x\"\n---\n\n# 论文总结：T\n\n**作者**：A\n\n**关联**：[Zotero](z)\n\n> **一句话总结**：S\n\n## 研究背景\n\nB\n"
        body = card_body(card)
        self.assertNotIn("title:", body)
        self.assertNotIn("关联", body)
        self.assertIn("一句话总结", body)
        self.assertIn("## 研究背景", body)

    def test_excerpt_takes_method_and_conclusions(self):
        text = "# Title\n\nAbstract text.\n\n## 1 Introduction\n\n" + "intro " * 300 + "\n\n## 2 Solid shell formulation\n\n" \
            + "method details " * 100 + "\n\n## 5 Results\n\n" + "results " * 300 + "\n\n## 6 Conclusions\n\nThe key finding.\n\n## References\n\n[1] x"
        result = excerpt(text, 1000)
        self.assertIn("Solid shell formulation", result)
        self.assertIn("The key finding.", result)
        self.assertNotIn("[1] x", result)

    def test_vectors_threshold_and_mmr(self):
        refs = [[1, 0, 0, 0], [0.9, 0.1, 0, 0], [0.8, 0, 0.2, 0], [0.85, 0.05, 0.1, 0]]
        good, bad = [0.95, 0.05, 0.02, 0], [0, 0, 0.1, 1]
        background = mean_vector([unit(v) for v in refs + [good, bad]])
        centred = [center(v, background) for v in refs]
        threshold = self_threshold(centred)
        similarity = lambda v: max(sum(a * b for a, b in zip(center(v, background), r)) for r in centred)  # noqa: E731
        self.assertGreaterEqual(similarity(good), threshold)
        self.assertLess(similarity(bad), threshold)
        self.assertIsNone(self_threshold(centred[:2]), "two papers are too few to calibrate")
        order = mmr_select([1.0, 0.8, 0.6], [[1, 0], [1, 0], [0, 1]], 2)
        self.assertEqual(order, [0, 2], "a near-duplicate loses its slot to another direction")
        self.assertEqual(openalex_abstract({"phase": [0], "field": [1], "fracture": [2]}), "phase field fracture")


class ReviewWorkflowTests(unittest.TestCase):
    def papers(self, count, root: Path):
        papers = []
        for n in range(1, count + 1):
            markdown = root / f"MD_{n}.md"
            markdown.write_text(f"# Paper {n}\n\n## 2 Formulation\n\n" + "model " * 200 + "\n\n## Conclusions\n\nDone.", encoding="utf-8")
            papers.append({"key": f"KEY{n:05d}", "code": f"P{n}", "title": f"Paper {n}", "creator": "Liu 等", "year": str(2016 + n),
                           "venue": "TWS", "doi": f"10.1/{n}", "abstract": "摘要", "card": "## 研究对象\n薄壳 " * 30 if n % 2 else "",
                           "source": "card" if n % 2 else "abstract", "markdown_path": str(markdown),
                           "openalex_id": f"W{n}", "refs": [f"W{n - 1}"] if n > 1 else []})
        return papers

    def test_leaf_review_reads_cards_then_rereads_key_full_texts(self):
        root = Path(tempfile.mkdtemp())
        model = ScriptedModel()
        report, info = review(lambda p: model.chat("s", p)[0], path="相场/实体壳", lang="zh", papers=self.papers(3, root), children=[], reread=2)
        self.assertEqual(len(model.prompts), 2)
        first = model.prompts[0][1]
        self.assertIn("P2 引用了 P1", first, "citation links between the papers are given to the model")
        self.assertIn("资料：AI 总结卡片", first)
        self.assertIn("资料：仅摘要", first)
        self.assertIn("<paper code=", model.prompts[1][1], "the check reads the key papers' full texts")
        self.assertTrue(report["overview"].startswith("核对后"))
        self.assertEqual((info["reread"], info["edges"]), (2, 2))

    def test_many_papers_are_reviewed_in_groups_then_merged(self):
        model = ScriptedModel()
        papers = self.papers(6, Path(tempfile.mkdtemp()))
        report, info = review(lambda p: model.chat("s", p)[0], path="大分类", lang="zh", papers=papers, children=[], budget=500)
        self.assertGreater(info["groups"], 1)
        self.assertEqual(len(model.prompts), info["groups"] + 1)
        self.assertIn("（G 编号）", model.prompts[-1][1])
        self.assertIn("P6：", model.prompts[-1][1], "the merge step knows every paper's code")
        self.assertTrue(set(report["branches"][0]["papers"]) <= {p["code"] for p in papers})

    def test_context_overflow_switches_to_groups(self):
        model = ScriptedModel()
        papers = self.papers(6, Path(tempfile.mkdtemp()))
        for paper in papers:
            paper.update(card="薄壳相场断裂。" * 600, source="card")

        def call(prompt):
            if prompt.count("\n[P") > 3:
                raise ContextTooLong("context window exceeded")
            return model.chat("s", prompt)[0]
        report, info = review(call, path="x", lang="zh", papers=papers, children=[])
        self.assertGreater(info["groups"], 1)
        self.assertTrue(report["overview"])

    def test_rendered_report_has_diagrams_codes_and_links(self):
        root = Path(tempfile.mkdtemp())
        papers = self.papers(2, root)
        papers[0]["card_path"] = str(root / "SUM_1.md")
        report = normalize_report(json.loads(report_json(["P1", "P2"])), {"P1", "P2"})
        text = render_markdown({"key": "COLLECT1", "name": "实体壳", "path": "相场/实体壳", "paper_total": 2}, report, papers, [],
                               lang="zh", model="m", info={"reread": 1, "edges": 1}, links={"map": "MAP_实体壳.html", "tracking": "TRK_实体壳.md"})
        self.assertTrue(text.startswith("---\ncollection: \"实体壳\""))
        for part in ("```mermaid\nmindmap", "```mermaid\ntimeline", "```mermaid\nflowchart LR", "P1（Liu 等 2017）",
                     "zotero://select/library/items/KEY00001", "](<MAP_实体壳.html>)", "| 2017 | 1 | P1 |", "## 研究进展"):
            self.assertIn(part, text)


class ResearcherTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        self.service = Harness(self.root)
        self.addCleanup(self.service.close)
        state = self.service.state
        state["collections"] = [
            {"key": "ROOT0001", "data": {"name": "相场模型", "parentCollection": False}, "meta": {"numItems": 1}},
            {"key": "LEAF0001", "data": {"name": "实体壳", "parentCollection": "ROOT0001"}, "meta": {"numItems": 2}},
            {"key": "LEAF0002", "data": {"name": "分层壳: 新", "parentCollection": "ROOT0001"}, "meta": {"numItems": 1}},
            {"key": "OTHER001", "data": {"name": "无关分类", "parentCollection": False}, "meta": {"numItems": 0}}]
        self.add_paper("PAPER001", "Thermo-elastic solid shell", "LEAF0001", "2022", "10.1016/j.tws.2022.109535", card="薄壁 FGM 壳")
        self.add_paper("PAPER002", "Phase field solid shell", "LEAF0001", "2017", "10.1007/s00466-017-1386-3")
        self.add_paper("PAPER003", "Layered shell delamination", "LEAF0002", "2020", "10.1/layer")
        self.add_paper("PAPER004", "Shell overview", "ROOT0001", "2019", "")
        state["items"]["PAPER001"]["data"]["collections"].append("ROOT0001")  # also filed in the parent: not repeated there
        self.model = ScriptedModel()
        self.scholar = FakeScholar()
        self.scholar.works = {"10.1016/j.tws.2022.109535": {"id": "https://openalex.org/W2", "referenced_works": ["https://openalex.org/W1"]},
                              "10.1007/s00466-017-1386-3": {"id": "https://openalex.org/W1", "referenced_works": []}}
        settings = copy.deepcopy(DEFAULTS)
        settings.update(zotero_port=self.service.server.server_port, output_root=str(self.root / "output"),
                        research_roots=["ROOT0001"], research_reread=2)
        self.events = []
        self.worker = Researcher(self.service.store, settings, {"deepseek": "sk-test"}, {}, self.service.state["bridge_token"],
                                 lambda kind, data: self.events.append((kind, data)),
                                 zotero_factory=lambda port, token: ZoteroClient(port, token),
                                 llm_factory=lambda *args: self.model, scholar_factory=lambda *a, **k: self.scholar)
        self.out = self.root / "output" / RESEARCH_DIR

    def add_paper(self, key, title, collection, year, doi, card=""):
        self.service.state["items"][key] = item(key, "journalArticle", title=title, creator="Liu 等", collections=[collection],
                                                date=year, DOI=doi, abstractNote=f"Abstract of {title}")
        markdown = self.root / "storage" / f"MD{key}" / f"MD_{key}.md"
        markdown.parent.mkdir(parents=True, exist_ok=True)
        markdown.write_text(f"# {title}\n\n## Formulation\n\n" + "theory " * 120 + "\n\n## Conclusions\n\nResult.", encoding="utf-8")
        self.service.markdown(f"M{key[1:]}", key, markdown)
        if card:
            path = self.root / "storage" / f"SU{key}" / f"SUM_{key}.md"
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(f"---\ntitle: \"{title}\"\n---\n\n# 论文总结：{title}\n\n> **一句话总结**：{card}\n\n## 研究对象\n\n{card}\n", encoding="utf-8")
            self.service.markdown(f"S{key[1:]}", key, path)
        return markdown

    def test_bottom_up_review_writes_reports_maps_and_index(self):
        self.assertEqual(self.worker.review_all(), 3)
        leaf = self.out / "相场模型" / "实体壳" / "RES_实体壳.md"
        odd = self.out / "相场模型" / "分层壳 新" / "RES_分层壳 新.md"
        parent = self.out / "相场模型" / "RES_相场模型.md"
        for path in (leaf, odd, parent, leaf.with_name("MAP_实体壳.html"), parent.with_name("MAP_相场模型.html"),
                     self.out / INDEX_MD, self.out / INDEX_JSONL):
            self.assertTrue(path.is_file(), path)
        prompts = [p for _, p in self.model.prompts]
        parent_prompt = next(p for p in prompts if "相场模型”撰写" in p)
        leaf_indexes = [n for n, p in enumerate(prompts) if "实体壳”撰写" in p or "分层壳: 新”撰写" in p]
        self.assertGreater(prompts.index(parent_prompt), max(leaf_indexes), "the parent is reviewed after its sub-collections")
        self.assertIn("[C1] 子分类：分层壳: 新", parent_prompt)
        self.assertIn("[C2] 子分类：实体壳", parent_prompt)
        self.assertIn("Shell overview", parent_prompt, "papers filed directly in the parent are read there")
        self.assertNotIn("Thermo-elastic solid shell", parent_prompt, "papers already reviewed in a sub-collection are not re-read")
        leaf_prompt = next(p for p in prompts if "实体壳”撰写" in p)
        self.assertIn("P2 引用了 P1", leaf_prompt, "OpenAlex reference lists become citation links")
        self.assertIn("资料：AI 总结卡片", leaf_prompt)
        text = leaf.read_text(encoding="utf-8")
        self.assertIn("](<../RES_相场模型.md>)", text, "the leaf links up to its parent's review")
        self.assertIn("1 篇依据 AI 总结卡片，1 篇仅依据摘要", text)
        self.assertIn("C2（实体壳）", parent.read_text(encoding="utf-8"))
        self.assertIn("](<实体壳/RES_实体壳.md>)", parent.read_text(encoding="utf-8"))
        page = parent.with_name("MAP_相场模型.html").read_text(encoding="utf-8")
        self.assertIn("zotero://select/library/items/PAPER003", page, "the parent's map shows the papers of its sub-collections")
        self.assertIn('class="cite"', leaf.with_name("MAP_实体壳.html").read_text(encoding="utf-8"))
        index = (self.out / INDEX_MD).read_text(encoding="utf-8")
        self.assertIn("- [相场模型](<相场模型/RES_相场模型.md>)", index)
        self.assertIn("  - [实体壳](<相场模型/实体壳/RES_实体壳.md>)", index)
        self.assertFalse((self.out / "无关分类").exists(), "collections outside the chosen roots are untouched")

    def test_index_of_a_deep_collection_is_not_indented_into_a_code_block(self):
        self.worker.settings["research_roots"] = ["LEAF0001"]
        self.worker.review_all()
        lines = (self.out / INDEX_MD).read_text(encoding="utf-8").splitlines()
        self.assertIn("- [实体壳](<相场模型/实体壳/RES_实体壳.md>)", "\n".join(lines))
        self.assertFalse(any(line.startswith("  - [实体壳]") for line in lines))

    def test_unchanged_collections_are_not_reread_and_changes_propagate_up(self):
        self.worker.review_all()
        calls = len(self.model.prompts)
        self.assertEqual(self.worker.review_all(), 0)
        self.assertEqual(len(self.model.prompts), calls, "nothing changed: no model call")
        plan = self.worker.review_all(estimate=True)
        self.assertEqual(plan["collections"], [])
        self.add_paper("PAPER005", "New layered shell", "LEAF0002", "2024", "10.1/new")
        plan = self.worker.review_all(estimate=True)
        self.assertEqual([p["name"] for p in plan["collections"]], ["分层壳: 新", "相场模型"])
        self.assertEqual(len(self.model.prompts), calls, "estimating calls no model")
        self.assertEqual(self.worker.review_all(), 2)
        self.assertEqual(self.worker.review_all(force_keys={"LEAF0001"}), 2, "a forced collection is redone with its parent")

    def test_tracking_writes_reports_and_parent_merges_children(self):
        s = self.scholar
        s.papers = {"DOI:10.1016/j.tws.2022.109535": s2_row("S1", "Thermo-elastic solid shell", "2022-06-01", vector=[1, 0, 0, 0], count=17),
                    "DOI:10.1007/s00466-017-1386-3": s2_row("S2", "Phase field solid shell", "2017-03-01", vector=[0.9, 0.1, 0, 0], count=110),
                    "DOI:10.1/layer": s2_row("S3", "Layered", "2020-01-01", vector=[0.8, 0, 0.2, 0], count=5),
                    "C1": {"paperId": "C1", "embedding": {"vector": [0.95, 0.05, 0.02, 0]}},
                    "C2": {"paperId": "C2", "embedding": {"vector": [0, 0, 0.1, 1]}}}
        recent = (time.strftime("%Y-%m-%d"))
        s.citations = {"S1": [s2_row("C1", "Relevant shell fracture", recent, doi="10.9/c1", abstract="shell")],
                       "S2": [s2_row("C1", "Relevant shell fracture", recent, doi="10.9/c1"),
                              s2_row("OLD", "Relevant but old", "2001-01-01", doi="10.9/old"),
                              s2_row("LIB", "Phase field solid shell", recent, doi="10.1007/s00466-017-1386-3")],
                       "S3": [s2_row("C2", "Unrelated rock grouting", recent, doi="10.9/c2")]}
        self.assertEqual(self.worker.track_all(), 1)
        leaf = (self.out / "相场模型" / "实体壳" / "TRK_实体壳.md").read_text(encoding="utf-8")
        self.assertIn("Relevant shell fracture", leaf)
        self.assertIn("引用了本分类 P1（Liu 等 2017）、P2（Liu 等 2022）", leaf)
        self.assertNotIn("Relevant but old", leaf, "outside the publication window")
        self.assertNotIn("### 2. Phase field solid shell", leaf, "already in the library")
        layered = (self.out / "相场模型" / "分层壳 新" / "TRK_分层壳 新.md").read_text(encoding="utf-8")
        self.assertNotIn("Unrelated rock grouting", layered, "dissimilar or low-scored papers are not recommended")
        parent = (self.out / "相场模型" / "TRK_相场模型.md").read_text(encoding="utf-8")
        self.assertIn("## 子分类：[实体壳](<实体壳/TRK_实体壳.md>)", parent)
        citation_calls = [c for c in s.calls if c[0] == "citations"]
        self.worker.track_all()
        self.assertEqual(len([c for c in s.calls if c[0] == "citations"]), len(citation_calls),
                         "seeds whose citation count did not change are not fetched again")
        again = (self.out / "相场模型" / "实体壳" / "TRK_实体壳.md").read_text(encoding="utf-8")
        self.assertEqual(again.count("### 1. Relevant shell fracture"), 1, "a paper already reported is not reported twice")

    def test_schedule_runs_when_due_only_when_enabled(self):
        ran = []
        self.worker.review_all = lambda **_: ran.append("review") or 0
        self.worker.track_all = lambda: ran.append("track") or 0
        self.worker.scheduled()
        self.assertEqual(ran, [], "the switch is off")
        self.worker.settings["research_enabled"] = True
        self.worker.scheduled()
        self.assertEqual(ran, ["review", "track"], "never run before: both are due")
        self.service.store.set_value("review_last", time.time())
        self.service.store.set_value("track_last", time.time())
        self.worker.scheduled()
        self.assertEqual(ran, ["review", "track"])
        self.service.store.set_value("review_last", time.time() - 8 * 86400)
        self.worker.scheduled()
        self.assertEqual(ran, ["review", "track", "review"], "a week later the review is due again, tracking is not")

    def test_missing_api_key_stops_before_reading(self):
        self.worker.keys = {}
        self.assertIsNone(self.worker.guarded("review", self.worker.review_all))
        self.assertTrue(any("API Key" in str(data) for kind, data in self.events if kind == "log"))
        self.assertEqual(self.model.prompts, [])


class TrackFunctionTests(unittest.TestCase):
    def gate_case(self, seed_count, relevant, unrelated):
        s = FakeScholar()
        recent = time.strftime("%Y-%m-%d")
        seeds = [{"key": f"K{n}", "code": f"P{n}", "label": f"A {n}", "title": f"T{n}", "doi": f"10.1/{n}", "year": "2020"}
                 for n in range(seed_count)]
        s.papers = {f"DOI:10.1/{n}": s2_row(f"S{n}", f"T{n}", "2020-01-01", count=4,
                                            vector=[1, 0.05 * (n % 3), 0.05 * (n % 4), 0.02 * n, 0, 0]) for n in range(seed_count)}
        rows = [s2_row(f"R{n}", f"Relevant {n}", recent, doi=f"10.8/{n}") for n in range(relevant)]
        rows += [s2_row(f"U{n}", f"Unrelated {n}", recent, doi=f"10.9/{n}") for n in range(unrelated)]
        s.recommendations = rows
        s.papers.update({f"R{n}": {"paperId": f"R{n}", "embedding": {"vector": [1, 0.04 * (n % 5), 0.03, 0.01 * n, 0, 0]}}
                         for n in range(relevant)})
        s.papers.update({f"U{n}": {"paperId": f"U{n}", "embedding": {"vector": [0.1, 0, 0, 0, 0.3 * (n % 2), 1]}}
                         for n in range(unrelated)})
        return track(s, seeds, path="x", profile={}, since="2000-01-01", library_dois=set(), library_titles=set(), seen=set(),
                     cache={}, llm_call=lambda system, prompt: ScriptedModel().chat(system, prompt)[0], budget=50, pool_size=60)

    def test_similarity_gate_needs_enough_vectors(self):
        small = self.gate_case(3, 2, 2)
        self.assertIsNone(small["stats"]["threshold"], "three papers cannot calibrate a threshold")
        self.assertEqual(small["stats"]["gate_passed"], small["stats"]["after_filters"], "nothing is dropped by the gate")
        large = self.gate_case(10, 25, 6)
        self.assertIsNotNone(large["stats"]["threshold"])
        self.assertFalse([k for k in large["reviewed"] if k.startswith("doi:10.9/")],
                         "clearly unrelated papers are dropped before the model reads them")
        self.assertGreaterEqual(large["stats"]["gate_passed"], 20, "most related papers pass (the threshold is a 10th percentile)")

    def test_candidates_routes_budget_and_render(self):
        s = FakeScholar()
        seeds = [{"key": f"K{n}", "code": f"P{n}", "label": f"A {n}", "title": f"T{n}", "doi": f"10.1/{n}", "year": "2020",
                  "openalex_id": f"W{n}"} for n in range(1, 4)]
        s.papers = {f"DOI:10.1/{n}": s2_row(f"S{n}", f"T{n}", "2020-01-01", vector=v, count=4)
                    for n, v in zip((1, 2, 3), ([1, 0, 0], [0.9, 0.1, 0], [0.8, 0, 0.2]))}
        recent = time.strftime("%Y-%m-%d")
        s.recommendations = [s2_row(f"R{n}", f"Relevant similar {n}", recent, doi=f"10.8/{n}") for n in range(5)]
        s.papers.update({f"R{n}": {"paperId": f"R{n}", "embedding": {"vector": [0.97, 0.02 * n, 0.01]}} for n in range(5)})
        s.citing_rows = [{"id": "https://openalex.org/W9", "doi": "https://doi.org/10.7/x", "title": "Relevant via OpenAlex",
                          "publication_date": recent, "primary_location": {"source": {"display_name": "IJSS"}},
                          "abstract_inverted_index": {"shell": [0]}, "cited_by_count": 0,
                          "referenced_works": ["https://openalex.org/W2"]}]
        result = track(s, seeds, path="x", profile={"profile": "shells", "queries": ["q"], "keywords": ["壳"]},
                       since="2000-01-01", library_dois=set(), library_titles=set(), seen={"doi:10.8/0"}, cache={},
                       llm_call=lambda system, prompt: ScriptedModel().chat(system, prompt)[0], budget=3, keyword_search=False)
        titles = [i["title"] for i in result["items"]]
        self.assertEqual(len(titles), 3, "the budget caps the list")
        self.assertIn("Relevant via OpenAlex", titles, "OpenAlex citing papers are included")
        self.assertNotIn("Relevant similar 0", titles, "already reported earlier")
        self.assertFalse(any(c[0] == "works" and c[2] for c in s.calls), "keyword search only with an OpenAlex key")
        via = next(i for i in result["items"] if i["title"] == "Relevant via OpenAlex")
        self.assertEqual(via["cites"], ["P2（A 2）"])
        self.assertEqual(candidate_key({"doi": "10.7/x"}), "doi:10.7/x")
        text = render_tracking({"key": "K", "name": "实体壳", "path": "a/实体壳"},
                               [{"date": "2026-10-28", "since": "2026-05-01", "stats": result["stats"], "notes": ["说明"],
                                 "items": result["items"]}], lang="zh", review_file="RES_实体壳.md")
        self.assertIn("## 2026-10-28", text)
        self.assertIn("**相关度**：9/10", text)
        self.assertIn("[研究综述](<RES_实体壳.md>)", text)


class FakeDatabases(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def reply(self, status, body=None):
        payload = json.dumps(body or {}).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def do_POST(self):
        self.rfile.read(int(self.headers["Content-Length"]))
        self.server.seen.append(("POST", self.path, dict(self.headers)))
        self.reply(429)  # the shared pool's usual answer to /paper/batch

    always_busy = False

    def do_GET(self):
        url = urlparse(self.path)
        self.server.seen.append(("GET", self.path, dict(self.headers)))
        if self.always_busy and "/graph/" in url.path:
            return self.reply(429)
        if url.path.endswith("DOI:10.1/a"):
            self.server.hits += 1
            return self.reply(429) if self.server.hits == 1 else self.reply(200, {"paperId": "A", "citationCount": 3})
        if url.path.startswith("/works/doi:"):
            return self.reply(200, {"id": "https://openalex.org/W1", "key_sent": "api_key" in parse_qs(url.query)})
        self.reply(404)


class ScholarClientTests(unittest.TestCase):
    def test_throttled_batch_falls_back_to_single_lookups_with_retries(self):
        server = ThreadingHTTPServer(("127.0.0.1", 0), FakeDatabases)
        server.seen, server.hits = [], 0
        threading.Thread(target=server.serve_forever, daemon=True).start()
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        base = f"http://127.0.0.1:{server.server_port}"
        waits, notices = [], []
        client = ScholarClient("s2-key", "oa-key", s2_base=base, openalex_base=base, s2_pause=0, openalex_pause=0,
                               sleep=waits.append, on_wait=lambda seconds, reason: notices.append(reason))
        rows = client.s2_papers(["DOI:10.1/a", "DOI:10.1/missing"], "paperId,citationCount")
        self.assertEqual(rows, [{"paperId": "A", "citationCount": 3}, None])
        self.assertFalse(client.batch_ok, "later lookups in this run skip the throttled batch endpoint")
        self.assertEqual(len([s for s in server.seen if s[0] == "POST"]), 2)
        self.assertTrue(waits and all(w > 0 for w in waits), "429 answers are retried after a pause")
        self.assertTrue(any("繁忙" in n for n in notices), "the wait is reported to the progress line")
        self.assertTrue(all(s[2].get("x-api-key") == "s2-key" for s in server.seen), "the optional key is sent as a header")
        self.assertTrue(client.openalex_work("10.1/a")["key_sent"])

    def test_a_dropped_connection_switches_between_proxy_and_direct(self):
        server = ThreadingHTTPServer(("127.0.0.1", 0), FakeDatabases)
        server.seen, server.hits = [], 1
        threading.Thread(target=server.serve_forever, daemon=True).start()
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        base = f"http://127.0.0.1:{server.server_port}"
        waits = []
        client = ScholarClient(s2_base=base, openalex_base=base, s2_pause=0, openalex_pause=0, sleep=waits.append)
        broken = client.sessions["proxy"]
        broken.request = lambda *a, **k: (_ for _ in ()).throw(requests.ConnectionError("TLS EOF"))
        self.assertEqual(client.request("s2", "GET", base + "/graph/v1/paper/DOI:10.1/a")["paperId"], "A")
        self.assertEqual(client.route["s2"], "direct", "the working route is kept for later requests")
        self.assertEqual(waits, [], "the other route is tried at once")
        self.assertEqual(client.route["openalex"], "proxy", "each service keeps its own route")

    def test_three_failed_lookups_skip_semantic_scholar_for_the_run(self):
        server = ThreadingHTTPServer(("127.0.0.1", 0), FakeDatabases)
        server.seen, server.hits = [], 0
        threading.Thread(target=server.serve_forever, daemon=True).start()
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        base = f"http://127.0.0.1:{server.server_port}"
        client = ScholarClient(s2_base=base, openalex_base=base, s2_pause=0, openalex_pause=0, sleep=lambda s: None)
        server.hits = 1  # DOI:10.1/a would answer 429 once more: make every lookup a throttled one instead
        FakeDatabases.always_busy = True
        self.addCleanup(lambda: setattr(FakeDatabases, "always_busy", False))
        rows = client.s2_papers(["DOI:busy/1", "DOI:busy/2", "DOI:busy/3", "DOI:busy/4", "DOI:busy/5"], "paperId")
        self.assertEqual(rows, [None] * 5)
        self.assertTrue(client.s2_down)
        singles = [s for s in server.seen if s[0] == "GET"]
        self.assertEqual(len(singles), 3 * 3, "three lookups with three tries each, then no more requests")
        self.assertIsNotNone(client.openalex_work("10.1/a"), "OpenAlex is unaffected")


class KeylessTrackingTests(unittest.TestCase):
    def test_without_s2_key_citing_papers_come_from_openalex_only(self):
        s = FakeScholar()
        s.s2_fast = False
        seeds = [{"key": "K1", "code": "P1", "label": "A 1", "title": "T1", "doi": "10.1/1", "year": "2020",
                  "openalex_id": "W1", "openalex_count": 20},
                 {"key": "K2", "code": "P2", "label": "A 2", "title": "T2", "doi": "10.1/2", "year": "2018",
                  "openalex_id": "W2", "openalex_count": 5000}]
        s.citing_rows = [{"id": "https://openalex.org/W9", "doi": "10.7/x", "title": "Relevant citing", "publication_date":
                          time.strftime("%Y-%m-%d"), "referenced_works": ["https://openalex.org/W1"]}]
        result = track(s, seeds, path="x", profile={}, since="2000-01-01", library_dois=set(), library_titles=set(), seen=set(),
                       cache={}, llm_call=lambda system, prompt: ScriptedModel().chat(system, prompt)[0])
        self.assertEqual([i["title"] for i in result["items"]], ["Relevant citing"])
        self.assertFalse([c for c in s.calls if c[0] == "citations"], "no per-paper Semantic Scholar requests without a key")
        works = [c for c in s.calls if c[0] == "works"]
        self.assertEqual(works[0][1].split(",")[0], "cites:W1", "the classic (5000 citations by OpenAlex) is not a seed")


class ConfigTests(unittest.TestCase):
    def test_scholar_keys_are_encrypted_and_restored(self):
        home = Path(tempfile.mkdtemp())
        config = Config(home)
        config.scholar_keys.update(s2="s2-secret-key", openalex="oa-secret-key")
        config.data["research_roots"] = ["ROOT0001"]
        config.save()
        raw = (home / "settings.json").read_text(encoding="utf-8")
        self.assertNotIn("secret-key", raw)
        again = Config(home)
        self.assertEqual(again.scholar_keys, {"s2": "s2-secret-key", "openalex": "oa-secret-key"})
        self.assertEqual(again.data["research_roots"], ["ROOT0001"])
        self.assertFalse(again.data["research_enabled"], "the feature is off until the user turns it on")


if __name__ == "__main__":
    unittest.main()
