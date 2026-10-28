from __future__ import annotations

import html
import ipaddress
import re
import uuid
from pathlib import Path
from urllib.parse import urljoin, urlparse
from urllib.request import url2pathname

import requests

KEY = re.compile(r"[A-Z0-9]{8}")
SKIP_TYPES = {"attachment", "note", "annotation"}
# Translated copies sit beside the original PDF. Matches Doc2X ("保留排版双语PDF…"), PDF2zh/BabelDOC ("-mono.pdf",
# ".zh.dual.pdf"), this program's 双语_/译文_ files and plain "译文" names, not words in titles such as "translation".
TRANSLATION = re.compile(r"译文|双语|翻译|译左|译右|中文版|bilingual|_zh_|\.zh\.|[-_.](?:mono|dual)\.pdf$", re.IGNORECASE)
# Side-by-side translations from any tool: Doc2X ("保留排版双语PDF(译左原右)…"), PDF2zh/BabelDOC ("…_dual.pdf",
# ".zh.dual.pdf") and this program's 双语_ files. A translation-only PDF (译文, -mono.pdf) is not one.
BILINGUAL = re.compile(r"双语|译左|译右|bilingual|[-_.]dual\.pdf$", re.IGNORECASE)
BILINGUAL_PREFIX, MONO_PREFIX = "双语_", "译文_"


class ServiceError(Exception):
    def __init__(self, message: str, *, retryable: bool = False, auth: bool = False, resume_stage: str | None = None):
        super().__init__(message)
        self.retryable = retryable
        self.auth = auth
        self.resume_stage = resume_stage


def safe_message(message: str, token: str = "") -> str:
    value = str(message)
    if token and len(token) >= 8:  # very short values would mangle ordinary text
        value = value.replace(token, "[密钥已隐藏]")
    value = re.sub(r"https?://[^\s\"<>]+", "[服务地址]", value)
    value = re.sub(r"(?i)Bearer\s+\S+", "Bearer [已隐藏]", value)
    return value[:600]


def _is_markdown_file(data: dict) -> bool:
    name = (data.get("filename") or data.get("path") or data.get("title") or "").lower()
    return "markdown" in (data.get("contentType") or "").lower() or name.endswith((".md", ".markdown"))


def is_summary_attachment(data: dict) -> bool:
    """AI summary cards are Markdown too, but named SUM_作者_论文名称.md and never count as the paper's full text."""
    name = Path(data.get("filename") or data.get("path") or "").name or data.get("title") or ""
    return _is_markdown_file(data) and name.upper().startswith("SUM_")


def is_markdown_attachment(data: dict) -> bool:
    return _is_markdown_file(data) and not is_summary_attachment(data)


def is_translated_pdf(data: dict) -> bool:
    """A PDF that is a translation of the paper (any tool's), not the original."""
    return data.get("contentType") == "application/pdf" and any(
        TRANSLATION.search(data.get(field) or "") for field in ("filename", "title"))


def is_bilingual_attachment(data: dict) -> bool:
    """A side-by-side bilingual PDF made by any tool: the paper then never gets another one from this program."""
    name = Path(data.get("filename") or data.get("path") or "").name
    return data.get("contentType") == "application/pdf" and any(
        BILINGUAL.search(value or "") for value in (name, data.get("title")))


def note_title(note_html: str) -> str:
    """First line of a Zotero note, as Zotero shows it in the item tree."""
    text = re.sub(r"<(br|/p|/h[1-6]|/div|/li)\b[^>]*>", "\n", note_html or "", flags=re.IGNORECASE)
    text = html.unescape(re.sub(r"<[^>]+>", "", text))
    return next((line.strip() for line in text.splitlines() if line.strip()), "（空笔记）")[:120]


def file_url_to_path(value: str) -> Path | None:
    parsed = urlparse(value or "")
    if parsed.scheme != "file" or parsed.netloc not in ("", "localhost"):
        return None
    return Path(url2pathname(parsed.path))


class ZoteroClient:
    def __init__(self, port: int = 23119, bridge_token: str = ""):
        if not 1024 <= int(port) <= 65535:
            raise ValueError("Zotero 端口无效。")
        self.base = f"http://127.0.0.1:{int(port)}"
        self.bridge_token = bridge_token
        self.session = requests.Session()
        self.session.trust_env = False
        self.session.headers.update({"Zotero-API-Version": "3", "User-Agent": "ZoteroMinerU/2.0"})

    def request(self, path: str, *, payload: dict | None = None, text: bool = False, headers: dict | None = None):
        try:
            response = self.session.request("GET" if payload is None else "POST", self.base + path,
                                            json=payload, headers=headers, timeout=(3, 60), allow_redirects=False)
        except requests.Timeout:
            raise ServiceError("Zotero 响应超时，请等待文库加载完成后重试。", retryable=True) from None
        except requests.RequestException:
            raise ServiceError("无法连接 Zotero。请打开 Zotero，并在 设置→高级 中勾选“允许此计算机上的其他应用程序与 Zotero 通信”。",
                               retryable=True) from None
        if not response.ok:
            try:
                detail = response.json().get("error", "")
            except (ValueError, AttributeError):
                detail = ""
            if detail:
                raise ServiceError("Zotero：" + str(detail), retryable=response.status_code >= 500)
        if response.status_code == 403:
            raise ServiceError("Zotero 拒绝访问：请在 Zotero 设置→高级 中启用本地 API（允许其他应用程序通信）。")
        if response.status_code == 404:
            raise ServiceError("Zotero 中找不到该地址：条目可能已删除，或尚未安装 MinerU 桥接插件。")
        if not response.ok:
            raise ServiceError(f"Zotero 返回 HTTP {response.status_code}。", retryable=response.status_code >= 500)
        if text:
            return response.text
        try:
            return response.json()
        except ValueError:
            raise ServiceError("Zotero 返回的数据格式不正确。", retryable=True) from None

    def rows(self, path: str) -> list[dict]:
        # The desktop API returns all matching rows without a default limit.
        rows = self.request("/api/users/0/" + path)
        if not isinstance(rows, list):
            raise ServiceError("Zotero 列表数据格式不正确。")
        return rows

    def collections(self) -> list[dict]:
        return self.rows("collections")

    def item(self, key: str) -> dict:
        return self.request(f"/api/users/0/items/{key}")

    def children(self, key: str) -> list[dict]:
        return self.rows(f"items/{key}/children")

    def bridge(self, op: str = "status", **data) -> dict:
        result = self.request("/mineru-bridge/v1", payload={"op": op, **data},
                              headers={"X-MinerU-Bridge-Token": self.bridge_token})
        if not isinstance(result, dict) or not result.get("ok"):
            raise ServiceError((result or {}).get("error", "桥接插件操作失败。"))
        return result

    def scope_keys(self, selected: list[str], include_sub: bool, collections: list[dict]) -> set[str]:
        keys = set(selected)
        if include_sub:
            while True:
                expanded = keys | {c["key"] for c in collections if c["data"].get("parentCollection") in keys}
                if expanded == keys:
                    break
                keys = expanded
        return keys

    def papers(self, selected: list[str], include_sub: bool, collections: list[dict]) -> list[dict]:
        """Regular items in the chosen collections, with their PDF and Markdown attachments."""
        if not selected:
            return []
        keys = self.scope_keys(selected, include_sub, collections)
        items: dict[str, dict] = {}
        if "__all__" in keys:
            items = {i["key"]: i for i in self.rows("items?itemType=-annotation")}
        else:
            for key in sorted(keys):
                if KEY.fullmatch(key):
                    for item in self.rows(f"collections/{key}/items"):
                        items[item["key"]] = item
        names = {c["key"]: c["data"]["name"] for c in collections}
        papers: dict[str, dict] = {}
        for key, item in items.items():
            data = item["data"]
            if data.get("deleted") or data.get("itemType") in SKIP_TYPES:
                continue
            papers[key] = {
                "key": key, "title": data.get("title") or "（无标题）",
                "creator": item.get("meta", {}).get("creatorSummary", ""),
                "date": data.get("date", ""), "date_added": data.get("dateAdded", ""),
                "collections": "、".join(names.get(c, c) for c in data.get("collections", []) if c in names),
                "url": data.get("url", ""), "pdfs": [], "markdowns": [], "summaries": [], "translations": [],
                "children": [],
            }
        for key, item in items.items():
            data = item["data"]
            parent = papers.get(data.get("parentItem", ""))
            if not parent or data.get("deleted") or data.get("itemType") not in ("attachment", "note"):
                continue
            if data.get("itemType") == "note":
                parent["children"].append({"key": key, "kind": "note", "title": note_title(data.get("note", "")),
                                           "filename": "", "path": "", "href": "", "link_mode": "",
                                           "date_added": data.get("dateAdded", "")})
                continue
            entry = {"key": key, "title": data.get("title", ""), "filename": data.get("filename") or Path(data.get("path") or "").name,
                     "path": data.get("path", ""), "link_mode": data.get("linkMode", ""),
                     "date_added": data.get("dateAdded", ""), "url": data.get("url", ""),
                     "href": item.get("links", {}).get("enclosure", {}).get("href", "")}
            if data.get("linkMode") == "linked_url":
                kind = "link"
            elif is_summary_attachment(data):
                kind = "summary"
                parent["summaries"].append(entry)
            elif is_markdown_attachment(data):
                kind = "markdown"
                parent["markdowns"].append(entry)
            elif data.get("contentType") == "application/pdf":
                # Translations stay candidates for conversion (choose_pdf ranks them last) but are listed apart.
                kind = "translation" if is_translated_pdf(data) else "pdf"
                parent["pdfs"].append(entry)
                if is_bilingual_attachment(data):
                    parent["translations"].append(entry)
            elif "html" in (data.get("contentType") or ""):
                kind = "snapshot"
            else:
                kind = "other"
            parent["children"].append({**entry, "kind": kind, "content_type": data.get("contentType", "")})
        order = {"pdf": 0, "translation": 1, "markdown": 2, "summary": 3, "note": 4, "snapshot": 5, "other": 6, "link": 7}
        for paper in papers.values():
            paper["children"].sort(key=lambda c: (order.get(c["kind"], 9), c.get("date_added", "")))
        return list(papers.values())

    def file_path(self, key: str, href: str = "") -> Path:
        path = file_url_to_path(href)
        if path is None:
            path = file_url_to_path(self.request(f"/api/users/0/items/{key}/file/view/url", text=True).strip())
        if path is None:
            raise ServiceError("PDF 尚未下载到本地，或附件路径不可用。", retryable=True)
        return path


class MinerUClient:
    def __init__(self, token: str, base: str = "https://mineru.net", *, allow_local: bool = False):
        self.token = token.strip().removeprefix("Bearer ").strip()
        self.base = base.rstrip("/")
        self.allow_local = allow_local
        self.session = requests.Session()

    def _validate_url(self, url: str) -> None:
        p = urlparse(url)
        if p.username or p.password or not p.hostname:
            raise ServiceError("解析服务返回了无效的下载／上传地址。")
        if self.allow_local and p.scheme == "http" and p.hostname in ("127.0.0.1", "localhost"):
            return
        if p.scheme != "https" or p.hostname.lower() in ("localhost", "localhost.localdomain"):
            raise ServiceError("解析服务返回了不安全的下载／上传地址。")
        try:
            address = ipaddress.ip_address(p.hostname)
        except ValueError:
            return
        if not address.is_global:
            raise ServiceError("解析服务返回了本地网络地址，已阻止访问。")

    def api(self, method: str, path: str, payload: dict | None = None, *, probe: bool = False) -> dict:
        if not self.token:
            raise ServiceError("请填写并保存 MinerU API Token。", auth=True)
        try:
            response = self.session.request(method, self.base + path, json=payload,
                headers={"Authorization": "Bearer " + self.token}, timeout=(15, 60), allow_redirects=False)
        except requests.RequestException:
            raise ServiceError("MinerU 网络连接失败，稍后自动重试。", retryable=True) from None
        if response.status_code in (401, 403):
            raise ServiceError("MinerU Token 被拒绝或已经过期，请在设置中更新 Token。", auth=True)
        if not response.ok:
            raise ServiceError(f"MinerU 返回 HTTP {response.status_code}。",
                               retryable=response.status_code == 429 or response.status_code >= 500)
        try:
            body = response.json()
        except ValueError:
            raise ServiceError("MinerU 返回非 JSON 数据，稍后重试。", retryable=True) from None
        code = str(body.get("code"))
        if code in ("A0202", "A0211"):
            raise ServiceError("MinerU Token 错误或已过期，请在设置中更新 Token。", auth=True)
        if probe and code in ("-60012", "-60013"):
            return {"probe": True}
        if code != "0":
            message = safe_message(body.get("msg", "未知错误"), self.token)
            hints = {"-60005": "文件超过 200 MB", "-60006": "文件超过 200 页", "-60018": "今日解析任务数已达上限",
                     "-60009": "提交队列已满", "-60007": "模型服务暂时不可用"}
            if code in hints:
                message = hints[code] + "（" + message + "）"
            raise ServiceError(f"MinerU（{code}）：{message}",
                               retryable=code in ("-10001", "-60001", "-60007", "-60009"),
                               resume_stage="queued" if code in ("-60012", "-60013") else None)
        return body.get("data") or {}

    def probe(self) -> str:
        self.api("GET", "/api/v4/extract-results/batch/" + str(uuid.uuid4()), probe=True)
        return "MinerU 接口可达，Token 有效。（未提交任何 PDF；额度以实际解析结果为准）"

    def submit(self, parts: list[dict], options: dict) -> tuple[str, list[str]]:
        """parts: [{"name", "data_id", "page_ranges"}]; one upload URL is returned per part."""
        files = []
        for part in parts:
            entry = {"name": part["name"], "data_id": part["data_id"], "is_ocr": bool(options.get("ocr"))}
            if part.get("page_ranges"):
                entry["page_ranges"] = part["page_ranges"]
            files.append(entry)
        payload = {"files": files, "model_version": options.get("model", "vlm"),
                   "enable_formula": bool(options.get("formula", True)), "enable_table": bool(options.get("table", True)),
                   "language": options.get("language", "ch")}
        data = self.api("POST", "/api/v4/file-urls/batch", payload)
        urls = data.get("file_urls") or []
        if not data.get("batch_id") or len(urls) != len(parts):
            raise ServiceError("MinerU 未返回有效的任务编号和上传地址。", retryable=True)
        for url in urls:
            self._validate_url(url)
        return data["batch_id"], urls

    def results(self, batch_id: str) -> dict[str, dict]:
        rows = self.api("GET", "/api/v4/extract-results/batch/" + batch_id).get("extract_result") or []
        return {row.get("data_id", ""): row for row in rows}

    def upload(self, url: str, path: Path) -> None:
        self._validate_url(url)
        try:
            with path.open("rb") as stream:
                # The signed URL rejects an explicit Content-Type header.
                response = self.session.put(url, data=stream, timeout=(15, 600), allow_redirects=False)
        except requests.RequestException:
            raise ServiceError("PDF 上传连接中断，稍后自动重试。", retryable=True) from None
        if response.status_code not in (200, 201, 204):
            raise ServiceError(f"PDF 上传失败（HTTP {response.status_code}）。",
                               retryable=response.status_code >= 500 or response.status_code == 429,
                               resume_stage="queued" if response.status_code in (401, 403, 404) else None)

    def download(self, url: str, path: Path, cancelled=lambda: False) -> None:
        temporary = path.with_name(path.name + ".part")
        try:
            for _ in range(6):
                self._validate_url(url)
                response = self.session.get(url, stream=True, timeout=(15, 120), allow_redirects=False)
                if response.is_redirect:
                    location = response.headers.get("Location", "")
                    response.close()
                    url = urljoin(url, location)
                    continue
                if not response.ok:
                    response.close()
                    raise ServiceError(f"结果下载失败（HTTP {response.status_code}）。",
                                       retryable=response.status_code >= 500 or response.status_code == 429,
                                       resume_stage="parsing" if response.status_code in (401, 403, 404) else None)
                total = 0
                path.parent.mkdir(parents=True, exist_ok=True)
                with response, temporary.open("wb") as stream:
                    for chunk in response.iter_content(1024 * 1024):
                        if cancelled():
                            raise ServiceError("下载已暂停，稍后继续。", retryable=True)
                        total += len(chunk)
                        if total > 1024 * 1024 * 1024:
                            raise ServiceError("结果压缩包超过 1 GB，已停止下载。")
                        stream.write(chunk)
                temporary.replace(path)
                return
            raise ServiceError("下载地址重定向次数过多。")
        except requests.RequestException:
            raise ServiceError("结果下载连接中断，稍后自动重试。", retryable=True) from None
