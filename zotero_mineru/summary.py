"""Built-in, model-independent paper summary workflow.

Workflow (the same for every provider):
1. Clean the converted Markdown: drop embedded images and the reference list, which carry no content for a summary.
2. Deep reading (default): the model reads the complete paper and writes the card, then reads the complete paper again
   to check the draft item by item, correcting errors and filling omissions. Quick reading skips the check.
   Only a paper longer than the model's context window is read part by part in order, carrying the previous part's
   notes forward (zotero-AI-Butler's "last round" strategy), and the card is written and checked from those notes.
3. Check the answer has every required heading; if not, ask the model once to reformat its own draft.
4. Render the card ourselves (YAML front matter from Zotero metadata + fixed headings), so the file looks the same
   whichever model wrote it.
"""
from __future__ import annotations

import base64
import html
import json
import re
from datetime import date
from pathlib import Path

from .clients import ServiceError
from .llm import ContextTooLong


class Cancelled(Exception):
    """The user stopped this summary; the workflow makes no further model calls."""


SECTIONS = {
    "zh": ["研究背景", "研究目标", "研究对象", "研究方法", "模型与理论", "研究内容", "研究结论"],
    "en": ["Background", "Objectives", "Research Object", "Methods", "Models and Theory", "Research Content", "Conclusions"],
}
LABELS = {"zh": ("一句话总结", "关键词"), "en": ("One-sentence summary", "Keywords")}
GUIDE = {
    "zh": [
        "研究背景：领域现状、存在的问题或研究空白（3–5 句）。",
        "研究目标：本文要解决的具体科学问题或工程问题。",
        "研究对象：材料、结构、系统或数据集，写出关键参数、尺寸、工况和边界条件。",
        "研究方法：试验、数值模拟、理论推导等，写出方法名称、软件、设备和关键设置。",
        "模型与理论：本文构建或采用的模型、理论或算法，写出核心假设、控制方程或关键公式（LaTeX），以及与已有模型的区别；"
        "没有构建新模型时写清所依据的理论。",
        "研究内容：按逻辑顺序列出完成的主要研究工作（“- ”列表，3–8 条）。",
        "研究结论：主要结论和关键定量结果（“- ”列表，写出数值和单位），以及作者指出的局限或展望。",
    ],
    "en": [
        "Background: state of the field and the problem or gap addressed (3–5 sentences).",
        "Objectives: the specific scientific or engineering question this paper answers.",
        "Research Object: materials, structures, systems or datasets, with key parameters, dimensions and conditions.",
        "Methods: experiments, simulations or derivations, naming methods, software, equipment and key settings.",
        "Models and Theory: models, theories or algorithms built or used, with core assumptions and governing equations "
        "(LaTeX) and how they differ from earlier work.",
        "Research Content: the main pieces of work in logical order (\"- \" list, 3–8 items).",
        "Conclusions: main findings with key quantitative results (\"- \" list, with values and units), plus stated limitations.",
    ],
}
SYSTEM = {
    "zh": """你是一名严谨的科研文献分析专家。你将阅读一篇学术论文（由 PDF 转换得到的 Markdown，可能含少量识别错误、HTML 表格和 LaTeX 公式），为它撰写一张结构化的“论文内容卡片”。卡片会保存在文献库中，供研究者和 AI 检索助手在不读全文的情况下快速判断论文是否与某个研究问题相关，因此必须准确、具体、信息密度高。

必须遵守：
1. 只依据论文内容写作，不编造、不臆测；论文没有说明的写“原文未明确说明”。
2. 写具体信息而不是空泛描述：具体的研究对象及参数、方法和软件名称、模型核心方程或假设、试验或数值设置、关键定量结果（数值和单位）。
3. 专业术语首次出现时写作“中文术语（English term）”；公式用 LaTeX，行内 $...$，独立公式 $$...$$。
4. 严格按照给定模板输出：标题文字、顺序和级别不得改动；不要用代码块包裹输出；不要写开场白、解释或结束语。""",
    "en": """You are a meticulous research analyst. You will read an academic paper (Markdown converted from PDF, possibly with minor OCR errors, HTML tables and LaTeX) and write a structured "paper card". The card is stored in a reference library so researchers and AI retrieval agents can judge relevance without reading the full text, so it must be accurate, specific and dense with information.

Rules:
1. Use only the paper; never invent. Write "Not stated in the paper" when information is missing.
2. Be specific: objects and parameters, method and software names, core equations or assumptions, experimental or numerical settings, key quantitative results with units.
3. Write formulas in LaTeX: inline $...$, display $$...$$.
4. Follow the template exactly: keep every heading's wording, order and level; no code fences; no preamble or closing remarks.""",
}
NOTES_PROMPT = {
    "zh": """现在逐段精读论文《{title}》。下面是全文按原文顺序划分的第 {index}/{total} 部分。这一步只写精读笔记，读完全部内容后会根据所有笔记撰写论文卡片，所以笔记要完整、细致，不要粗略概括。
{previous}
请逐句精读这一部分，用简体中文记录其中的全部要点，按需要包括：
- 研究背景、已有研究的不足、本文要解决的问题；
- 研究目标与创新点；
- 研究对象：材料、结构、试件或数据集，及其尺寸、参数、工况、边界条件；
- 研究方法：试验方案与设备、数值方法与软件、网格/步长等关键设置、理论推导思路；
- 模型与理论：定义、基本假设、控制方程和关键公式（LaTeX 原样保留并说明符号含义）、本构关系、参数取值和标定方法；
- 研究内容与结果：每项分析或试验做了什么、得到什么，图表反映的规律，所有关键数值（带单位）；
- 结论、局限与展望。
要求：用“- ”条目分层列出，保留具体名称、公式和数值；只记录本部分实际出现的内容，不编造；不要重复前一部分笔记已经记录的内容；不要输出笔记以外的文字。

<part>
{text}
</part>""",
    "en": """You are now deep-reading the paper "{title}" section by section. Below is part {index}/{total} of the full text, in original order. This step only writes close-reading notes; the paper card will be written from all notes once the whole paper has been read, so the notes must be complete and detailed rather than a rough gist.
{previous}
Read this part sentence by sentence and record, in English, every key point it contains, as applicable:
- background, gaps in earlier work, the problem addressed;
- objectives and novelty;
- research object: materials, structures, specimens or datasets with dimensions, parameters, loading and boundary conditions;
- methods: experimental design and equipment, numerical methods and software, key settings (mesh, step size...), derivation strategy;
- models and theory: definitions, assumptions, governing equations and key formulas (keep LaTeX verbatim and explain symbols), constitutive relations, parameter values and calibration;
- content and results: what each analysis or test did and found, trends shown by figures and tables, all key numbers with units;
- conclusions, limitations and outlook.
Use nested "- " bullets, keep concrete names, formulas and numbers, record only what this part actually says, do not repeat the previous part's notes, and output nothing but the notes.

<part>
{text}
</part>""",
}
CHECK = {
    "zh": """下面是你写出的论文卡片初稿，以及{material}。请重新逐节阅读{material_short}，逐项核对初稿：
1. 改正与论文不符、含糊或张冠李戴的内容；
2. 补充遗漏的关键信息，尤其是研究对象参数、方法设置、模型方程和定量结果；
3. 删除论文中没有依据的内容；
4. 保持模板格式完全不变（每个标题都保留），直接输出修订后的完整卡片，不要说明修改了什么；
5. 修订后整张卡片仍不得超过 {limit} 字（中文按字计，英文单词和数字按词计），补充关键信息时相应精简次要表述。

输出模板：
{template}

<draft>
{draft}
</draft>
{body}""",
    "en": """Below is the draft paper card you wrote, plus {material}. Read {material_short} again section by section and check the draft item by item:
1. fix anything inaccurate, vague or attributed to the wrong part;
2. add missing key information, especially object parameters, method settings, model equations and quantitative results;
3. remove anything the paper does not support;
4. keep the template exactly (every heading) and output only the full revised card, without describing the changes;
5. the revised card must still not exceed {limit} words — make room for added facts by trimming minor wording.

Output template:
{template}

<draft>
{draft}
</draft>
{body}""",
}
SEEING = {
    "zh": "论文中的图片已按顺序附在后面，编号与正文中的 [图 N] 标记对应。请结合图片理解试验装置、模型示意、结果曲线和数据表，"
          "把图中反映的关键信息（构型、趋势、数值、对比结论）写进相应部分；图片中可见的内容也算有依据。",
    "en": "The paper's figures are attached after the text, numbered to match the [Figure N] markers. Use them to understand "
          "set-ups, model sketches, result curves and tables, and put what they show (configurations, trends, values, "
          "comparisons) into the relevant parts; content visible in a figure counts as supported.",
}
DEEP_READING = {
    "zh": "阅读要求：请完整、逐节精读全文——引言、方法、模型与公式推导、试验或数值算例、结果与讨论、结论以及附录——不要只看摘要和结论；"
          "卡片的每个部分都要写出论文中的具体参数、方程和数值。",
    "en": "Reading requirement: read the complete paper section by section — introduction, methods, model and derivations, "
          "experiments or numerical examples, results and discussion, conclusions and appendices — not just the abstract "
          "and conclusions; every part of the card must carry the paper's concrete parameters, equations and numbers.",
}
PREVIOUS = {"zh": "\n前一部分的笔记（仅供衔接参考）：\n<previous_notes>\n{notes}\n</previous_notes>\n",
            "en": "\nNotes from the previous part (for continuity only):\n<previous_notes>\n{notes}\n</previous_notes>\n"}
LENGTH = {
    "zh": "篇幅要求：整张卡片（一句话总结、关键词和七个部分合计）不超过 {limit} 字（中文按字计，英文单词和数字按词计），"
          "建议 {low}–{high} 字；优先保留具体参数、方程和数值，删去套话、铺垫和重复内容。",
    "en": "Length: the whole card (summary sentence, keywords and the seven parts together) must not exceed {limit} words; "
          "aim for {low}–{high} words, keeping concrete parameters, equations and numbers and dropping filler and repetition.",
}
COMPRESS = {
    "zh": """下面的论文卡片约 {words} 字，超过了 {limit} 字的上限。请压缩到 {target} 字以内（中文按字计，英文单词和数字按词计）：
保留所有标题和模板格式，保留关键参数、方程和定量结果，删去背景铺垫、重复和次要细节，不要添加新内容，直接输出压缩后的完整卡片。

输出模板：
{template}

<card>
{card}
</card>""",
    "en": """The paper card below has about {words} words, over the {limit}-word limit. Compress it to at most {target} words:
keep every heading and the template, keep key parameters, equations and quantitative results, drop background filler,
repetition and minor details, add nothing new, and output only the full compressed card.

Output template:
{template}

<card>
{card}
</card>""",
}
REPAIR = {
    "zh": "下面是一份论文卡片草稿，它缺少或改动了这些部分：{missing}。请在不改变事实内容的前提下，严格按模板重新整理并完整输出（缺失的部分写“原文未明确说明”）。\n\n模板：\n{template}\n\n草稿：\n{draft}",
    "en": "Below is a draft paper card that is missing or altered these parts: {missing}. Without changing its facts, rewrite it to follow the template exactly and completely (write \"Not stated in the paper\" for missing parts).\n\nTemplate:\n{template}\n\nDraft:\n{draft}",
}

IMAGE_MD = re.compile(r"!\[[^\]\n]*\]\([^)\n]*\)")
IMAGE_HTML = re.compile(r"<img\b[^>]*>", re.IGNORECASE)
HEADING = re.compile(r"^#{1,6}\s+(.+?)\s*#*\s*$", re.MULTILINE)
DROP_HEADING = re.compile(r"^(?:\d+[.\s]*)?(references?|bibliography|参考文献|acknowledge?ments?|致谢)\b", re.IGNORECASE)
CJK = re.compile(r"[　-鿿가-힯＀-￯]")


HAN = re.compile(r"[\u3400-\u9fff\uf900-\ufaff]")
LATIN_WORD = re.compile(r"[A-Za-z0-9]+(?:['’.\-][A-Za-z0-9]+)*")


def count_words(text: str) -> int:
    """Word-style count (as Microsoft Word's 字数): each Chinese character is one, each English word or number is one."""
    return len(HAN.findall(text)) + len(LATIN_WORD.findall(text))


def card_words(card: dict) -> int:
    return count_words(" ".join([card["one_line"], *card["keywords"], *card["sections"].values()]))


def estimate_tokens(text: str) -> int:
    """Rough, provider-neutral estimate: one token per CJK character, ~3.5 characters per token otherwise."""
    cjk = len(CJK.findall(text))
    return cjk + int((len(text) - cjk) / 3.5) + 1


def _shrink(data: bytes, longest: int = 1024) -> tuple[str, bytes, int] | None:
    """Downscale a figure for a vision model; returns (mime, bytes, pixels) or None for unreadable/tiny images."""
    try:
        from PySide6.QtCore import QBuffer, QIODevice, Qt
        from PySide6.QtGui import QImage
    except ImportError:
        return None
    image = QImage()
    if not image.loadFromData(data):
        return None
    pixels = image.width() * image.height()
    if image.width() < 120 or image.height() < 80:
        return None  # logos, icons and inline symbols carry nothing for the summary
    if max(image.width(), image.height()) > longest:
        image = image.scaled(longest, longest, Qt.AspectRatioMode.KeepAspectRatio, Qt.TransformationMode.SmoothTransformation)
    buffer = QBuffer()
    buffer.open(QIODevice.OpenModeFlag.WriteOnly)
    image.convertToFormat(QImage.Format.Format_RGB888).save(buffer, "JPEG", 85)
    return "image/jpeg", bytes(buffer.data()), pixels


def extract_figures(markdown: str, lang: str = "zh", limit: int = 30, base_dir: Path | None = None) -> tuple[str, list[dict]]:
    """Replace each figure by a numbered [图 N] marker and collect the figures (embedded or beside the file) in order."""
    figures, numbers = [], {}
    word = "图" if lang == "zh" else "Figure"

    def load(target: str) -> bytes | None:
        target = target.strip().strip("<>")
        if target.startswith("data:image/") and ";base64," in target:
            try:
                return base64.b64decode(target.split(",", 1)[1])
            except ValueError:
                return None
        if base_dir and not re.match(r"^[a-z]+:", target, re.IGNORECASE):
            path = (base_dir / target).resolve()
            if path.is_relative_to(base_dir.resolve()) and path.is_file() and path.stat().st_size < 30 * 1024 * 1024:
                return path.read_bytes()
        return None

    def figure(target: str) -> str:
        if target in numbers:
            return f"[{word} {numbers[target]}]" if numbers[target] else f"[{'图' if lang == 'zh' else 'figure'}]"
        shrunk = load(target)
        shrunk = _shrink(shrunk) if shrunk and len(figures) < limit else None
        if not shrunk:
            numbers[target] = 0
            return f"[{'图' if lang == 'zh' else 'figure'}]"
        numbers[target] = len(figures) + 1
        figures.append({"number": numbers[target], "label": f"[{word} {numbers[target]}]", "mime": shrunk[0],
                        "data": base64.b64encode(shrunk[1]).decode("ascii")})
        return f"[{word} {numbers[target]}]"

    text = re.sub(r"!\[[^\]\n]*\]\(\s*(<[^>\n]+>|[^)\s]+)[^)\n]*\)", lambda m: figure(m.group(1)), markdown)
    return text, figures


def clean_markdown(text: str) -> str:
    text = IMAGE_MD.sub("[图]", text)
    text = IMAGE_HTML.sub("[图]", text)
    text = re.sub(r"[A-Za-z0-9+/=]{400,}", "", text)  # stray base64
    # Remove the reference list / acknowledgements up to the next heading (appendices after them are kept).
    headings = list(HEADING.finditer(text))
    pieces, cursor = [], 0
    for n, match in enumerate(headings):
        if DROP_HEADING.match(match.group(1).strip("*_ ")) and match.start() > len(text) * 0.3:
            end = headings[n + 1].start() if n + 1 < len(headings) else len(text)
            pieces.append(text[cursor:match.start()])
            cursor = end
    pieces.append(text[cursor:])
    text = "".join(pieces)
    text = re.sub(r"[ \t]+\n", "\n", text)
    return re.sub(r"\n{3,}", "\n\n", text).strip()


def split_parts(text: str, budget: int) -> list[str]:
    """Split at headings (then paragraphs) into pieces of at most `budget` estimated tokens."""
    blocks, start = [], 0
    for match in HEADING.finditer(text):
        if match.start() > start:
            blocks.append(text[start:match.start()])
            start = match.start()
    blocks.append(text[start:])
    pieces = []
    for block in blocks:
        if estimate_tokens(block) <= budget:
            pieces.append(block)
            continue
        for paragraph in re.split(r"\n\s*\n", block):
            while estimate_tokens(paragraph) > budget:
                cut = max(1000, int(len(paragraph) * budget / estimate_tokens(paragraph)))
                pieces.append(paragraph[:cut])
                paragraph = paragraph[cut:]
            pieces.append(paragraph + "\n\n")
    parts, current = [], ""
    for piece in pieces:
        if current and estimate_tokens(current + piece) > budget:
            parts.append(current.strip())
            current = ""
        current += piece
    if current.strip():
        parts.append(current.strip())
    return parts


def template(lang: str) -> str:
    one, keys = LABELS[lang]
    sep = "；" if lang == "zh" else "; "
    lines = [f"{one}：……" if lang == "zh" else f"{one}: ...", f"{keys}：……{sep}……{sep}……" if lang == "zh" else f"{keys}: ...; ...; ...", ""]
    for name in SECTIONS[lang]:
        lines += [f"## {name}", "……" if lang == "zh" else "...", ""]
    return "\n".join(lines).strip()


def card_prompt(meta: dict, lang: str, *, paper: str = "", notes: str = "", max_words: int = 0) -> str:
    info = {"zh": ["标题", "作者", "年份", "期刊/来源"], "en": ["Title", "Authors", "Year", "Journal/Source"]}[lang]
    values = [meta.get("title", ""), "; ".join(meta.get("authors", [])[:8]), meta.get("year", ""), meta.get("journal", "")]
    lines = (["请阅读下面这篇论文，按模板输出论文内容卡片。输出语言：简体中文。", "", "论文信息（来自 Zotero，供参考）："]
             if lang == "zh" else
             ["Read the paper below and write its paper card following the template. Output language: English.", "",
              "Paper information (from Zotero, for reference):"])
    lines += [f"- {label}：{value or '—'}" if lang == "zh" else f"- {label}: {value or '-'}" for label, value in zip(info, values)]
    lines += ["", DEEP_READING[lang]]
    if max_words:
        lines.append(LENGTH[lang].format(limit=max_words, low=int(max_words * 0.6), high=int(max_words * 0.9)))
    one, keys = LABELS[lang]
    lines += ["", "各部分写作要求：" if lang == "zh" else "What each part must contain:",
              f"- {one}：不超过 80 字，说明用什么方法研究了什么对象的什么问题、得到什么主要结论。" if lang == "zh"
              else f"- {one}: at most 40 words — what was studied, how, and the main finding.",
              f"- {keys}：6–12 个，便于检索，包含研究对象、方法和模型名称，用分号分隔。" if lang == "zh"
              else f"- {keys}: 6–12 search keywords covering object, methods and model names, separated by semicolons."]
    lines += ["- " + line for line in GUIDE[lang]]
    lines += ["", "输出模板（严格照此格式，保留每一个标题）：" if lang == "zh" else "Output template (follow exactly, keep every heading):",
              template(lang), ""]
    if notes:
        lines.append("论文超过模型一次能读的长度，以下是按原文顺序逐段精读全文得到的笔记。请综合全部笔记撰写卡片，不要遗漏其中的关键信息："
                     if lang == "zh" else
                     "The paper is longer than the model can read at once; below are close-reading notes taken part by part in "
                     "original order. Write the card from all of them without dropping key information:")
        lines.append(f"<paper_notes>\n{notes}\n</paper_notes>")
    if paper:
        lines.append(f"<paper>\n{paper}\n</paper>")
    return "\n".join(lines)


def _plain(line: str) -> str:
    """A line without Markdown decoration or numbering, for recognising headings and labels."""
    value = re.sub(r"^[#>\s]+", "", line)
    value = re.sub(r"[*_`]+", "", value)
    value = re.sub(r"^\s*(?:\d+|[一二三四五六七八九十]+)\s*[.、．)）]\s*", "", value)
    return value.strip().rstrip("：:").strip()


def parse_card(text: str, lang: str) -> tuple[dict, list[str]]:
    """Split a model answer into its parts; returns (card, missing parts)."""
    text = re.sub(r"^```[a-zA-Z]*\s*\n|\n```\s*$", "", text.strip())
    one_label, key_label = LABELS[lang]
    label = re.compile(rf"^({re.escape(one_label)}|{re.escape(key_label)})\s*[：:]\s*(.*)$")
    card = {"one_line": "", "keywords": [], "sections": {}}
    current = None
    for line in text.splitlines():
        plain = _plain(line)
        head = label.match(re.sub(r"[*_`]+", "", line).strip(" >#"))
        if head:
            field = "one_line" if head.group(1) == one_label else "keywords"
            if not card[field]:
                value = head.group(2).strip()
                card[field] = value if field == "one_line" else [k.strip() for k in re.split(r"[；;、,，]", value) if k.strip()]
                continue
        is_heading = line.lstrip().startswith("#")
        matched = next((name for name in SECTIONS[lang]
                        if plain == name or (is_heading and plain.startswith(name))), None)
        if matched:
            current = matched
            card["sections"][current] = []
            continue
        if current:
            card["sections"][current].append(line)
    card["sections"] = {k: "\n".join(v).strip() for k, v in card["sections"].items()}
    missing = [name for name in SECTIONS[lang] if not card["sections"].get(name)]
    if not card["one_line"]:
        missing.insert(0, one_label)
    return card, missing


def card_text(card: dict, lang: str) -> str:
    """The card in template form, for sending back to a model."""
    one, keys = LABELS[lang]
    sep = "：" if lang == "zh" else ": "
    lines = [f"{one}{sep}{card['one_line']}", f"{keys}{sep}{('；' if lang == 'zh' else '; ').join(card['keywords'])}", ""]
    for name in SECTIONS[lang]:
        lines += [f"## {name}", card["sections"].get(name, ""), ""]
    return "\n".join(lines).strip()


def _cut(line: str, room: int) -> str:
    """Longest run of whole sentences of `line` within `room` words."""
    kept = ""
    for sentence in re.split(r"(?<=[。；！？;!?])|(?<=\.)\s", line):
        if count_words(kept + sentence) > room:
            break
        kept += sentence
    return kept.rstrip()


def trim_card(card: dict, limit: int) -> dict:
    """Last resort when a model will not compress: shorten every part proportionally at line/sentence boundaries."""
    total = card_words(card)
    if total <= limit:
        return card
    ratio = limit / total * 0.95
    sections = {}
    for name, text in card["sections"].items():
        budget = max(20, int(count_words(text) * ratio))
        kept, used = [], 0
        lines = text.splitlines()
        for line in lines:
            words = count_words(line)
            if used + words <= budget:
                kept.append(line)
                used += words
                continue
            partial = _cut(line, budget - used)
            if partial:
                kept.append(partial)
            break
        sections[name] = "\n".join(kept).rstrip() + ("……" if kept != lines else "")
    return {**card, "sections": sections}


def fit_card(call, card: dict, lang: str, limit: int, log=lambda message: None) -> dict:
    """Keep the card within `limit` words: ask the model to compress (twice at most), then trim if still too long."""
    if not limit:
        return card
    for _ in range(2):
        words = card_words(card)
        if words <= limit or call is None:
            break
        log(f"总结约 {words} 字，超过 {limit} 字上限，正在请模型压缩…")
        compressed, missing = parse_card(call(COMPRESS[lang].format(words=words, limit=limit, target=int(limit * 0.9),
                                                                     template=template(lang), card=card_text(card, lang))), lang)
        if missing:
            break
        card = compressed
    if card_words(card) > limit:
        log(f"压缩后仍超过 {limit} 字，按比例截短各部分。")
        card = trim_card(card, limit)
    return card


def summarize(client, meta: dict, markdown: str, *, lang: str = "zh", depth: str = "deep", budget: int = 0,
              part_tokens: int = 30000, max_output: int | None = None, max_words: int = 3000,
              vision: bool = False, vision_max: int = 30, base_dir: Path | None = None,
              log=lambda message: None, cancelled=lambda: False,
              step=lambda done, total: None) -> tuple[dict, dict]:
    """Run the workflow; returns (card, usage).

    depth "deep" reads the complete paper, writes the card and checks it against the complete paper again;
    "quick" writes it in one read. `budget` caps a single input in estimated tokens (0 = no cap of our own: the paper
    is split only if the model reports its context window is exceeded). `max_output` None leaves output unlimited.
    With `vision`, the paper's figures are sent along (numbered to match [图 N] markers in the text).
    `cancelled` is checked before every model call; once it returns True, Cancelled is raised.
    `step(calls done, calls planned)` is reported before every model call, for a progress bar.
    """
    figures = []
    if vision:
        markdown, figures = extract_figures(markdown, lang, vision_max, base_dir)
        if figures:
            log(f"已附上论文中的 {len(figures)} 张图片供模型查看。")
    text = clean_markdown(markdown)
    if estimate_tokens(text) < 200:
        raise ServiceError("Markdown 正文过短，无法生成总结。")
    usage = {"input": 0, "output": 0, "calls": 0}
    planned = {"calls": 0}
    system = SYSTEM[lang]
    budget = budget or 10 ** 9

    def pictures(fragment: str) -> list[dict]:
        """The figures whose markers appear in this piece of text."""
        return [f for f in figures if f["label"] in fragment]

    def call(prompt, images=None):
        if cancelled():
            raise Cancelled()
        step(usage["calls"], max(planned["calls"], usage["calls"] + 1))
        if images:
            prompt = prompt + "\n\n" + SEEING[lang]
        answer, used = client.chat(system, prompt, max_tokens=max_output or None, images=images or None)
        if images and getattr(client, "sees_images", True):
            usage["images"] = max(usage.get("images", 0), len(images))
        usage["input"] += used["input"]
        usage["output"] += used["output"]
        usage["calls"] += 1
        return answer

    def read_notes(size):
        parts = split_parts(text, size)
        planned["calls"] = usage["calls"] + len(parts) + 1 + (depth == "deep")
        notes, previous = [], ""
        for n, part in enumerate(parts, 1):
            log(f"论文较长，分段精读第 {n}/{len(parts)} 部分…")
            carry = PREVIOUS[lang].format(notes=previous[-4000:]) if previous else ""
            previous = call(NOTES_PROMPT[lang].format(title=meta.get("title", ""), index=n, total=len(parts),
                                                      previous=carry, text=part), pictures(part))
            notes.append((f"### 第 {n} 部分\n" if lang == "zh" else f"### Part {n}\n") + previous)
        return "\n\n".join(notes)

    while True:
        try:
            whole = estimate_tokens(text) <= budget
            planned["calls"] = usage["calls"] + 1 + (depth == "deep")  # segmented reading adds its parts below
            if whole:
                log(f"正在完整阅读全文（约 {estimate_tokens(text)} tokens）并撰写论文卡片…")
                notes, answer = "", call(card_prompt(meta, lang, paper=text, max_words=max_words), figures)
            else:
                notes = read_notes(min(part_tokens, budget))
                log("分段精读完成，正在根据全部笔记撰写论文卡片…")
                answer = call(card_prompt(meta, lang, notes=notes, max_words=max_words))
            card, missing = parse_card(answer, lang)
            if missing:
                log("输出格式不完整（缺少：" + "、".join(missing) + "），请模型按模板重新整理一次。")
                answer = call(REPAIR[lang].format(missing="、".join(missing), template=template(lang), draft=answer))
                card, missing = parse_card(answer, lang)
                if missing:
                    raise ServiceError("大模型输出缺少以下部分：" + "、".join(missing) + "。可换用更强的模型后重试。", retryable=True)
            if depth == "deep":
                log("正在重新阅读" + ("全文" if whole else "全部笔记") + "，逐项核对、补全论文卡片…")
                zh = lang == "zh"
                checked = call(CHECK[lang].format(
                    material=("论文全文" if zh else "the complete paper") if whole else ("逐段精读笔记" if zh else "the close-reading notes"),
                    material_short=("全文" if zh else "the paper") if whole else ("笔记" if zh else "the notes"),
                    template=template(lang), draft=answer, limit=max_words or 100000,
                    body=f"\n<paper>\n{text}\n</paper>" if whole else f"\n<paper_notes>\n{notes}\n</paper_notes>"),
                    figures if whole else None)
                revised, missing = parse_card(checked, lang)
                if missing:
                    log("核对稿格式不完整，保留初稿。")
                else:
                    card = revised
            if figures and not getattr(client, "sees_images", True):
                log("该模型不支持看图，已自动改为只读文字。")
                usage["images"] = 0
            return fit_card(call, card, lang, max_words, log), usage
        except ContextTooLong:
            size = min(budget, estimate_tokens(text))
            if size <= 4000:
                raise
            budget = size // 2
            log(f"论文超过模型上下文长度，改为每段约 {min(part_tokens, budget)} tokens 分段精读后重试。")


def _yaml(value) -> str:
    return json.dumps(value, ensure_ascii=False)


def render_card(meta: dict, card: dict, lang: str, model: str) -> str:
    one_label, key_label = LABELS[lang]
    front = {
        "title": meta.get("title", ""), "authors": meta.get("authors", []), "year": meta.get("year", ""),
        "journal": meta.get("journal", ""), "doi": meta.get("doi", ""), "zotero_key": meta.get("key", ""),
        "zotero_link": f"zotero://select/library/items/{meta.get('key', '')}",
        "pdf_file": meta.get("pdf_name", ""), "pdf_zotero_key": meta.get("pdf_key", ""),
        "pdf_link": f"zotero://open-pdf/library/items/{meta['pdf_key']}" if meta.get("pdf_key") else "",
        "fulltext_markdown": meta.get("markdown_name", ""), "markdown_zotero_key": meta.get("markdown_key", ""),
        "markdown_link": f"zotero://select/library/items/{meta['markdown_key']}" if meta.get("markdown_key") else "",
        "keywords": card["keywords"], "summary": card["one_line"], "word_count": card_words(card),
        "figures_viewed": meta.get("figures_viewed", 0), "generated_by": model, "generated_at": date.today().isoformat(),
    }
    lines = ["---", *[f"{k}: {_yaml(v)}" for k, v in front.items()], "---", ""]
    lines.append(("# 论文总结：" if lang == "zh" else "# Paper card: ") + meta.get("title", ""))
    lines.append("")
    info = [("作者" if lang == "zh" else "Authors", "; ".join(meta.get("authors", [])[:10])),
            ("年份" if lang == "zh" else "Year", meta.get("year", "")),
            ("来源" if lang == "zh" else "Source", meta.get("journal", ""))]
    lines.append(" ｜ ".join(f"**{k}**：{v}" if lang == "zh" else f"**{k}**: {v}" for k, v in info if v))
    links = association_links(meta, lang)
    if links:
        label = "**关联**：" if lang == "zh" else "**Linked**: "
        lines += ["", label + " ｜ ".join(f"[{text}]({target})" for text, target in links)]
    lines += ["", f"> **{one_label}**：{card['one_line']}" if lang == "zh" else f"> **{one_label}**: {card['one_line']}", ""]
    sep = "；" if lang == "zh" else "; "
    lines += [f"**{key_label}**：{sep.join(card['keywords'])}" if lang == "zh" else f"**{key_label}**: {sep.join(card['keywords'])}", ""]
    for name in SECTIONS[lang]:
        lines += [f"## {name}", "", card["sections"].get(name, ""), ""]
    return "\n".join(lines).rstrip() + "\n"


def association_links(meta: dict, lang: str) -> list[tuple[str, str]]:
    """Links from the card to its Zotero item, the paper PDF and the full-text Markdown."""
    zh = lang == "zh"
    links = []
    if meta.get("key"):
        links.append(("Zotero 条目" if zh else "Zotero item", f"zotero://select/library/items/{meta['key']}"))
    if meta.get("pdf_key"):
        links.append(("论文 PDF" if zh else "Paper PDF", f"zotero://open-pdf/library/items/{meta['pdf_key']}"))
    if meta.get("markdown_rel"):
        links.append(("全文 Markdown" if zh else "Full-text Markdown", f"<{meta['markdown_rel']}>"))
    elif meta.get("markdown_key"):
        links.append(("全文 Markdown" if zh else "Full-text Markdown", f"zotero://select/library/items/{meta['markdown_key']}"))
    return links


def note_html(meta: dict, card: dict, lang: str) -> str:
    """Zotero child-note version of the card (Zotero notes are HTML)."""
    def inline(value: str) -> str:
        value = html.escape(value)
        return re.sub(r"\*\*(.+?)\*\*", r"<strong>\1</strong>", value)

    one_label, key_label = LABELS[lang]
    parts = [f"<h1>{'AI 论文总结' if lang == 'zh' else 'AI paper card'}：{html.escape(meta.get('title', ''))}</h1>",
             f"<p><strong>{one_label}</strong>：{inline(card['one_line'])}</p>",
             *([f"<p><strong>{'关联' if lang == 'zh' else 'Linked'}</strong>："
                + " ｜ ".join(f'<a href="{html.escape(target if not target.startswith("<") else "zotero://select/library/items/" + meta.get("markdown_key", ""))}">{html.escape(text)}</a>'
                             for text, target in association_links(meta, lang)) + "</p>"] if meta.get("key") else []),
             f"<p><strong>{key_label}</strong>：{inline('；'.join(card['keywords']))}</p>"]
    for name in SECTIONS[lang]:
        parts.append(f"<h2>{html.escape(name)}</h2>")
        items, paragraph = [], []
        for line in card["sections"].get(name, "").splitlines() + [""]:
            bullet = re.match(r"^\s*[-*•]\s+(.*)$", line)
            if bullet:
                items.append(f"<li>{inline(bullet.group(1))}</li>")
                continue
            if items:
                parts.append("<ul>" + "".join(items) + "</ul>")
                items = []
            if line.strip():
                paragraph.append(inline(line.strip()))
            elif paragraph:
                parts.append("<p>" + "<br/>".join(paragraph) + "</p>")
                paragraph = []
    return "<div data-schema-version=\"9\">" + "".join(parts) + "</div>"
