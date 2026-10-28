"""Collection-level research reviews, built bottom-up along the user's own Zotero collection tree.

Workflow (the same for every provider):
1. Leaf collection: the model reads each paper's AI summary card (or, without one, the Zotero abstract) plus the
   citation links between the papers, and writes the collection's review — research objects and keywords, a research
   map of sub-directions, the lineage of stages and inheritance links, development, progress, gaps — as JSON.
2. With "re-read" on, the method and conclusion sections of the key papers' full texts are given back with the draft
   and the model checks and completes it against the originals.
3. Parent collection: the model merges its sub-collections' reports with the papers filed directly in it, the way
   GraphRAG builds higher-level community reports from lower-level ones instead of re-reading every source.
4. Too much material for one call: papers are reviewed in groups first and the group reviews merged like
   sub-collections (also used when the provider reports its context window is exceeded).
5. The software renders the Markdown report (with Mermaid diagrams) and the HTML research map itself, so the files
   look the same whichever model wrote them.
"""
from __future__ import annotations

import html
import json
import re
from datetime import date
from pathlib import Path

from .clients import ServiceError
from .llm import ContextTooLong
from .summary import HEADING, clean_markdown, estimate_tokens

PROMPT_VERSION = 1
LANGUAGE = {"zh": "简体中文", "en": "English"}
SYSTEM = """你是一名资深的科研综述专家，擅长梳理一个研究方向的文献脉络。你将阅读一个文献分类中的资料（论文的 AI 总结卡片、摘要或题录，子分类的研究综述），以及论文之间的引用关系，为这个分类撰写研究综述。综述保存在文献库中，供研究者和 AI 检索助手快速了解这个方向的研究对象、研究地图、研究脉络和研究进展，因此必须准确、具体、有依据。

必须遵守：
1. 只依据给出的资料写作，不编造论文、作者、数值或结论；资料没有涉及的内容不要写。
2. 用资料中的编号指代论文和子分类（论文 P1、P2……，子分类 C1、C2……，分组 G1、G2……），不要自己编号，不要写论文标题全称。
3. 写具体内容：研究对象及其参数、方法和模型名称、关键方程或假设、代表性定量结果；不要写空泛的套话。
4. 专业术语首次出现时写作“中文术语（English term）”（用英文写作时直接写英文术语）。
5. 只输出一个 JSON 对象，不要用代码块包裹，不要写任何解释。"""

SCHEMA = """输出一个 JSON 对象，字段名保持英文，结构如下：
{
  "overview": "概述，200–400 字：这个分类研究什么问题、有哪几条主要技术路线、代表性成果和目前的发展阶段",
  "keywords": ["8–15 个研究关键词"],
  "objects": [{"name": "研究对象", "type": "类型（结构、材料、现象、载荷与工况、方法对象等）", "details": "关键参数、尺度、工况和边界条件", "papers": ["P1"]}],
  "branches": [{"name": "子方向名称", "summary": "这一子方向研究什么、采用什么方法和模型", "papers": ["P1", "P3"],
                "children": [{"name": "更细的方向", "summary": "……", "papers": ["P3"]}]}],
  "lineage": [{"period": "起止年份，如 2015–2018", "stage": "阶段名称", "summary": "这一阶段的主要工作、方法和模型",
               "advance": "相对前一阶段解决了什么问题、有什么改进", "papers": ["P2"]}],
  "links": [{"from": "P2", "to": "P5", "relation": "一句话：后者如何继承、改进、对比或应用前者的工作"}],
  "development": "研究发展情况：按时间说明研究如何兴起、方法如何演变、研究热点如何转移（可用“- ”列表）",
  "progress": "研究进展：当前的最新水平和代表性成果，写出具体方法、模型和定量结果（可用“- ”列表）",
  "gaps": ["尚未解决的问题或研究空白，3–6 条"],
  "directions": ["值得关注的研究方向，3–6 条"],
  "search": {"queries": ["3–5 条英文检索式，每条 3–8 个英文单词，用于检索这一方向新发表的论文"],
             "profile": "用 2–4 句英文概括这一分类的研究兴趣（研究对象、方法、模型），用于判断新论文是否相关"}
}"""

RULES = {
    "leaf": """要求：
- branches 构成研究地图：按研究内容把论文分成 2–6 个子方向，必要时再细分一层；每篇论文至少归入一个子方向。
- lineage 构成研究脉络：按时间先后分成 2–6 个阶段，说明方法和认识如何一步步发展。
- links 写出论文之间最重要的继承和发展关系（至多 15 条），优先采用上面列出的已知引用关系；from 是较早的工作，to 是在其基础上发展的工作。
- 资料较少时可以减少条目数量，但不要编造。""",
    "parent": """要求：
- 这是上级分类：在各子分类综述（C 编号）的基础上汇总、整合，写出整个分类的研究综述；直接放在本分类中的论文用 P 编号。
- branches 以子分类为主干组织研究地图，papers 中写子分类编号（如 C1）和直接论文编号（如 P2）；每个子分类至少出现一次。
- lineage 综合各子分类的研究脉络，写出整个方向的发展阶段，papers 中可写 C 和 P 编号。
- links 写出子分类之间、或论文之间最重要的继承、交叉和方法迁移关系（至多 15 条），from/to 用 C 或 P 编号。
- objects 和 keywords 要覆盖各子分类的主要研究对象和关键词，并说明它们的联系与区别。""",
    "groups": """要求：
- 资料是同一分类的论文分组后，各组的初步综述（G 编号）；其中的 P 编号对全体论文统一有效。请把它们合并为整个分类的研究综述。
- branches、lineage、links 的 papers / from / to 中写 P 编号（不要写 G 编号）；每篇论文至少归入一个子方向。
- 合并重复的研究对象、子方向和阶段，保留具体的方法、模型和定量结果。""",
}

CHECK = """下面是你为文献分类“{path}”写出的研究综述初稿（JSON），以及其中关键论文全文的方法和结论段落（摘自论文 Markdown 全文）。请对照原文逐项核对初稿：
1. 改正与原文不符、含糊或张冠李戴的内容；
2. 补充关键论文的具体模型、方法和定量结果；
3. 研究脉络中的继承和发展关系要有依据（已知引用关系或原文表述），没有依据的删去；
4. 保持 JSON 结构和字段名不变，论文仍用编号指代，所有文字仍用{language}书写，直接输出修订后的完整 JSON。

<draft>
{draft}
</draft>

论文编号对照：
{index}

{excerpts}"""

REPAIR = """你上一次的输出不是合法的 JSON（{error}）。请把下面的内容整理成一个合法的 JSON 对象，只输出 JSON。

{schema}

<previous>
{text}
</previous>"""

LABELS = {
    "zh": {"title": "研究综述", "path": "分类路径", "links": "关联", "zotero": "Zotero 分类", "map": "研究地图（交互图）",
           "tracking": "新文献追踪", "parent": "上一级综述", "coverage": "资料覆盖", "objects": "研究对象与关键词",
           "keywords": "关键词", "atlas": "研究地图", "lineage": "研究脉络", "development": "研究发展情况",
           "progress": "研究进展", "gaps": "研究空白与问题", "directions": "值得关注的方向", "children": "子分类",
           "papers": "论文清单", "relations": "关键继承与发展关系", "years": "年度分布", "advance": "进步",
           "related": "相关论文", "object": "研究对象", "type": "类型", "details": "关键参数与工况", "branch": "子方向",
           "content": "研究内容", "count": "篇数", "year": "年份", "author": "作者", "paper_title": "标题",
           "source": "来源", "basis": "依据", "open": "链接"},
    "en": {"title": "Research review", "path": "Collection path", "links": "Linked", "zotero": "Zotero collection",
           "map": "Research map (interactive)", "tracking": "New-paper tracking", "parent": "Parent review",
           "coverage": "Coverage", "objects": "Research objects and keywords", "keywords": "Keywords",
           "atlas": "Research map", "lineage": "Research lineage", "development": "Development", "progress": "Progress",
           "gaps": "Gaps and open problems", "directions": "Directions worth watching", "children": "Sub-collections",
           "papers": "Papers", "relations": "Key inheritance links", "years": "Papers per year", "advance": "Advance",
           "related": "Papers", "object": "Object", "type": "Type", "details": "Key parameters and conditions",
           "branch": "Direction", "content": "Content", "count": "Count", "year": "Year", "author": "Authors",
           "paper_title": "Title", "source": "Source", "basis": "Basis", "open": "Links"},
}
BASIS = {"zh": {"card": "AI 总结卡片", "abstract": "仅摘要", "meta": "仅题录"},
         "en": {"card": "AI card", "abstract": "abstract only", "meta": "metadata only"}}
# Not \b: Chinese characters count as word characters, and models write "P3把…" or "向P5".
CODE = re.compile(r"(?<![A-Za-z0-9])([PCG])(\d{1,3})(?![0-9A-Za-z])")
FRONT_MATTER = re.compile(r"\A---\n.*?\n---\n", re.DOTALL)
METHOD_HEADING = re.compile(r"method|formulation|model|theor|framework|approach|governing|方法|模型|理论|公式", re.IGNORECASE)
CONCLUSION_HEADING = re.compile(r"conclu|summary|closing|remarks|结论|总结|结语", re.IGNORECASE)


# ------------------------------------------------------------------ material
def card_body(text: str) -> str:
    """The summary card without its YAML block, title line and link line (they repeat Zotero metadata)."""
    text = FRONT_MATTER.sub("", text.replace("\r\n", "\n"), count=1)
    lines = [line for line in text.splitlines()
             if not line.startswith("# ") and not line.startswith(("**关联**", "**Linked**", "**作者**", "**Authors**"))]
    return re.sub(r"\n{3,}", "\n\n", "\n".join(lines)).strip()


def paper_label(paper: dict) -> str:
    creator = (paper.get("creator") or "").strip() or "佚名"
    return f"{creator} {paper.get('year') or 'n.d.'}".strip()


def citation_edges(papers: list[dict]) -> list[tuple[str, str]]:
    """(citing code, cited code) pairs among the papers, from OpenAlex reference lists."""
    by_id = {p["openalex_id"]: p["code"] for p in papers if p.get("openalex_id")}
    edges = []
    for paper in papers:
        for ref in paper.get("refs") or []:
            target = by_id.get(ref)
            if target and target != paper["code"]:
                edges.append((paper["code"], target))
    return sorted(set(edges), key=lambda e: (int(e[0][1:]), int(e[1][1:])))


def paper_block(paper: dict, lang: str) -> str:
    lines = [f"[{paper['code']}] {paper.get('year') or '年份不详'} · {paper.get('creator') or ''} · {paper.get('venue') or ''}".rstrip(" ·"),
             f"标题：{paper.get('title', '')}",
             f"资料：{BASIS['zh'][paper['source']]}"]
    if paper["source"] == "card":
        lines.append(paper["card"])
    elif paper["source"] == "abstract":
        lines.append("摘要：" + paper["abstract"])
    return "\n".join(lines)


def child_block(code: str, child: dict, *, with_codes: bool = False) -> str:
    """A sub-collection's (or paper group's) review as material for the level above.

    Paper codes inside a sub-collection's review are local to it, so they are left out unless `with_codes` (paper
    groups of one collection share one numbering).
    """
    report = child["report"]
    refs = (lambda codes: f"（{'、'.join(codes)}）" if with_codes and codes else "")  # noqa: E731
    lines = [f"[{code}] {'子分类' if not with_codes else '分组'}：{child['name']}（{child.get('paper_total', 0)} 篇论文）",
             "概述：" + report.get("overview", ""),
             "关键词：" + "；".join(report.get("keywords", []))]
    if report.get("objects"):
        lines.append("研究对象：" + "；".join(f"{o['name']}（{o.get('type', '')}）{refs(o.get('papers'))}：{o.get('details', '')}"
                                         for o in report["objects"]))
    if report.get("branches"):
        lines.append("子方向：" + "；".join(f"{b['name']}{refs(b.get('papers'))}：{b.get('summary', '')}" for b in report["branches"]))
    if report.get("lineage"):
        lines.append("研究脉络：" + "；".join(f"{s.get('period', '')} {s.get('stage', '')}{refs(s.get('papers'))}：{s.get('summary', '')}"
                                          for s in report["lineage"]))
    if with_codes and report.get("links"):
        lines.append("继承关系：" + "；".join(f"{l['from']}→{l['to']}：{l['relation']}" for l in report["links"]))
    for name, label in (("development", "发展情况"), ("progress", "研究进展")):
        if report.get(name):
            lines.append(f"{label}：" + report[name])
    if report.get("gaps"):
        lines.append("研究空白：" + "；".join(report["gaps"]))
    return "\n".join(lines)


def excerpt(markdown: str, limit: int = 3500) -> str:
    """Abstract/introduction start, the main method section and the conclusions of a full text."""
    text = clean_markdown(markdown)
    headings = list(HEADING.finditer(text))
    sections = []
    for n, match in enumerate(headings):
        end = headings[n + 1].start() if n + 1 < len(headings) else len(text)
        sections.append((match.group(1).strip("*_ "), text[match.start():end]))
    pieces = [text[:min(limit // 2, len(text))]]
    method = next((body for title, body in sections if METHOD_HEADING.search(title) and len(body) > 400), "")
    conclusion = next((body for title, body in reversed(sections) if CONCLUSION_HEADING.search(title)), "")
    if method:
        pieces.append(method[:limit])
    pieces.append(conclusion[:limit] if conclusion else text[-limit:])
    return "\n\n…\n\n".join(p.strip() for p in pieces if p.strip())


# ------------------------------------------------------------------ JSON answers
def _clean_list(value) -> list[str]:
    if isinstance(value, str):
        value = re.split(r"[；;\n]", value)
    return [str(v).strip() for v in value or [] if str(v).strip()] if isinstance(value, list) else []


def _codes(value, valid: set[str]) -> list[str]:
    found = []
    for item in value if isinstance(value, list) else [value]:
        for prefix, number in CODE.findall(str(item)):
            code = prefix + number
            if code in valid and code not in found:
                found.append(code)
    return found


def normalize_report(data: dict, valid: set[str]) -> dict:
    """Coerce a model's JSON into the fixed structure, dropping references to papers that do not exist."""
    def text(value) -> str:
        if isinstance(value, list):
            return "\n".join("- " + str(v).strip() for v in value if str(v).strip())
        return str(value or "").strip()

    def branches(rows, depth=0):
        result = []
        for row in rows if isinstance(rows, list) else []:
            if isinstance(row, dict) and str(row.get("name", "")).strip():
                result.append({"name": str(row["name"]).strip(), "summary": text(row.get("summary")),
                               "papers": _codes(row.get("papers"), valid),
                               "children": branches(row.get("children"), depth + 1) if depth == 0 else []})
        return result

    search = data.get("search") if isinstance(data.get("search"), dict) else {}
    report = {
        "overview": text(data.get("overview")),
        "keywords": _clean_list(data.get("keywords"))[:20],
        "objects": [{"name": str(o.get("name", "")).strip(), "type": text(o.get("type")), "details": text(o.get("details")),
                     "papers": _codes(o.get("papers"), valid)}
                    for o in data.get("objects") or [] if isinstance(o, dict) and str(o.get("name", "")).strip()],
        "branches": branches(data.get("branches")),
        "lineage": [{"period": re.sub(r"^(\d{4})\s*[–—-]\s*\1$", r"\1", text(s.get("period"))), "stage": text(s.get("stage")), "summary": text(s.get("summary")),
                     "advance": text(s.get("advance")), "papers": _codes(s.get("papers"), valid)}
                    for s in data.get("lineage") or [] if isinstance(s, dict) and (s.get("stage") or s.get("summary"))],
        "links": [],
        "development": text(data.get("development")), "progress": text(data.get("progress")),
        "gaps": _clean_list(data.get("gaps")), "directions": _clean_list(data.get("directions")),
        "search": {"queries": _clean_list(search.get("queries"))[:6], "profile": text(search.get("profile"))},
    }
    for link in data.get("links") or []:
        if isinstance(link, dict):
            source, target = _codes(link.get("from"), valid), _codes(link.get("to"), valid)
            if source and target and source[0] != target[0]:
                report["links"].append({"from": source[0], "to": target[0], "relation": text(link.get("relation"))})
    return report


def parse_json(text: str):
    text = text.strip()
    fenced = re.search(r"```(?:json)?\s*(.*?)```", text, re.DOTALL)
    if fenced:
        text = fenced.group(1)
    start, end = text.find("{"), text.rfind("}")
    if start < 0 or end <= start:
        raise ValueError("没有找到 JSON 对象")
    body = text[start:end + 1]
    try:
        return json.loads(body)
    except json.JSONDecodeError:
        return json.loads(re.sub(r",\s*([}\]])", r"\1", body))  # trailing commas are the most common slip


def ask_json(call, prompt: str, valid: set[str], log) -> dict:
    answer = call(prompt)
    try:
        data = parse_json(answer)
    except ValueError as error:
        log("模型输出不是合法 JSON，请模型重新整理一次…")
        answer = call(REPAIR.format(error=str(error)[:120], schema=SCHEMA, text=answer[:30000]))
        try:
            data = parse_json(answer)
        except ValueError:
            raise ServiceError("大模型没有按要求输出 JSON 格式的综述，可换用更强的模型后重试。", retryable=True) from None
    if not isinstance(data, dict):
        raise ServiceError("大模型输出的 JSON 结构不正确。", retryable=True)
    report = normalize_report(data, valid)
    if not report["overview"] and not report["branches"]:
        raise ServiceError("大模型输出的综述缺少概述和研究地图，可换用更强的模型后重试。", retryable=True)
    return report


# ------------------------------------------------------------------ review workflow
def review_prompt(path: str, lang: str, mode: str, papers: list[dict], children: list[tuple[str, str]],
                  edges: list[tuple[str, str]]) -> str:
    cards = sum(p["source"] == "card" for p in papers)
    abstracts = sum(p["source"] == "abstract" for p in papers)
    lines = [f"请为文献分类“{path}”撰写研究综述。所有文字内容用{LANGUAGE[lang]}书写（search 字段除外，用英文）。", ""]
    if mode == "parent":
        lines.append(f"资料说明：{len(children)} 个子分类的研究综述（C 编号）；直接放在本分类中的论文 {len(papers)} 篇（P 编号）。")
    elif mode == "groups":
        lines.append(f"资料说明：本分类论文较多，已分为 {sum(1 for code, _ in children if code)} 组分别写出初步综述（G 编号）。")
    else:
        lines.append(f"资料说明：共 {len(papers)} 篇论文，其中 {cards} 篇提供 AI 总结卡片（基于全文精读），{abstracts} 篇只有摘要，"
                     f"{len(papers) - cards - abstracts} 篇只有题录。只有摘要或题录的论文信息较少，不要过度推断。")
    if papers:
        known = "；".join(f"{a} 引用了 {b}" for a, b in edges) if edges else "（未获取到这些论文之间的引用关系）"
        lines += ["", "已知引用关系（来自 OpenAlex 文献数据库）：" + known]
    lines += ["", SCHEMA, "", RULES[mode], ""]
    for code, block in children:
        lines += [block, ""]
    for paper in papers:
        lines += [paper_block(paper, lang), ""]
    return "\n".join(lines).strip()


def group_papers(papers: list[dict], budget: int) -> list[list[dict]]:
    groups, current, size = [], [], 0
    for paper in sorted(papers, key=lambda p: (p.get("year") or "9999", p["code"])):
        tokens = estimate_tokens(paper_block(paper, "zh"))
        if current and size + tokens > budget:
            groups.append(current)
            current, size = [], 0
        current.append(paper)
        size += tokens
    if current:
        groups.append(current)
    return groups


def key_papers(report: dict, papers: list[dict], count: int) -> list[dict]:
    """Papers the lineage and links lean on most, then the newest; only those with a readable full text."""
    weight = {p["code"]: 0 for p in papers}
    for stage in report["lineage"]:
        for code in stage["papers"]:
            weight[code] = weight.get(code, 0) + 2
    for link in report["links"]:
        for code in (link["from"], link["to"]):
            weight[code] = weight.get(code, 0) + 2
    for branch in report["branches"]:
        for code in branch["papers"]:
            weight[code] = weight.get(code, 0) + 1
    readable = [p for p in papers if p.get("markdown_path") and Path(p["markdown_path"]).is_file()]
    readable.sort(key=lambda p: (weight.get(p["code"], 0), p.get("year") or ""), reverse=True)
    return readable[:count]


def review(call, *, path: str, lang: str, papers: list[dict], children: list[dict], reread: int = 0,
           budget: int = 0, log=lambda message: None) -> tuple[dict, dict]:
    """Write one collection's review; returns (report, info). `call(prompt) -> text` talks to the model."""
    edges = citation_edges(papers)
    child_codes = [(f"C{n}", child) for n, child in enumerate(children, 1)]
    valid = {p["code"] for p in papers} | {code for code, _ in child_codes}
    mode = "parent" if children else "leaf"
    budget = budget or 10 ** 9
    info = {"reread": 0, "groups": 0, "edges": len(edges)}

    while True:
        try:
            material = estimate_tokens("\n".join(paper_block(p, lang) for p in papers))
            if mode == "leaf" and material > budget and len(papers) > 1:
                groups = group_papers(papers, budget)
                info["groups"] = len(groups)
                partial = []
                for n, group in enumerate(groups, 1):
                    log(f"论文较多，分组综述第 {n}/{len(groups)} 组（{len(group)} 篇）…")
                    codes = {p["code"] for p in group}
                    group_edges = [e for e in edges if e[0] in codes and e[1] in codes]
                    partial.append(ask_json(call, review_prompt(f"{path}（第 {n} 组）", lang, "leaf", group, [], group_edges),
                                            codes, log))
                index = "论文编号对照：\n" + "\n".join(f"{p['code']}：{paper_label(p)}，{p.get('title', '')}" for p in papers)
                blocks = [("", index)] + [(f"G{n}", child_block(f"G{n}", {"name": f"第 {n} 组", "report": r,
                                                                         "paper_total": len(groups[n - 1])}, with_codes=True))
                                          for n, r in enumerate(partial, 1)]
                log("正在合并各组综述…")
                report = ask_json(call, review_prompt(path, lang, "groups", [], blocks, []), valid, log)
            else:
                log(f"正在阅读 {len(papers)} 篇论文资料" + (f"和 {len(children)} 个子分类综述" if children else "")
                    + f"（约 {material + sum(estimate_tokens(child_block(c, ch)) for c, ch in child_codes)} tokens），撰写研究综述…")
                blocks = [(code, child_block(code, child)) for code, child in child_codes]
                report = ask_json(call, review_prompt(path, lang, mode, papers, blocks, edges), valid, log)
            break
        except ContextTooLong:
            size = min(budget, estimate_tokens("\n".join(paper_block(p, lang) for p in papers)))
            if mode != "leaf" or size <= 6000 or len(papers) < 2:
                raise
            budget = size // 2
            log(f"资料超过模型上下文长度，改为每组约 {budget} tokens 分组综述后合并。")

    chosen = key_papers(report, papers, reread) if reread else []
    if chosen:
        log(f"正在回读 {len(chosen)} 篇关键论文的原文（方法与结论段落），核对补全综述…")
        excerpts = []
        for paper in chosen:
            try:
                text = Path(paper["markdown_path"]).read_text(encoding="utf-8", errors="replace")
            except OSError:
                continue
            excerpts.append(f"<paper code=\"{paper['code']}\">\n{excerpt(text)}\n</paper>")
        index = "\n".join(f"{p['code']}：{paper_label(p)}，{p.get('title', '')}" for p in papers)
        try:
            checked = ask_json(call, CHECK.format(path=path, language=LANGUAGE[lang], index=index, excerpts="\n\n".join(excerpts),
                                                  draft=json.dumps(report, ensure_ascii=False, indent=1)), valid, log)
            report = checked
            info["reread"] = len(excerpts)
        except ContextTooLong:
            log("回读原文超过模型上下文长度，保留初稿。")
        except ServiceError as error:
            if not error.retryable:
                raise
            log("核对稿格式不正确，保留初稿。")
    return report, info


# ------------------------------------------------------------------ rendering helpers
def expand_codes(text: str, labels: dict[str, str]) -> str:
    """"P3" → "P3（Liu 等 2022）" at its first mention in a text, so a reader need not look the code up."""
    done = set()

    def replace(match):
        code = match.group(0)
        after = text[match.end():match.end() + 1]
        if code not in labels or code in done or (after and after in "（("):
            return code
        done.add(code)
        return f"{code}（{labels[code]}）"
    return CODE.sub(replace, text)


def mermaid_text(value: str, limit: int = 40) -> str:
    """Text Mermaid accepts in mindmap/timeline/flowchart labels: no brackets, quotes, colons or semicolons."""
    value = re.sub(r"\s+", " ", str(value)).strip()
    table = str.maketrans({"(": "（", ")": "）", "[": "［", "]": "］", "{": "｛", "}": "｝", ":": "：", ";": "；",
                           "\"": "'", "`": "'", "#": "＃", "<": "‹", ">": "›", "|": "｜", "%": "％", "&": "＆"})
    value = value.translate(table)
    return value[:limit - 1] + "…" if len(value) > limit else value


def mindmap(name: str, report: dict, labels: dict[str, str]) -> str:
    lines = ["```mermaid", "mindmap", f"  root(({mermaid_text(name, 24)}))"]
    for branch in report["branches"]:
        lines.append("    " + mermaid_text(branch["name"]))
        for child in branch["children"]:
            lines.append("      " + mermaid_text(child["name"]))
        if not branch["children"]:
            for code in branch["papers"][:6]:
                lines.append("      " + mermaid_text(f"{code} {labels.get(code, '')}"))
    lines.append("```")
    return "\n".join(lines)


def timeline(name: str, report: dict, labels: dict[str, str]) -> str:
    lines = ["```mermaid", "timeline", f"    title {mermaid_text(name, 30)}"]
    for stage in report["lineage"]:
        lines.append(f"    section {mermaid_text(stage['period'] or '—', 20)}")
        events = [mermaid_text(stage["stage"] or "阶段", 30)]
        events += [mermaid_text(f"{code} {labels.get(code, '')}", 30) for code in stage["papers"][:5]]
        lines.append("      " + " : ".join(events))
    lines.append("```")
    return "\n".join(lines)


def flowchart(report: dict, labels: dict[str, str]) -> str:
    if not report["links"]:
        return ""
    lines = ["```mermaid", "flowchart LR"]
    for code in dict.fromkeys(c for link in report["links"] for c in (link["from"], link["to"])):
        lines.append(f'  {code}["{mermaid_text(code + " · " + labels.get(code, ""), 36)}"]')
    for link in report["links"]:
        lines.append(f"  {link['from']} --> {link['to']}")
    lines.append("```")
    return "\n".join(lines)


def file_link(path: str) -> str:
    return "<" + Path(path).resolve().as_uri() + ">" if path else ""


def render_markdown(collection: dict, report: dict, papers: list[dict], children: list[dict], *, lang: str,
                    model: str, info: dict, links: dict[str, str]) -> str:
    """The collection's review as Markdown; `links` maps "parent"/"map"/"tracking"/child keys to relative paths."""
    L = LABELS[lang]
    labels = {p["code"]: paper_label(p) for p in papers}
    labels.update({f"C{n}": child["name"] for n, child in enumerate(children, 1)})
    expand = lambda value: expand_codes(value, labels)  # noqa: E731
    coverage = {kind: sum(p["source"] == kind for p in papers) for kind in ("card", "abstract", "meta")}
    total = collection.get("paper_total", len(papers))
    front = {
        "collection": collection["name"], "collection_key": collection["key"], "path": collection["path"],
        "level": "parent" if children else "leaf", "papers_total": total, "papers_direct": len(papers),
        "coverage": coverage, "subcollections": [c["name"] for c in children], "keywords": report["keywords"],
        "zotero_link": f"zotero://select/library/collections/{collection['key']}",
        "generated_by": model, "generated_at": date.today().isoformat(), "prompt_version": PROMPT_VERSION,
    }
    lines = ["---", *[f"{k}: {json.dumps(v, ensure_ascii=False)}" for k, v in front.items()], "---", "",
             f"# {L['title']}：{collection['name']}", "",
             f"**{L['path']}**：{collection['path'].replace('/', ' / ')}", ""]
    related = [(L["zotero"], f"zotero://select/library/collections/{collection['key']}")]
    for name, label in (("map", L["map"]), ("tracking", L["tracking"]), ("parent", L["parent"])):
        if links.get(name):
            related.append((label, f"<{links[name]}>"))
    lines += [f"**{L['links']}**：" + " ｜ ".join(f"[{text}]({target})" for text, target in related), ""]
    if lang == "zh":
        cover = (f"本分类共 {total} 篇论文" + (f"（其中 {len(papers)} 篇直接在本分类中，其余在子分类中）" if children else "")
                 + f"；直接论文中 {coverage['card']} 篇依据 AI 总结卡片，{coverage['abstract']} 篇仅依据摘要，"
                 + f"{coverage['meta']} 篇仅依据题录" + (f"；回读了 {info.get('reread', 0)} 篇关键论文的原文" if info.get("reread") else "")
                 + (f"；获取到 {info.get('edges', 0)} 条论文之间的引用关系" if info.get("edges") else "") + "。")
    else:
        cover = (f"{total} papers; {coverage['card']} read via AI cards, {coverage['abstract']} via abstracts only, "
                 f"{coverage['meta']} metadata only; {info.get('reread', 0)} full texts re-read; {info.get('edges', 0)} citation links.")
    lines += [f"**{L['coverage']}**：{cover}", "", "> " + expand(report["overview"]).replace("\n", "\n> "), ""]

    lines += [f"## {L['objects']}", "", f"**{L['keywords']}**：" + ("；" if lang == "zh" else "; ").join(report["keywords"]), ""]
    if report["objects"]:
        lines += [f"| {L['object']} | {L['type']} | {L['details']} | {L['related']} |", "|---|---|---|---|"]
        for o in report["objects"]:
            cells = [o["name"], o["type"], expand(o["details"]), "、".join(expand(c) for c in o["papers"])]
            lines.append("| " + " | ".join(c.replace("|", "｜").replace("\n", " ") for c in cells) + " |")
        lines.append("")

    lines += [f"## {L['atlas']}", "", mindmap(collection["name"], report, labels), ""]
    for branch in report["branches"]:
        lines += [f"### {branch['name']}", "", expand(branch["summary"])]
        if branch["papers"]:
            lines += ["", f"{L['related']}：" + "、".join(expand(c) for c in branch["papers"])]
        for child in branch["children"]:
            lines += ["", f"- **{child['name']}**：{expand(child['summary'])}"
                      + (f"（{'、'.join(child['papers'])}）" if child["papers"] else "")]
        lines.append("")

    lines += [f"## {L['lineage']}", ""]
    if report["lineage"]:
        lines += [timeline(collection["name"], report, labels), ""]
        for stage in report["lineage"]:
            lines += [f"### {stage['period']} · {stage['stage']}", "", expand(stage["summary"])]
            if stage["advance"]:
                lines += ["", f"**{L['advance']}**：{expand(stage['advance'])}"]
            if stage["papers"]:
                lines += ["", f"{L['related']}：" + "、".join(expand(c) for c in stage["papers"])]
            lines.append("")
    if report["links"]:
        lines += [f"### {L['relations']}", "", flowchart(report, labels), ""]
        lines += [f"- {expand(l['from'])} → {expand(l['to'])}：{expand(l['relation'])}" for l in report["links"]]
        lines.append("")

    lines += [f"## {L['development']}", ""]
    years: dict[str, list[str]] = {}
    for paper in papers:
        years.setdefault(paper.get("year") or "—", []).append(paper["code"])
    if papers:
        lines += [f"**{L['years']}**", "", f"| {L['year']} | {L['count']} | {L['related']} |", "|---|---|---|"]
        lines += [f"| {y} | {len(codes)} | {'、'.join(codes)} |" for y, codes in sorted(years.items())]
        lines.append("")
    lines += [expand(report["development"]), "", f"## {L['progress']}", "", expand(report["progress"]), ""]
    for name in ("gaps", "directions"):
        if report[name]:
            lines += [f"### {L[name]}", "", *[f"- {expand(v)}" for v in report[name]], ""]

    if children:
        lines += [f"## {L['children']}", "", f"| | {L['children']} | {L['count']} | {L['keywords']} |", "|---|---|---|---|"]
        for n, child in enumerate(children, 1):
            target = links.get("child:" + child["key"])
            name = f"[{child['name']}](<{target}>)" if target else child["name"]
            lines.append(f"| C{n} | {name} | {child.get('paper_total', 0)} | {'；'.join(child['report'].get('keywords', [])[:6])} |")
        lines.append("")
    if papers:
        lines += [f"## {L['papers']}", "",
                  f"| | {L['year']} | {L['author']} | {L['paper_title']} | {L['source']} | {L['basis']} | {L['open']} |",
                  "|---|---|---|---|---|---|---|"]
        for paper in papers:
            opens = [f"[Zotero](zotero://select/library/items/{paper['key']})"]
            if paper.get("card_path"):
                opens.append(f"[{'总结' if lang == 'zh' else 'card'}]({file_link(paper['card_path'])})")
            if paper.get("markdown_path"):
                opens.append(f"[{'全文' if lang == 'zh' else 'full text'}]({file_link(paper['markdown_path'])})")
            if paper.get("doi"):
                opens.append(f"[DOI](https://doi.org/{paper['doi']})")
            cells = [paper["code"], paper.get("year") or "", paper.get("creator") or "", paper.get("title", ""),
                     paper.get("venue") or "", BASIS[lang][paper["source"]], " ".join(opens)]
            lines.append("| " + " | ".join(str(c).replace("|", "｜").replace("\n", " ") for c in cells) + " |")
        lines.append("")
    return "\n".join(lines).rstrip() + "\n"


# ------------------------------------------------------------------ HTML research map
PALETTE = ["#2563eb", "#16a34a", "#d97706", "#9333ea", "#dc2626", "#0891b2", "#be185d", "#4d7c0f", "#7c3aed", "#b45309"]


def render_map(collection: dict, report: dict, nodes: list[dict], edges: list[tuple[str, str]], links: list[tuple[str, str]],
               *, lang: str, report_file: str) -> str:
    """A self-contained page: the research map as a tree and a timeline graph of the papers and their citations.

    nodes: {"id", "label", "title", "year", "group", "href"}; edges: citation (citing id, cited id);
    links: inheritance (earlier id, later id) proposed in the review.
    """
    L = LABELS[lang]
    groups = list(dict.fromkeys(n["group"] for n in nodes))
    color = {g: PALETTE[i % len(PALETTE)] for i, g in enumerate(groups)}
    years = [int(n["year"]) for n in nodes if str(n.get("year", "")).isdigit()]
    low, high = (min(years), max(years)) if years else (2000, 2001)
    if low == high:
        low, high = low - 1, high + 1
    left, right, top, lane = 190, 40, 50, 64
    width = 1100
    height = top + lane * max(1, len(groups)) + 40
    span = width - left - right

    def x_of(year):
        return left + (int(year) - low) / (high - low) * span if str(year).isdigit() else left - 30

    placed: dict[str, tuple[float, float]] = {}
    crowd: dict[tuple, int] = {}
    for node in nodes:
        row = groups.index(node["group"])
        cell = (row, node.get("year"))
        k = crowd.get(cell, 0)
        crowd[cell] = k + 1
        offset = ((k + 1) // 2) * (1 if k % 2 else -1) * 13
        placed[node["id"]] = (x_of(node.get("year")) + (k // 4) * 9, top + row * lane + lane / 2 + max(-26, min(26, offset)))

    svg = [f'<svg viewBox="0 0 {width} {height}" role="img" aria-label="{html.escape(L["lineage"])}">',
           '<defs><marker id="arrow" viewBox="0 0 10 10" refX="9" refY="5" markerWidth="6" markerHeight="6" orient="auto-start-reverse">'
           '<path d="M0,0 L10,5 L0,10 z" class="arrowhead"/></marker></defs>']
    for row, group in enumerate(groups):
        y = top + row * lane
        svg.append(f'<rect x="0" y="{y}" width="{width}" height="{lane}" class="lane{row % 2}"/>')
        svg.append(f'<text x="10" y="{y + lane / 2 + 4}" class="lanelabel" fill="{color[group]}">{html.escape(group[:16])}</text>')
    for year in range(low, high + 1):
        if (high - low) <= 25 or year % 5 == 0:
            x = x_of(year)
            svg.append(f'<line x1="{x}" y1="{top - 8}" x2="{x}" y2="{height - 30}" class="grid"/>'
                       f'<text x="{x}" y="{height - 12}" class="axis" text-anchor="middle">{year}</text>')
    for kind, pairs in (("cite", edges), ("inherit", links)):
        for source, target in pairs:
            if source in placed and target in placed and source != target:
                (x1, y1), (x2, y2) = placed[source], placed[target]
                svg.append(f'<line x1="{x1}" y1="{y1}" x2="{x2}" y2="{y2}" class="{kind}" marker-end="url(#arrow)"/>')
    for node in nodes:
        x, y = placed[node["id"]]
        tip = html.escape(f"{node['label']} · {node['title']}")
        svg.append(f'<a href="{html.escape(node["href"])}"><circle cx="{x:.1f}" cy="{y:.1f}" r="7" fill="{color[node["group"]]}">'
                   f'<title>{tip}</title></circle></a>')
        if node.get("code"):
            svg.append(f'<text x="{x + 9:.1f}" y="{y + 4:.1f}" class="code">{html.escape(node["code"])}</text>')
    svg.append("</svg>")

    def tree(branches):
        parts = ["<ul>"]
        for branch in branches:
            parts.append(f"<li><details open><summary><b>{html.escape(branch['name'])}</b>"
                         f"{' · ' + html.escape(', '.join(branch['papers'][:12])) if branch['papers'] else ''}</summary>"
                         f"<p>{html.escape(branch['summary'])}</p>{tree(branch['children']) if branch['children'] else ''}</details></li>")
        parts.append("</ul>")
        return "".join(parts)

    legend = "".join(f'<span class="chip"><i style="background:{color[g]}"></i>{html.escape(g)}</span>' for g in groups)
    stages = "".join(f"<li><b>{html.escape(s['period'])} · {html.escape(s['stage'])}</b>：{html.escape(s['summary'])}</li>"
                     for s in report["lineage"])
    keywords = "".join(f'<span class="kw">{html.escape(k)}</span>' for k in report["keywords"])
    return f"""<!doctype html>
<html lang="{'zh-CN' if lang == 'zh' else 'en'}"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{html.escape(L['atlas'])} · {html.escape(collection['name'])}</title>
<style>
:root {{--bg:#ffffff;--fg:#1e293b;--muted:#64748b;--line:#cbd5e1;--lane0:#f8fafc;--lane1:#f1f5f9;--accent:#2563eb;}}
@media (prefers-color-scheme: dark) {{:root {{--bg:#0f172a;--fg:#e2e8f0;--muted:#94a3b8;--line:#475569;--lane0:#111c33;--lane1:#16223b;--accent:#60a5fa;}}}}
body {{margin:0;padding:24px 16px;background:var(--bg);color:var(--fg);font:14px/1.6 "Microsoft YaHei UI","Segoe UI",sans-serif;}}
main {{max-width:1120px;margin:0 auto;}} h1 {{font-size:22px;margin:0 0 4px;}} h2 {{font-size:17px;margin:26px 0 8px;}}
.muted {{color:var(--muted);}} a {{color:var(--accent);}} svg {{width:100%;height:auto;display:block;}}
.lane0 {{fill:var(--lane0);}} .lane1 {{fill:var(--lane1);}} .grid {{stroke:var(--line);stroke-dasharray:2 4;}}
.axis,.code {{fill:var(--muted);font-size:11px;}} .lanelabel {{font-size:12px;font-weight:600;}}
.cite {{stroke:var(--muted);stroke-width:1;opacity:.55;}} .inherit {{stroke:var(--accent);stroke-width:1.6;stroke-dasharray:5 3;}}
.arrowhead {{fill:var(--muted);}} circle {{stroke:var(--bg);stroke-width:1.5;cursor:pointer;}}
.chip {{display:inline-flex;align-items:center;gap:5px;margin:0 12px 6px 0;font-size:12px;}} .chip i {{width:10px;height:10px;border-radius:50%;display:inline-block;}}
.kw {{display:inline-block;padding:1px 8px;margin:0 6px 6px 0;border:1px solid var(--line);border-radius:10px;font-size:12px;}}
ul {{padding-left:20px;}} details p {{margin:2px 0 6px;color:var(--muted);}} .wrap {{overflow-x:auto;}}
</style></head><body><main>
<h1>{html.escape(L['atlas'])}：{html.escape(collection['name'])}</h1>
<div class="muted">{html.escape(collection['path'].replace('/', ' / '))} ｜ <a href="{html.escape(report_file)}">{html.escape(L['title'])}（Markdown）</a>
 ｜ <a href="zotero://select/library/collections/{html.escape(collection['key'])}">{html.escape(L['zotero'])}</a></div>
<p>{html.escape(report['overview'])}</p><div>{keywords}</div>
<h2>{html.escape(L['lineage'])}</h2>
<div class="muted">{'横轴为发表年份，每一行是一个子方向或子分类；灰色箭头为论文之间的引用关系（来自 OpenAlex），蓝色虚线为综述梳理出的继承与发展关系。点击圆点在 Zotero 中打开论文。' if lang == 'zh' else 'Horizontal axis: year; rows: directions or sub-collections; grey arrows: citations (OpenAlex); blue dashes: inheritance links from the review. Click a dot to open the paper in Zotero.'}</div>
<div>{legend}</div><div class="wrap">{''.join(svg)}</div>
<ol>{stages}</ol>
<h2>{html.escape(L['atlas'])}</h2>{tree(report['branches'])}
</main></body></html>
"""
