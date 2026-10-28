"""Layout-preserving PDF translation engine for ZoteroMinerU, run as a separate process.

BabelDOC (AGPL-3.0, https://github.com/funstory-ai/BabelDOC) parses the PDF's own text, keeps formulas, figures and
graphics as they are and re-typesets only the translated paragraphs; the text is translated by the user's own LLM
through zotero_mineru.llm. The result is one PDF whose pages hold the translation on the left and the original on the
right.

    BabelDocWorker --job job.json      translate (API key in the ZMT_LLM_KEY environment variable)
    BabelDocWorker --check             make sure the layout model and fonts are present (downloads them once)

Every line on stdout is one JSON object: {"type": "progress", "percent", "stage"}, then {"type": "done", ...} or
{"type": "error", "message"}. Logs go to stderr.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import logging
import multiprocessing
import os
import re
import shutil
import sys
import threading
import time
import unicodedata
from pathlib import Path

if getattr(sys, "frozen", False):
    sys.path.insert(0, str(Path(sys.executable).parent))
else:
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
# PyMuPDF 2 loads a layout model of its own on import (also in BabelDOC's font-subsetting child process) when the
# optional pymupdf.layout is importable; BabelDOC never uses it, so that import is made to fail as "not installed".
sys.modules["pymupdf.layout"] = None

PROMPT_VERSION = "zmt-1"
STAGES = {  # BabelDOC stage names → what the user sees
    "Parse PDF and Create Intermediate Representation": "解析 PDF", "DetectScannedFile": "检查是否扫描版",
    "Parse Page Layout": "识别版面", "Parse Paragraphs": "识别段落", "Parse Formulas and Styles": "识别公式与样式",
    "Automatic Term Extraction": "提取术语", "Translate Paragraphs": "翻译段落", "Typesetting": "排版",
    "Add Fonts": "嵌入字体", "Generate drawing instructions": "生成页面", "Subset font": "字体子集化",
    "Save PDF": "保存 PDF",
}
# Thinking makes translation slow and expensive without improving it; providers that reject the field get it dropped.
THINKING_OFF = {
    "deepseek": {"thinking": {"type": "disabled"}}, "zhipu": {"thinking": {"type": "disabled"}},
    "qwen": {"enable_thinking": False}, "siliconflow": {"enable_thinking": False},
}
LANGUAGES = {"zh": "Simplified Chinese (简体中文)"}
ERRORS = {  # BabelDOC exceptions → messages for the user; none of them improves by retrying
    "ScannedPDFError": "这是扫描版 PDF（页面是图片，没有文字层），保留排版翻译暂不支持。",
    "ExtractTextError": "无法从这个 PDF 中提取文字（文字层损坏或加密）。",
    "InputFileGeneratedByBabelDOCError": "这个 PDF 本身就是翻译生成的文件，无需再翻译。",
    "ContentFilterError": "大模型服务商的内容审核拒绝了部分文字，请换用其他模型。",
    "contains no paragraphs": "PDF 中没有可翻译的文字（可能是扫描版或只有图片）。",
}
LIST_ENTRY = re.compile(r"\s*(?:\[\d{1,3}\]|\d{1,3}\.)\s+\S")  # "43. Raghu P" or "[43] Raghu P"
REFERENCE = re.compile(r"doi|et al\.|\b(?:19|20)\d{2}[;,)]", re.IGNORECASE)


def emit(kind: str, **data) -> None:
    # ASCII-only JSON: a frozen program ignores PYTHONIOENCODING and writes in the console code page.
    sys.stdout.write(json.dumps({"type": kind, **data}) + "\n")
    sys.stdout.flush()


def system_prompt(lang_out: str, keep_references: bool) -> str:
    language = LANGUAGES.get(lang_out, lang_out)
    prompt = (f"You are a professional translator of scientific and engineering papers into {language}. "
              "Translate accurately and fluently, using the standard terminology of the field. "
              "Keep person names, DOIs, URLs, citation numbers, variable names and units unchanged.")
    if keep_references:
        prompt += (" If an item is an entry of a bibliography or reference list, output that item exactly as given, "
                   "without translating any part of it.")
    return prompt


class StatsHandler(logging.Handler):
    """Picks BabelDOC's own paragraph statistics out of its log."""

    PATTERN = re.compile(r"Translation completed\. Total: (\d+), Successful: (\d+), Fallback: (\d+)")

    def __init__(self):
        super().__init__(logging.INFO)
        self.total = self.ok = self.fallback = 0

    def emit(self, record):
        message = record.getMessage()
        match = self.PATTERN.search(message)
        if match:
            self.total, self.ok, self.fallback = (self.total + int(match[1]), self.ok + int(match[2]),
                                                  self.fallback + int(match[3]))


def make_translator(job: dict, key: str):
    from babeldoc.translator.translator import BaseTranslator
    from zotero_mineru.llm import LLMClient

    class LLMTranslator(BaseTranslator):
        """BabelDOC translator that sends its prompts to the user's chosen model (OpenAI-compatible or Anthropic)."""

        name = "zmt"

        def __init__(self):
            super().__init__(job["lang_in"], job["lang_out"], ignore_cache=False)
            self.add_cache_impact_parameters("model", f"{job['provider']}|{job['model']}")
            self.add_cache_impact_parameters("prompt", PROMPT_VERSION + ("|keep-refs" if job.get("keep_references") else ""))
            self.local = threading.local()
            self.lock = threading.Lock()
            self.usage = {"calls": 0, "input": 0, "output": 0, "failed": 0}
            self.last_error = ""
            self.display = self.client().display

        def probe(self):
            """One tiny request first: a wrong key or model fails here at once, instead of BabelDOC quietly keeping
            the original text of every paragraph."""
            self.client().chat("You are a translator.", "Translate into Chinese: phase-field fracture", max_tokens=256,
                               temperature=0)

        def client(self):
            # One HTTP session per worker thread.
            if not hasattr(self.local, "client"):
                client = LLMClient(job["provider"], key, job["model"], job.get("base", ""), timeout=300)
                client.extra = dict(THINKING_OFF.get(job["provider"], {}))
                self.local.client = client
            return self.local.client

        def _chat(self, prompt: str) -> str:
            try:
                text, used = self.client().chat(system_prompt(job["lang_out"], job.get("keep_references", False)),
                                                prompt, max_tokens=8192, temperature=0)
            except Exception as error:  # BabelDOC falls back to single paragraphs, then keeps the original text
                with self.lock:
                    self.usage["failed"] += 1
                    self.last_error = str(error)
                raise
            with self.lock:
                self.usage["calls"] += 1
                self.usage["input"] += used["input"]
                self.usage["output"] += used["output"]
            return text

        def do_llm_translate(self, text, rate_limit_params: dict = None):
            if text is None:  # BabelDOC's capability probe
                return None
            return self._chat(text)

        def do_translate(self, text, rate_limit_params: dict = None):
            return self._chat("Translate the following text. Keep every placeholder such as {v1} and every tag such as "
                              "<b>…</b> exactly as it is. Output only the translation.\n\n" + text)

    return LLMTranslator()


def patch_fonts():
    """BabelDOC sets italic Chinese in a handwriting (Kai) face; papers read better with the regular serif/sans face."""
    from babeldoc.assets import assets

    original = assets.get_font_family

    def font_family(lang_code):
        family = dict(original(lang_code))
        family["script"] = []
        return family

    assets.get_font_family = font_family


def patch_reference_split():
    """Start a new paragraph at every numbered entry of a list ("43. Raghu P, …", "[43] …").

    BabelDOC only splits after a short line, so a reference whose predecessor ends with a full-width line was merged
    into it and the two entries were translated and set as one block."""
    from babeldoc.format.pdf.document_il.midend import paragraph_finder as finder

    original = finder.ParagraphFinder.process_independent_paragraphs

    def process_independent_paragraphs(self, paragraphs, median_width):
        index = 0
        while index < len(paragraphs):
            paragraph = paragraphs[index]
            lines = paragraph.pdf_paragraph_composition
            texts = ["".join(c.char_unicode or "" for c in part.pdf_line.pdf_character) if part.pdf_line else ""
                     for part in lines]
            starts = [n for n, text in enumerate(texts) if n and LIST_ENTRY.match(text)]
            # Only lists: the paragraph starts with an entry, or holds at least two (body text rarely does).
            if starts and (LIST_ENTRY.match(texts[0]) or len(starts) >= 2):
                split = starts[0]
                tail = finder.PdfParagraph(box=finder.Box(0, 0, 0, 0), pdf_paragraph_composition=lines[split:],
                                           unicode="", debug_id=finder.generate_base58_id(),
                                           layout_label=paragraph.layout_label, layout_id=paragraph.layout_id)
                paragraph.pdf_paragraph_composition = lines[:split]
                self.update_paragraph_data(paragraph)
                self.update_paragraph_data(tail)
                paragraphs.insert(index + 1, tail)
            index += 1
        return original(self, paragraphs, median_width)

    finder.ParagraphFinder.process_independent_paragraphs = process_independent_paragraphs


def glyph_fonts(lang_out: str):
    import pymupdf
    from babeldoc.assets import assets

    family = assets.get_font_family(lang_out)
    names = dict.fromkeys(family["normal"] + family["fallback"] + family["base"])
    return [pymupdf.Font(fontfile=str(assets.get_font_and_metadata(name)[0])) for name in names]


def _standard(code: int) -> int:
    """The ordinary character for a CJK compatibility ideograph or Kangxi radical (U+F98A 力 → U+529B 力)."""
    if 0xF900 <= code <= 0xFAFF or 0x2F00 <= code <= 0x2FDF:
        normal = unicodedata.normalize("NFKC", chr(code))
        if len(normal) == 1:
            return ord(normal)
    return code


def normalize_tounicode(cmap: str) -> tuple[str, int]:
    """Point every glyph of a ToUnicode CMap at the ordinary character.

    Fonts such as Source Han Serif draw 力 and its compatibility twin U+F98A with one glyph, and the CMap written for
    the embedded subset may name the twin: the page looks right, but copied or searched text (in Zotero too) does not
    match. bfchar entries are rewritten in place; bfranges that cover such characters become bfchar entries."""
    fixed = 0

    def bfchar(block):
        def line(match):
            nonlocal fixed
            code = int(match[3], 16)
            if _standard(code) == code:
                return match[0]
            fixed += 1
            return f"{match[1]}<{match[2]}> <{_standard(code):04x}>"
        # One entry per line; a destination of several characters does not match and stays as it is.
        return re.sub(r"(?m)^(\s*)<([0-9a-fA-F]+)>\s*<([0-9a-fA-F]{4})>\s*$", line, block)

    def ranges(match):
        nonlocal fixed
        kept, single = [], []
        for line in match[2].splitlines():
            if not line.strip():
                continue
            # One entry per line; a destination of several characters (ligatures such as "fi") is kept as it is.
            entry = re.fullmatch(r"\s*<([0-9a-fA-F]+)>\s*<([0-9a-fA-F]+)>\s*<([0-9a-fA-F]{4})>\s*", line)
            first, last, code = (int(entry[1], 16), int(entry[2], 16), int(entry[3], 16)) if entry else (0, -1, 0)
            if not entry or not 0 <= last - first <= 0xFF or \
                    all(_standard(code + n) == code + n for n in range(last - first + 1)):
                kept.append(line.strip())
                continue
            width, run = len(entry[1]), None
            for n in range(last - first + 2):  # one past the end closes the last run
                good = n <= last - first and _standard(code + n) == code + n
                if good and run is None:
                    run = n
                elif not good and run is not None:
                    kept.append(f"<{first + run:0{width}x}> <{first + n - 1:0{width}x}> <{code + run:04x}>")
                    run = None
                if n <= last - first and not good:
                    fixed += 1
                    single.append(f"<{first + n:0{width}x}> <{_standard(code + n):04x}>")
        text = ""
        for groups, name in ((kept, "bfrange"), (single, "bfchar")):
            for start in range(0, len(groups), 100):
                chunk = groups[start:start + 100]
                text += f"{len(chunk)} begin{name}\n" + "\n".join(chunk) + f"\nend{name}\n"
        return text.rstrip("\n")

    cmap = re.sub(r"(\d+)\s+beginbfchar(.*?)endbfchar", lambda m: bfchar(m[0]), cmap, flags=re.DOTALL)
    # Ranges written with an array of destinations are left alone (none of BabelDOC's fonts use them).
    cmap = re.sub(r"(\d+)\s+beginbfrange((?:(?!\[).)*?)endbfrange", ranges, cmap, flags=re.DOTALL)
    return cmap, fixed


def fix_text_layer(path: Path) -> int:
    """Normalise the ToUnicode maps of the Chinese fonts BabelDOC embedded; the paper's own fonts stay untouched.

    Only the changed maps are appended (incremental save): rewriting the whole file made it six times larger."""
    import pymupdf

    fixed, temporary = 0, None
    with pymupdf.open(path) as document:
        for xref in range(1, document.xref_length()):
            try:
                name, unicode_map = document.xref_get_key(xref, "BaseFont"), document.xref_get_key(xref, "ToUnicode")
            except Exception:
                continue
            if name[0] != "name" or unicode_map[0] != "xref" or not any(
                    family in name[1] for family in ("Source Han", "Noto", "LXGW", "Klee", "MaruBuri")):
                continue
            stream = int(unicode_map[1].split()[0])
            cmap, count = normalize_tounicode(document.xref_stream(stream).decode("latin-1"))
            if count:
                document.update_stream(stream, cmap.encode("latin-1"))
                fixed += count
        if fixed and document.can_save_incrementally():
            document.saveIncr()
        elif fixed:
            temporary = path.with_name(path.stem + ".fixing.pdf")
            document.save(temporary, garbage=0, deflate=True, use_objstms=1)
    if temporary:
        os.replace(temporary, path)
    return fixed


def inspect(dual: Path, fonts) -> dict:
    """Check the finished PDF: broken characters and characters no Chinese font can draw (they would show as empty
    boxes) on the translated side, and pages that look untranslated. Characters that also appear on the original
    side (formulas, symbols kept from the paper) are drawn with the paper's own fonts and are not counted."""
    import pymupdf

    def bad(text):
        return sum(1 for ch in text if ch == "�" or 0xE000 <= ord(ch) <= 0xF8FF)

    broken, cjk, untranslated, missing, twins = 0, 0, [], {}, 0
    with pymupdf.open(dual) as document:
        pages = document.page_count
        for number, page in enumerate(document, 1):
            half = page.rect.width / 2
            left = page.get_text("text", clip=pymupdf.Rect(0, 0, half, page.rect.height))
            right = page.get_text("text", clip=pymupdf.Rect(half, 0, page.rect.width, page.rect.height))
            broken += max(0, bad(left) - bad(right))
            twins += sum(1 for ch in left if _standard(ord(ch)) != ord(ch))  # text that would not copy or search
            for char in set(left) - set(right):
                if ord(char) >= 0x80 and not char.isspace() and not unicodedata.category(char).startswith("C") \
                        and char not in missing and not any(font.has_glyph(ord(char)) for font in fonts):
                    missing[char] = number
            page_cjk = sum(1 for ch in left if "一" <= ch <= "鿿")
            cjk += page_cjk
            latin = sum(1 for ch in right if ch.isascii() and ch.isalpha())
            if latin > 800 and page_cjk < 40 and len(REFERENCE.findall(right)) < 5:
                untranslated.append(number)
    return {"pages": pages, "broken_chars": broken + twins, "cjk_chars": cjk, "untranslated_pages": untranslated,
            "missing_glyphs": list(missing)[:20]}


def check() -> None:
    import babeldoc
    import babeldoc.format.pdf.high_level as high_level
    from babeldoc.assets import assets

    emit("progress", percent=0, stage="检查翻译引擎的版面模型和字体（首次使用会下载约 300 MB）")
    high_level.init()
    assets.warmup()
    fonts = glyph_fonts("zh")
    emit("ready", babeldoc=getattr(babeldoc, "__version__", ""), fonts=len(fonts),
         cache=str(Path.home() / ".cache" / "babeldoc"))


def translate(job: dict, key: str) -> None:
    import babeldoc.format.pdf.high_level as high_level
    from babeldoc.docvision.doclayout import DocLayoutModel
    from babeldoc.format.pdf.translation_config import TranslationConfig, WatermarkOutputMode
    from babeldoc.glossary import Glossary
    from babeldoc.translator.translator import set_translate_rate_limiter

    started = time.time()
    stats = StatsHandler()
    logging.getLogger("babeldoc").addHandler(stats)
    high_level.init()
    translator = make_translator(job, key)
    emit("progress", percent=0, stage="测试大模型连接")
    translator.probe()
    emit("progress", percent=0, stage="加载版面模型和字体")
    patch_fonts()
    patch_reference_split()
    layout = DocLayoutModel.load_onnx()
    qps = max(1, int(job.get("qps", 8)))
    set_translate_rate_limiter(qps)
    glossaries, glossary_error = [], ""
    if job.get("glossary") and Path(job["glossary"]).is_file():
        try:
            glossary = Glossary.from_csv(Path(job["glossary"]), job["lang_out"])
            if glossary.entries:
                glossaries.append(glossary)
        except Exception as error:  # a malformed glossary must not stop the translation
            glossary_error = f"术语表未使用（{type(error).__name__}）：第一行应为 source,target，每行一个“英文,中文”"
    output_dir, work = Path(job["output_dir"]), Path(job["work_dir"])
    output_dir.mkdir(parents=True, exist_ok=True)
    work.mkdir(parents=True, exist_ok=True)
    pages = job.get("page_count") or 0
    config = TranslationConfig(
        translator=translator, input_file=job["input"], lang_in=job["lang_in"], lang_out=job["lang_out"],
        doc_layout_model=layout, output_dir=output_dir, working_dir=work,
        no_dual=False, no_mono=not job.get("mono", False),
        dual_translate_first=True, use_alternating_pages_dual=False,
        watermark_output_mode=WatermarkOutputMode.NoWatermark,
        qps=qps, pool_max_workers=qps, use_rich_pbar=False, report_interval=1.0,
        split_short_lines=True, primary_font_family=job.get("font", "serif"),
        auto_extract_glossary=job.get("auto_terms", True), save_auto_extracted_glossary=False,
        glossaries=glossaries or None, custom_system_prompt=system_prompt(job["lang_out"], job.get("keep_references", False)),
        disable_same_text_fallback=bool(job.get("keep_references")),
        split_strategy=TranslationConfig.create_max_pages_per_part_split_strategy(50) if pages > 80 else None,
    )
    result = None

    async def run():
        nonlocal result
        last = 0.0
        async for event in high_level.async_translate(config):
            kind = event["type"]
            if kind in ("progress_start", "progress_update", "progress_end"):
                now = time.monotonic()
                if kind != "progress_update" or now - last >= 1:
                    last = now
                    stage = STAGES.get(event["stage"], event["stage"])
                    if event.get("stage_total"):
                        stage += f"（{event.get('stage_current', 0)}/{event['stage_total']}）"
                    emit("progress", percent=round(event.get("overall_progress", 0), 1), stage=stage,
                         calls=translator.usage["calls"])
            elif kind == "finish":
                result = event["translate_result"]
                break
            elif kind == "error":
                raise RuntimeError(str(event.get("error") or "翻译引擎出错"))

    asyncio.run(run())
    if result is None or not result.dual_pdf_path:
        raise RuntimeError("翻译引擎没有生成双语 PDF。")
    final = output_dir / job["name"]
    shutil.move(str(result.dual_pdf_path), final)
    text_fixed = fix_text_layer(final)
    mono = ""
    if job.get("mono") and result.mono_pdf_path:
        mono_path = output_dir / job["mono_name"]
        shutil.move(str(result.mono_pdf_path), mono_path)
        fix_text_layer(mono_path)
        mono = str(mono_path)
    report = inspect(final, glyph_fonts(job["lang_out"]))
    emit("done", dual=str(final), mono=mono, seconds=round(time.time() - started), model=translator.display,
         usage=translator.usage, paragraphs={"total": stats.total, "ok": stats.ok, "fallback": stats.fallback},
         glossary_error=glossary_error, last_error=translator.last_error[:200], text_fixed=text_fixed, **report)


def main() -> int:
    multiprocessing.freeze_support()  # BabelDOC subsets fonts in a child process
    parser = argparse.ArgumentParser(description="ZoteroMinerU 保留排版翻译引擎（BabelDOC）")
    parser.add_argument("--job", type=Path)
    parser.add_argument("--check", action="store_true")
    args = parser.parse_args()
    if hasattr(sys.stderr, "reconfigure"):
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")  # engine.log stays readable with Chinese paths
    logging.basicConfig(level=logging.INFO, stream=sys.stderr, format="%(asctime)s %(name)s %(message)s")
    for noisy in ("httpx", "httpcore", "openai", "urllib3"):
        logging.getLogger(noisy).setLevel(logging.WARNING)
    try:
        if args.check:
            check()
        elif args.job:
            job = json.loads(args.job.read_text(encoding="utf-8"))
            translate(job, os.environ.get("ZMT_LLM_KEY", ""))
        else:
            parser.error("需要 --job 或 --check")
        return 0
    except BaseException as error:  # noqa: BLE001 - every failure must reach the main program as a message
        from zotero_mineru.clients import safe_message
        logging.exception("translation failed")
        message = ERRORS.get(type(error).__name__) or next(
            (text for key, text in ERRORS.items() if key in str(error)), "") or str(error) or type(error).__name__
        if isinstance(error, SystemExit) and not str(error).strip("0"):
            message = "翻译引擎提前退出（可能是下载版面模型或字体失败，请检查网络/代理）。"
        emit("error", message=safe_message(message, os.environ.get("ZMT_LLM_KEY", "")),
             retryable=getattr(error, "retryable", False))
        return 1


if __name__ == "__main__":
    sys.exit(main())
