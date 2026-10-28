"""Tracking newly published papers close to a collection.

Candidates come from three routes:
- citing: new papers that cite the collection's papers (OpenAlex; Semantic Scholar's citation lists too with a key);
- similar: Semantic Scholar recommendations seeded with the collection's papers;
- search: OpenAlex keyword search with the queries the collection review proposed (needs an OpenAlex key).
They are filtered (publication window, already in the library, already reported), gated by SPECTER2 similarity to the
collection's own papers with a self-calibrated threshold, ranked and diversified, and finally the model reads titles
and abstracts and scores each against the collection's research profile (as in ArxivDigest), giving its reasons.
"""
from __future__ import annotations

import json
import math
import re
from datetime import date

from .clients import ServiceError
from .scholar import (SEED_CITES_MAX, center, from_openalex, from_s2, idf_weight, max_similarity, mean_vector, mmr_select,
                      normalize_title, self_threshold, unit)

ROUTES = {"citing": "引用了本分类论文", "similar": "Semantic Scholar 相似推荐", "search": "OpenAlex 关键词检索"}
# The similarity gate needs enough vectors to estimate the background and calibrate its threshold.
GATE_MIN_PAPERS, GATE_MIN_CANDIDATES = 8, 20
SYSTEM = ("你是一名科研文献筛选助手，负责判断新发表的论文与研究者某个研究方向的相关程度。只依据给出的标题、期刊、摘要和引用线索判断，"
          "不编造论文内容。只输出 JSON。")
SCORE = """研究方向：文献分类“{path}”
方向描述：{profile}
关键词：{keywords}
本分类的代表论文：
{examples}

下面是 {count} 篇新发表的候选论文。请逐篇按 0–10 分评价它与这个研究方向的相关程度：
- 9–10：研究对象和方法都与本方向高度一致，研究者很可能需要阅读；
- 7–8：研究对象或方法明显相关；
- 4–6：只有部分相关（例如方法相同但研究对象不同）；
- 0–3：基本无关。
“引用了本分类论文”只说明存在联系，仍要按内容判断相关程度。

输出一个 JSON 数组，每篇一项，字段名保持英文：
[{{"id": "T1", "score": 8, "reason": "用{language}一句话说明它与本方向哪部分相关", "summary": "用{language}一句话介绍这篇论文做了什么（没有摘要时根据标题谨慎概括）"}}]

候选论文：
{candidates}"""


def candidate_key(paper: dict) -> str:
    if paper.get("doi"):
        return "doi:" + paper["doi"]
    if paper.get("s2_id"):
        return "s2:" + paper["s2_id"]
    if paper.get("openalex_id"):
        return "oa:" + paper["openalex_id"]
    return "t:" + normalize_title(paper.get("title"))


def merge(pool: dict, paper: dict, route: str, cites=()):
    key = candidate_key(paper)
    title_key = "t:" + normalize_title(paper.get("title"))
    existing = pool.get(key) or pool.get(title_key)
    if existing is None:
        existing = dict(paper, routes=[], cites=[])
        pool[key] = existing
        pool.setdefault(title_key, existing)
    else:
        for field, value in paper.items():
            if value and not existing.get(field):
                existing[field] = value
    if route not in existing["routes"]:
        existing["routes"].append(route)
    for code in cites:
        if code not in existing["cites"]:
            existing["cites"].append(code)


def parse_scores(text: str) -> list[dict]:
    fenced = re.search(r"```(?:json)?\s*(.*?)```", text, re.DOTALL)
    if fenced:
        text = fenced.group(1)
    start, end = text.find("["), text.rfind("]")
    if start < 0 or end <= start:
        raise ValueError("没有找到 JSON 数组")
    body = text[start:end + 1]
    try:
        rows = json.loads(body)
    except json.JSONDecodeError:
        rows = json.loads(re.sub(r",\s*([}\]])", r"\1", body))
    return [r for r in rows if isinstance(r, dict)]


def _year_ok(paper: dict, since: str) -> bool:
    when = paper.get("date") or ""
    return bool(when) and when[:len(since)] >= since if len(when) >= 10 else bool(when) and when[:4] >= since[:4]


def track(scholar, seeds: list[dict], *, path: str, profile: dict, since: str, library_dois: set[str],
          library_titles: set[str], seen: set[str], cache: dict, llm_call, lang: str = "zh", budget: int = 15,
          pool_size: int = 30, min_score: int = 6, keyword_search: bool = False, log=lambda message: None) -> dict:
    """Find, filter and rank new papers for one collection; returns items, statistics, notes and the updated cache.

    seeds: the collection's papers {"key", "code", "label", "title", "doi", "openalex_id", "openalex_count"};
    cache: {"s2": {paper key: {"id", "count", "citers": [...]}}} carried between runs, so unchanged seeds are not
    fetched again; llm_call(system, prompt) -> text.

    Citing papers come from OpenAlex (one request covers up to 50 seeds); Semantic Scholar's per-paper citation
    lists are added only with a Semantic Scholar key, since without one its shared pool is often too slow for that.
    """
    notes: list[str] = []
    stats = {"seeds": len(seeds), "candidates": 0, "after_filters": 0, "gate_passed": 0, "reviewed": 0, "recommended": 0}
    s2_cache = cache.setdefault("s2", {})

    # ---- resolve the collection's papers on Semantic Scholar (ids, citation counts, SPECTER2 vectors)
    lookups, owners = [], []
    for seed in seeds:
        known = s2_cache.get(seed["key"], {}).get("id")
        if seed.get("doi") or known:
            lookups.append(known or "DOI:" + seed["doi"])
            owners.append(seed)
    vectors: dict[str, list[float]] = {}
    counts: dict[str, int] = {s["key"]: s["openalex_count"] for s in seeds if s.get("openalex_count") is not None}
    resolved = set()
    if lookups:
        log(f"正在读取本分类 {len(lookups)} 篇论文在 Semantic Scholar 中的语义向量…")
        try:
            rows = scholar.s2_papers(lookups, "paperId,citationCount,embedding.specter_v2")
        except ServiceError as error:
            rows = [None] * len(lookups)
            notes.append("Semantic Scholar 暂不可用：" + str(error))
        for seed, row in zip(owners, rows):
            if not row:
                continue
            entry = s2_cache.setdefault(seed["key"], {})
            entry["id"] = row.get("paperId", "")
            resolved.add(seed["key"])
            counts[seed["key"]] = row.get("citationCount") or 0
            vector = (row.get("embedding") or {}).get("vector")
            if vector:
                vectors[seed["key"]] = vector
    if getattr(scholar, "s2_down", False):
        notes.append("Semantic Scholar 公共额度拥挤，本次只用 OpenAlex 找引用论文、未做语义门槛（填写免费的 Semantic Scholar Key 可改善）。")
    unusable = sum(1 for s in seeds if not s.get("doi") and not s.get("openalex_id") and s["key"] not in resolved)
    if unusable:
        notes.append(f"{unusable} 篇论文没有 DOI，未作为追踪种子。")

    pool: dict[str, dict] = {}
    # ---- route 1: papers citing the collection; classics are not seeds (their citers are cross-domain noise)
    classics = [s for s in seeds if counts.get(s["key"], 0) > SEED_CITES_MAX]
    if classics:
        notes.append(f"{len(classics)} 篇被引超过 {SEED_CITES_MAX} 次的经典论文不作为引用种子（引用它们的论文跨领域、噪音大）。")
    eligible = [s for s in seeds if s["key"] in resolved and 0 < counts.get(s["key"], 0) <= SEED_CITES_MAX] \
        if getattr(scholar, "s2_fast", True) else []
    fetched = 0
    for n, seed in enumerate(eligible, 1):
        entry = s2_cache[seed["key"]]
        if entry.get("count") != counts[seed["key"]] or "citers" not in entry:
            log(f"正在读取引用本分类论文的新论文（{n}/{len(eligible)}）…")
            try:
                entry["citers"] = [from_s2(row) for row in scholar.s2_citations(entry["id"])]
                entry["count"] = counts[seed["key"]]
                fetched += 1
            except ServiceError as error:
                notes.append(f"读取 {seed['label']} 的引用文献失败：{error}")
                continue
        for citer in entry.get("citers", []):
            merge(pool, citer, "citing", [seed["code"]])
    by_openalex = {s["openalex_id"]: s for s in seeds if s.get("openalex_id") and counts.get(s["key"], 1) <= SEED_CITES_MAX}
    ids = list(by_openalex)
    if ids:
        log(f"正在用 OpenAlex 查找引用本分类 {len(ids)} 篇论文的新论文…")
    for start in range(0, len(ids), 50):
        chunk = ids[start:start + 50]
        try:
            rows = scholar.openalex_works(filter=f"cites:{'|'.join(chunk)},from_publication_date:{since}", pages=2)
        except ServiceError as error:
            notes.append("OpenAlex 引用检索失败：" + str(error))
            break
        for row in rows:
            cited = [by_openalex[r.rsplit("/", 1)[-1]]["code"] for r in row.get("referenced_works") or []
                     if r.rsplit("/", 1)[-1] in by_openalex]
            merge(pool, from_openalex(row), "citing", cited)

    # ---- route 2: similar new papers
    positive = [s2_cache[s["key"]]["id"] for s in sorted(seeds, key=lambda s: s.get("year") or "", reverse=True)
                if s2_cache.get(s["key"], {}).get("id") and s["key"] in counts]
    if positive:
        log("正在获取 Semantic Scholar 的相似新论文推荐…")
        try:
            for row in scholar.s2_recommend(positive[:100], limit=100):
                merge(pool, from_s2(row), "similar")
        except ServiceError as error:
            notes.append("Semantic Scholar 推荐暂不可用：" + str(error))

    # ---- route 3: keyword search
    if keyword_search:
        for query in profile.get("queries", [])[:3]:
            try:
                for row in scholar.openalex_works(filter=f"from_publication_date:{since}", search=query, per_page=25):
                    merge(pool, from_openalex(row), "search")
            except ServiceError as error:
                notes.append("OpenAlex 关键词检索失败：" + str(error))
                break

    candidates = list({id(p): p for p in pool.values()}.values())
    stats["candidates"] = len(candidates)
    kept = [c for c in candidates
            if c.get("title") and _year_ok(c, since)
            and not (c.get("doi") and c["doi"] in library_dois) and normalize_title(c["title"]) not in library_titles
            and candidate_key(c) not in seen]
    stats["after_filters"] = len(kept)
    if not kept:
        return {"items": [], "stats": stats, "notes": notes, "cache": cache, "reviewed": []}

    # ---- relevance gate: SPECTER2 similarity to the closest of the collection's own papers
    kept.sort(key=lambda c: (len(c["cites"]), c.get("date") or ""), reverse=True)
    # Without a key the batch endpoint is usually throttled and each vector costs one paced request.
    limit = 0 if getattr(scholar, "s2_down", False) else 300 if scholar.batch_ok else 20
    want = [c for c in kept[:limit] if c.get("s2_id") or c.get("doi")]
    if want and vectors:
        log(f"正在比较 {len(want)} 篇候选论文与本分类论文的语义相似度…")
        try:
            rows = scholar.s2_papers([c["s2_id"] or "DOI:" + c["doi"] for c in want], "paperId,embedding.specter_v2")
        except ServiceError as error:
            rows = [None] * len(want)
            notes.append("候选论文的语义向量读取失败：" + str(error))
        for candidate, row in zip(want, rows):
            vector = ((row or {}).get("embedding") or {}).get("vector")
            if vector:
                candidate["vector"] = vector
    references = [vectors[s["key"]] for s in seeds if s["key"] in vectors and counts.get(s["key"], 0) <= SEED_CITES_MAX]
    with_vectors = [c for c in kept if c.get("vector")]
    threshold = None
    if len(references) >= GATE_MIN_PAPERS and len(with_vectors) >= GATE_MIN_CANDIDATES:
        background = mean_vector([unit(v) for v in references] + [unit(c["vector"]) for c in with_vectors])
        centred = [center(v, background) for v in references]
        threshold = self_threshold(centred)
        for candidate in with_vectors:
            candidate["similarity"] = max_similarity(center(candidate["vector"], background), centred)
    elif references:
        # Too few vectors to estimate a background and calibrate a threshold (a 5-paper collection rejected a
        # clearly relevant paper that way): rank by plain similarity and leave the verdict to the model.
        units = [unit(v) for v in references]
        for candidate in with_vectors:
            candidate["similarity"] = max_similarity(unit(candidate["vector"]), units)
    gated = [c for c in kept if threshold is None or c.get("similarity") is None or c["similarity"] >= threshold]
    stats["gate_passed"] = len(gated)
    stats["threshold"] = round(threshold, 3) if threshold is not None else None

    # ---- rank: citation evidence weighted by rarity, plus similarity; then diversify
    seed_counts = {s["code"]: counts.get(s["key"], 0) for s in seeds}
    for candidate in gated:
        candidate["weight"] = sum(idf_weight(seed_counts.get(code, 0)) for code in candidate["cites"])
    w_max = max((c["weight"] for c in gated), default=0) or 1e-9
    s_max = max((c.get("similarity") or 0 for c in gated), default=0) or 1e-9
    for candidate in gated:
        similarity = candidate.get("similarity")
        proxy = similarity / s_max if similarity is not None else (0.5 if "similar" in candidate["routes"] else 0.3)
        candidate["prior"] = 0.65 * candidate["weight"] / w_max + 0.35 * max(0.0, proxy)
    citing = sorted([c for c in gated if c["cites"]], key=lambda c: c["prior"], reverse=True)[:150]
    others = sorted([c for c in gated if not c["cites"]], key=lambda c: c["prior"], reverse=True)[:150]

    def pick(rows, count):
        order = mmr_select([r["prior"] for r in rows], [unit(r["vector"]) if r.get("vector") else None for r in rows], count)
        return [rows[i] for i in order]

    first = pick(citing, math.ceil(pool_size * 2 / 3))
    second = pick(others, pool_size - len(first))
    chosen = first + second
    if len(chosen) < pool_size:
        rest = [c for c in citing + others if c not in chosen]
        chosen += rest[:pool_size - len(chosen)]

    # ---- the model reads titles and abstracts and scores each against the collection's profile
    labels = {s["code"]: s["label"] for s in seeds}
    examples = "\n".join(f"- {s['code']} {s['label']}：{s['title']}" for s in seeds[:12])
    scored: dict[int, dict] = {}
    for start in range(0, len(chosen), 10):
        batch = chosen[start:start + 10]
        log(f"正在请大模型评审候选论文的相关性（{start + 1}–{start + len(batch)} / {len(chosen)}）…")
        blocks = []
        for n, candidate in enumerate(batch, start + 1):
            clues = []
            if candidate["cites"]:
                clues.append("引用了本分类 " + "、".join(f"{c}（{labels.get(c, '')}）" for c in candidate["cites"][:6]))
            clues += [ROUTES[r] for r in candidate["routes"] if r != "citing"]
            abstract = (candidate.get("abstract") or "（无摘要）")[:1200]
            blocks.append(f"[T{n}] {candidate.get('date', '')[:7]} · {candidate.get('venue') or '来源不详'}\n"
                          f"标题：{candidate['title']}\n线索：{'；'.join(clues)}\n摘要：{abstract}")
        prompt = SCORE.format(path=path, profile=profile.get("profile") or "（暂无综述，根据代表论文判断）",
                              keywords="；".join(profile.get("keywords", [])) or "—", examples=examples,
                              count=len(batch), language="简体中文" if lang == "zh" else "English",
                              candidates="\n\n".join(blocks))
        try:
            rows = parse_scores(llm_call(SYSTEM, prompt))
        except ValueError:
            notes.append("大模型有一批评审结果不是合法 JSON，这一批未评分。")
            continue
        for row in rows:
            match = re.fullmatch(r"T(\d+)", str(row.get("id", "")).strip())
            if not match or not start < int(match.group(1)) <= start + len(batch):
                continue
            try:
                score = max(0.0, min(10.0, float(row.get("score"))))
            except (TypeError, ValueError):
                continue
            scored[int(match.group(1)) - 1] = {"score": score, "reason": str(row.get("reason", "")).strip(),
                                                "summary": str(row.get("summary", "")).strip()}
    stats["reviewed"] = len(scored)
    items = []
    for index, verdict in scored.items():
        candidate = chosen[index]
        if verdict["score"] < min_score:
            continue
        items.append({"key": candidate_key(candidate), "title": candidate["title"], "date": candidate.get("date", ""),
                      "venue": candidate.get("venue", ""), "doi": candidate.get("doi", ""), "url": candidate.get("url", ""),
                      "abstract": (candidate.get("abstract") or "")[:800], "score": verdict["score"],
                      "reason": verdict["reason"], "summary": verdict["summary"], "routes": candidate["routes"],
                      "cites": [f"{c}（{labels.get(c, '')}）" for c in candidate["cites"]],
                      "similarity": round(candidate["similarity"], 3) if candidate.get("similarity") is not None else None,
                      "prior": round(candidate["prior"], 3)})
    items.sort(key=lambda i: (i["score"], i["prior"]), reverse=True)
    items = items[:budget]
    stats["recommended"] = len(items)
    stats["fetched_seeds"] = fetched
    return {"items": items, "stats": stats, "notes": notes, "cache": cache,
            "reviewed": [candidate_key(chosen[i]) for i in scored]}


def render_tracking(collection: dict, runs: list[dict], *, lang: str, review_file: str = "", children: list[tuple] = ()) -> str:
    """The tracking report: newest run first; a parent lists its sub-collections' latest recommendations."""
    zh = lang == "zh"
    lines = ["---", f"collection: {json.dumps(collection['name'], ensure_ascii=False)}",
             f"collection_key: {json.dumps(collection['key'])}", f"path: {json.dumps(collection['path'], ensure_ascii=False)}",
             f"updated_at: {json.dumps(date.today().isoformat())}", "---", "",
             f"# {'新文献追踪' if zh else 'New-paper tracking'}：{collection['name']}", "",
             f"**{'分类路径' if zh else 'Path'}**：{collection['path'].replace('/', ' / ')}", ""]
    related = [("Zotero 分类" if zh else "Zotero collection", f"zotero://select/library/collections/{collection['key']}")]
    if review_file:
        related.insert(0, ("研究综述" if zh else "Review", f"<{review_file}>"))
    lines += [f"**{'关联' if zh else 'Linked'}**：" + " ｜ ".join(f"[{t}]({u})" for t, u in related), ""]
    if zh:
        lines += ["> 追踪方法：以本分类的论文为种子，汇总①引用了这些论文的新论文（OpenAlex；有 Semantic Scholar Key 时加上其引用列表），②Semantic Scholar 的相似论文推荐，"
                  "③（如已填写 OpenAlex Key）按综述生成的检索式检索的新论文；去掉文库中已有和以前推荐过的论文，按与本分类论文的语义相似度"
                  "（SPECTER2，阈值按本分类自身论文自动校准）和引用证据排序，再由大模型阅读标题和摘要逐篇评分（0–10），列出达到阈值的论文。", ""]
    for child_name, child_link, child_items in children:
        if not child_items:
            continue
        title = f"[{child_name}](<{child_link}>)" if child_link else child_name
        lines += [f"## {'子分类' if zh else 'Sub-collection'}：{title}", ""]
        for item in child_items[:8]:
            lines.append(f"- **{item['score']:.0f}/10** [{item['title']}]({item['url']})（{item.get('date', '')[:7]}，"
                         f"{item.get('venue') or '—'}）：{item.get('reason', '')}")
        lines.append("")
    for run in runs:
        stats = run.get("stats", {})
        lines += [f"## {run['date']}（{'检索' if zh else 'window from'} {run['since']} {'之后发表的论文' if zh else ''}）", ""]
        if zh:
            total = len(run.get("items", []))
            lines.append(f"候选 {stats.get('candidates', 0)} 篇 → 去掉已入库和已推荐过的，剩 {stats.get('after_filters', 0)} 篇 → "
                         f"通过语义门槛 {stats.get('gate_passed', 0)} 篇 → 大模型评审 {stats.get('reviewed', 0)} 篇 → "
                         f"推荐 {stats.get('recommended', 0)} 篇" + (f"（连同当天之前的追踪共 {total} 篇）" if total > stats.get("recommended", 0) else "")
                         + ("。" if stats.get("threshold") is not None or not stats.get("gate_passed") else "（论文向量不足，未启用语义门槛）。"))
        else:
            lines.append(f"{stats.get('candidates', 0)} candidates → {stats.get('after_filters', 0)} new → "
                         f"{stats.get('gate_passed', 0)} passed similarity → {stats.get('reviewed', 0)} reviewed → "
                         f"{stats.get('recommended', 0)} recommended.")
        for note in run.get("notes", []):
            lines.append(f"> {note}")
        lines.append("")
        if not run.get("items"):
            lines += ["本次没有发现达到阈值的新论文。" if zh else "No new paper reached the threshold.", ""]
        for n, item in enumerate(run.get("items", []), 1):
            lines += [f"### {n}. {item['title']}", "",
                      f"- **{'相关度' if zh else 'Relevance'}**：{item['score']:.0f}/10" + (f" ｜ {'语义相似度' if zh else 'similarity'} {item['similarity']}"
                                                                                       if item.get("similarity") is not None else ""),
                      f"- **{'发表' if zh else 'Published'}**：{item.get('date') or '—'} ｜ {item.get('venue') or '—'}"
                      + (f" ｜ [DOI {item['doi']}](https://doi.org/{item['doi']})" if item.get("doi") else f" ｜ [{'链接' if zh else 'link'}]({item['url']})"),
                      f"- **{'为什么相关' if zh else 'Why'}**：{item.get('reason', '')}",
                      f"- **{'内容简介' if zh else 'Summary'}**：{item.get('summary', '')}"]
            clues = list(item.get("cites") and ["引用了本分类 " + "、".join(item["cites"])] or [])
            clues += [ROUTES[r] for r in item.get("routes", []) if r != "citing"]
            if clues:
                lines.append(f"- **{'线索' if zh else 'Clues'}**：" + "；".join(clues))
            lines.append("")
    return "\n".join(lines).rstrip() + "\n"
