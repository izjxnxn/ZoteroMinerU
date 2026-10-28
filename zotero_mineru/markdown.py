"""PDF checks, MinerU result parsing and single-file Markdown with embedded images."""
from __future__ import annotations

import base64
import logging
import os
import re
import unicodedata
import zipfile
from pathlib import Path, PurePosixPath
from urllib.parse import unquote

from .clients import ServiceError

MAX_PDF_BYTES = 200 * 1024 * 1024
MAX_PAGES_PER_TASK = 200
IMAGE_TYPES = {
    ".png": "image/png", ".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".gif": "image/gif",
    ".webp": "image/webp", ".bmp": "image/bmp", ".svg": "image/svg+xml", ".avif": "image/avif",
    ".tif": "image/tiff", ".tiff": "image/tiff",
}
MD_IMAGE = re.compile(r"!\[(?P<alt>[^\]\n]*)\]\(\s*(?P<target><[^>\n]+>|[^)\s]+)(?P<title>\s+(?:\"[^\"\n]*\"|'[^'\n]*'))?\s*\)")
HTML_IMAGE = re.compile(r"(?P<head><img\b[^>]*?\bsrc\s*=\s*)(?P<quote>[\"']?)(?P<target>[^\"'\s>]+)(?P=quote)", re.IGNORECASE)


def check_pdf(path: Path) -> tuple[int, int]:
    """Return (size, mtime_ns) of a readable PDF within MinerU's limits."""
    stat = path.stat()
    if stat.st_size == 0:
        raise ServiceError("PDF 文件为空。")
    if stat.st_size > MAX_PDF_BYTES:
        raise ServiceError("PDF 超过 MinerU 单文件 200 MB 上限。")
    with path.open("rb") as stream:
        if b"%PDF-" not in stream.read(1024):
            raise ServiceError("附件内容不是有效的 PDF 文件。")
    return stat.st_size, stat.st_mtime_ns


def page_count(path: Path) -> int | None:
    try:
        from pypdf import PdfReader
        logging.getLogger("pypdf").setLevel(logging.CRITICAL)
        return len(PdfReader(str(path), strict=False).pages)
    except Exception:
        # Encrypted or damaged files are left to MinerU, which reports its own error.
        return None


def page_ranges(pages: int | None) -> list[str]:
    """Split long documents into MinerU-sized page ranges; an empty string means the whole file."""
    if not pages or pages <= MAX_PAGES_PER_TASK:
        return [""]
    return [f"{start}-{min(start + MAX_PAGES_PER_TASK - 1, pages)}"
            for start in range(1, pages + 1, MAX_PAGES_PER_TASK)]


def valid_filename(value: str) -> str:
    """Mirror Zotero.File.getValidFileName so fallback names look like Zotero's own."""
    value = re.sub(r'[\/\\?*:|"<>]', "", value)
    value = re.sub(r"[\r\n\t]+", " ", value)
    value = re.sub(r"[ - ]", " ", value)
    value = re.sub(r"[​-‎]", "", value)
    value = re.sub(r"[ - ]", " ", value)
    value = re.sub(r"[\x00-\x08\x0b\x0c\x0e-\x1f￾￿]", "", value)
    value = unicodedata.normalize("NFC", value).strip()
    value = value.lstrip(".")
    return value or "_"


def markdown_filename(base: str, prefix: str = "MD_") -> str:
    """MD_作者_论文名称.md (or SUM_… for AI summaries) — base is Zotero's '{firstCreator}_{title}' rendering."""
    stem = valid_filename(prefix + base.strip().strip("_"))
    # Keep room for Windows path limits; Zotero truncates titles to 100 characters already.
    stem = stem[:180].rstrip(" .")
    return stem + ".md"


def fallback_base(creator: str, title: str, pdf_name: str = "") -> str:
    title = (title or "").strip()[:100]
    if not title and pdf_name:
        title = Path(pdf_name).stem
    parts = [p for p in ((creator or "").strip(), title) if p]
    return valid_filename("_".join(parts)) if parts else "未命名文献"


def _data_uri(name: str, payload: bytes) -> str | None:
    mime = IMAGE_TYPES.get(PurePosixPath(name).suffix.lower())
    if not mime:
        return None
    return f"data:{mime};base64," + base64.b64encode(payload).decode("ascii")


def embed_images(text: str, load) -> tuple[str, int, int]:
    """Replace relative image links by base64 data URIs.

    ``load(relative_posix_path)`` returns bytes or None. Returns (text, embedded, missing).
    """
    cache: dict[str, str | None] = {}
    found: set[str] = set()
    lost: set[str] = set()

    def resolve(target: str) -> str | None:
        raw = target.strip()
        if raw.startswith("<") and raw.endswith(">"):
            raw = raw[1:-1].strip()
        lowered = raw.lower()
        if not raw or lowered.startswith(("data:", "http:", "https:", "ftp:", "mailto:", "#")):
            return None
        if raw not in cache:
            path = unquote(raw.split("#", 1)[0].split("?", 1)[0]).replace("\\", "/")
            parts = []
            for part in PurePosixPath(path).parts:
                if part in ("", ".", "/"):
                    continue
                if part == "..":
                    if parts:
                        parts.pop()
                    continue
                parts.append(part)
            name = "/".join(parts)
            payload = load(name) if parts else None
            cache[raw] = _data_uri(parts[-1], payload) if payload is not None else None
            (found if cache[raw] else lost).add(name.casefold())
        return cache[raw]

    def markdown(match: re.Match) -> str:
        uri = resolve(match.group("target"))
        if not uri:
            return match.group(0)
        return f"![{match.group('alt')}]({uri}{match.group('title') or ''})"

    def html(match: re.Match) -> str:
        uri = resolve(match.group("target"))
        if not uri:
            return match.group(0)
        return f'{match.group("head")}"{uri}"'

    text = MD_IMAGE.sub(markdown, text)
    text = HTML_IMAGE.sub(html, text)
    return text, len(found), len(lost)


def markdown_from_zip(archive: Path) -> tuple[str, int, int]:
    """Read MinerU's full.md from the result archive and embed every referenced image."""
    try:
        with zipfile.ZipFile(archive) as zf:
            infos = [i for i in zf.infolist() if not i.is_dir()]
            if len(infos) > 20000 or sum(i.file_size for i in infos) > 2 * 1024 * 1024 * 1024:
                raise ServiceError("解析结果文件数量或解压大小超出安全上限。")
            members = {i.filename.replace("\\", "/"): i for i in infos}
            folded = {name.casefold(): info for name, info in members.items()}
            candidates = [n for n in members if n.lower().endswith(".md")]
            preferred = sorted((n for n in candidates if PurePosixPath(n).name == "full.md"), key=len)
            if preferred:
                chosen = preferred[0]
            elif len(candidates) == 1:
                chosen = candidates[0]
            else:
                raise ServiceError("结果中未找到 Markdown 正文文件。")
            base = PurePosixPath(chosen).parent
            text = zf.read(members[chosen]).decode("utf-8-sig", errors="replace")

            def load(relative: str):
                name = (base / relative).as_posix() if str(base) not in ("", ".") else relative
                info = members.get(name) or folded.get(name.casefold()) or members.get(relative)
                if info is None or info.file_size > 64 * 1024 * 1024:
                    return None
                return zf.read(info)

            return embed_images(text, load)
    except (zipfile.BadZipFile, RuntimeError, KeyError) as error:
        raise ServiceError("下载的结果不是有效的 ZIP 压缩包，将重新下载。", retryable=True, resume_stage="download") from error


def join_parts(parts: list[tuple[str, str]]) -> str:
    """Concatenate split-task results in page order."""
    if len(parts) == 1:
        return parts[0][1]
    chunks = []
    for ranges, text in parts:
        chunks.append(f"<!-- MinerU 分段解析：第 {ranges} 页 -->\n\n{text.strip()}\n")
    return "\n\n".join(chunks)


def write_text(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name("." + path.name + ".tmp")
    temporary.write_text(text, encoding="utf-8", newline="\n")
    os.replace(temporary, path)


def unique_path(directory: Path, filename: str, owner_key: str) -> Path:
    """Avoid overwriting another paper's file with the same author/title."""
    target = directory / filename
    if not target.exists():
        return target
    stem, suffix = target.stem, target.suffix
    return directory / f"{stem}_{owner_key}{suffix}"
