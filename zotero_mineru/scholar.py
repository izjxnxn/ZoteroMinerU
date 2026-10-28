"""Semantic Scholar and OpenAlex: citation links between library papers, new papers citing them, similar new papers.

Both databases are free. Semantic Scholar needs no key (an optional key gets a dedicated rate limit) and returns each
paper's SPECTER2 embedding, which serves as a field-independent relevance gate without bundling an embedding model.
OpenAlex single-work lookups are free and carry reference lists; its list queries supplement citing papers.

The relevance arithmetic follows Agentero's "who cites my library" scan (MIT licence, design only): skip classics,
weight a citation by how rarely the cited paper is cited, gate by max-similarity to any one library paper after
subtracting the background mean, calibrate the threshold on the library itself, then diversify with MMR.
"""
from __future__ import annotations

import math
import re
import threading
import time

import requests

from .clients import ServiceError

S2_BASE = "https://api.semanticscholar.org"
OPENALEX_BASE = "https://api.openalex.org"
CANDIDATE_FIELDS = "paperId,externalIds,title,year,publicationDate,venue,citationCount,abstract"
SEED_CITES_MAX = 2000  # field-defining classics: their citers say nothing about this collection's direction
MMR_LAMBDA = 0.7


def normalize_doi(value: str | None) -> str:
    value = (value or "").strip().lower()
    value = re.sub(r"^(?:https?://(?:dx\.)?doi\.org/|doi:\s*)", "", value)
    return value if value.startswith("10.") else ""


def normalize_title(value: str | None) -> str:
    return re.sub(r"[^0-9a-z一-鿿]+", "", (value or "").lower())


def openalex_abstract(inverted: dict | None) -> str:
    if not inverted:
        return ""
    words = sorted((position, word) for word, positions in inverted.items() for position in positions)
    return " ".join(word for _, word in words)


def short_id(openalex_id: str | None) -> str:
    return (openalex_id or "").rstrip("/").rsplit("/", 1)[-1]


# ------------------------------------------------------------------ vector arithmetic
def unit(vector: list[float]) -> list[float]:
    norm = math.sqrt(sum(x * x for x in vector))
    return [x / norm for x in vector] if norm > 1e-12 else list(vector)


def cosine(a: list[float], b: list[float]) -> float:
    return sum(x * y for x, y in zip(a, b)) if len(a) == len(b) else 0.0


def mean_vector(vectors: list[list[float]]) -> list[float]:
    if not vectors:
        return []
    size = len(vectors[0])
    total = [0.0] * size
    for vector in vectors:
        if len(vector) == size:
            for n, x in enumerate(vector):
                total[n] += x
    return [x / len(vectors) for x in total]


def center(vector: list[float], background: list[float]) -> list[float]:
    """Raw SPECTER2 cosines crowd into 0.80–0.96; subtracting the background mean spreads them out."""
    return unit([x - b for x, b in zip(unit(vector), background)])


def self_threshold(references: list[list[float]]) -> float | None:
    """10th percentile of each library paper's similarity to its nearest other paper (needs at least three)."""
    if len(references) < 3:
        return None
    nearest = sorted(max(cosine(v, o) for j, o in enumerate(references) if j != i) for i, v in enumerate(references))
    return nearest[min(len(nearest) - 1, len(nearest) * 10 // 100)]


def max_similarity(vector: list[float], references: list[list[float]]) -> float | None:
    """Similarity to the closest single paper — a multi-topic collection has no meaningful centroid."""
    return max((cosine(vector, r) for r in references), default=None)


def idf_weight(citation_count: int) -> float:
    """Citing a much-cited classic tells little; citing a rarely cited paper tells a lot."""
    return 1.0 / math.log10(max(0, citation_count) + 10)


def mmr_select(scores: list[float], vectors: list[list[float] | None], budget: int, lam: float = MMR_LAMBDA) -> list[int]:
    """Greedy maximal-marginal-relevance pick, so one hot direction does not take every slot."""
    remaining, picked = list(range(len(scores))), []
    while remaining and len(picked) < budget:
        def value(index):
            vector = vectors[index]
            redundancy = max((cosine(vector, vectors[p]) for p in picked if vector and vectors[p]), default=0.0)
            return lam * scores[index] - (1 - lam) * redundancy
        best = max(remaining, key=value)
        remaining.remove(best)
        picked.append(best)
    return picked


# ------------------------------------------------------------------ client
class ScholarClient:
    """Rate-limited, retrying access to both databases, through the system proxy or directly, whichever works."""

    def __init__(self, s2_key: str = "", openalex_key: str = "", *, s2_base: str = S2_BASE,
                 openalex_base: str = OPENALEX_BASE, s2_pause: float = 1.1, openalex_pause: float = 0.12,
                 retries: int = 6, sleep=time.sleep, cancelled=lambda: False, on_wait=lambda seconds, reason: None):
        self.s2_key, self.openalex_key = (s2_key or "").strip(), (openalex_key or "").strip()
        self.s2_base, self.openalex_base = s2_base.rstrip("/"), openalex_base.rstrip("/")
        self.pause = {"s2": s2_pause, "openalex": openalex_pause}
        self.retries, self.sleep, self.cancelled, self.on_wait = retries, sleep, cancelled, on_wait
        # Through a local proxy, Python's TLS connections are sometimes dropped mid-handshake while a direct
        # connection works (and on other networks the reverse): a failed connection switches that service's route.
        self.sessions = {"proxy": self._new_session(True), "direct": self._new_session(False)}
        self.route = {"s2": "proxy", "openalex": "proxy"}
        self.last = {"s2": 0.0, "openalex": 0.0}
        self.lock = threading.Lock()
        self.batch_ok = True  # the shared pool often answers /paper/batch with 429; then fall back to single lookups
        # Without a key the shared pool can answer only about once a minute for long stretches: after three failed
        # lookups in a row Semantic Scholar is skipped for the rest of the run instead of stalling it.
        self.s2_failures, self.s2_down = 0, False
        self.requests = 0

    @staticmethod
    def _new_session(use_proxy: bool) -> requests.Session:
        session = requests.Session()
        session.trust_env = use_proxy
        session.headers["User-Agent"] = "ZoteroMinerU/2.7 (research tracking; +https://github.com/opendatalab/MinerU)"
        return session

    def _wait(self, service: str):
        with self.lock:
            delay = self.last[service] + self.pause[service] - time.monotonic()
            if delay > 0:
                self.sleep(delay)
            self.last[service] = time.monotonic()

    def request(self, service: str, method: str, url: str, *, params=None, json=None, tries: int | None = None,
                breaker: bool = True):
        """JSON body, or None for 404. Retries 429/5xx/network errors with backoff; other errors raise."""
        if service == "s2" and self.s2_down:
            raise ServiceError("Semantic Scholar 暂时不可用，本次运行已跳过（填写免费的 Semantic Scholar Key 可避免）。", retryable=True)
        if tries is None:
            tries = self.retries if service == "openalex" or self.s2_key else 3
        try:
            result = self._request(service, method, url, params=params, json=json, tries=tries)
        except ServiceError as error:
            if service == "s2" and breaker and error.retryable and not self.cancelled():
                self.s2_failures += 1
                self.s2_down = self.s2_failures >= 3
            raise
        if service == "s2":
            self.s2_failures = 0
        return result

    def _request(self, service: str, method: str, url: str, *, params, json, tries: int):
        headers = {}
        if service == "s2" and self.s2_key:
            headers["x-api-key"] = self.s2_key
        if service == "openalex" and self.openalex_key:
            params = dict(params or {}, api_key=self.openalex_key)
        for attempt in range(tries):
            if self.cancelled():
                raise ServiceError("已停止。")
            self._wait(service)
            self.requests += 1
            route = self.route[service]
            try:
                response = self.sessions[route].request(method, url, params=params, json=json, headers=headers,
                                                        timeout=(15, 60))
            except requests.RequestException:
                # A dropped connection can leave the pool unusable: start a fresh one and try the other route next.
                self.sessions[route].close()
                self.sessions[route] = self._new_session(route == "proxy")
                self.route[service] = "direct" if route == "proxy" else "proxy"
                name = "Semantic Scholar" if service == "s2" else "OpenAlex"
                if attempt == 0 and tries > 1:  # the other route may work at once: no pause before trying it
                    continue
                if attempt + 1 >= tries:
                    raise ServiceError(f"无法连接 {name}，请检查网络或代理。", retryable=True) from None
                self.on_wait(min(60, 4 * 2 ** attempt), f"连接 {name} 失败")
                self.sleep(min(60, 4 * 2 ** attempt))
                continue
            if response.status_code == 404:
                return None
            if response.status_code in (429, 500, 502, 503, 504):
                if attempt + 1 >= tries:
                    name = "Semantic Scholar" if service == "s2" else "OpenAlex"
                    raise ServiceError(f"{name} 繁忙或达到频率上限（HTTP {response.status_code}），稍后再试。", retryable=True)
                retry_after = response.headers.get("Retry-After", "")
                wait = min(90, float(retry_after) if retry_after.isdigit() else 4 * 2 ** attempt)
                self.on_wait(wait, f"{'Semantic Scholar' if service == 's2' else 'OpenAlex'} 繁忙（HTTP {response.status_code}）")
                self.sleep(wait)
                continue
            if not response.ok:
                detail = response.text[:200].replace(self.openalex_key or "\0", "[密钥已隐藏]")
                raise ServiceError(f"{'Semantic Scholar' if service == 's2' else 'OpenAlex'} 返回 HTTP {response.status_code}：{detail}",
                                   auth=response.status_code in (401, 403))
            try:
                return response.json()
            except ValueError:
                raise ServiceError("文献数据库返回的数据不是 JSON。", retryable=True) from None
        return None

    # ---------------------------------------------------------------- Semantic Scholar
    def s2_papers(self, ids: list[str], fields: str) -> list[dict | None]:
        """Look up papers by S2 id / "DOI:…" / "arXiv:…", in input order (None when unknown)."""
        result: list[dict | None] = [None] * len(ids)
        start = 0
        while self.batch_ok and start < len(ids):
            chunk = ids[start:start + 100]
            try:
                rows = self.request("s2", "POST", f"{self.s2_base}/graph/v1/paper/batch", params={"fields": fields},
                                    json={"ids": chunk}, tries=2, breaker=False)
            except ServiceError as error:
                if not error.retryable:
                    raise
                self.batch_ok = False
                break
            for n, row in enumerate(rows or []):
                result[start + n] = row if isinstance(row, dict) else None
            start += len(chunk)
        for n in range(start, len(ids)):
            try:
                result[n] = self.request("s2", "GET", f"{self.s2_base}/graph/v1/paper/{ids[n]}", params={"fields": fields})
            except ServiceError as error:
                if not error.retryable:
                    raise
                result[n] = None
        return result

    def s2_citations(self, paper_id: str, fields: str = CANDIDATE_FIELDS) -> list[dict]:
        data = self.request("s2", "GET", f"{self.s2_base}/graph/v1/paper/{paper_id}/citations",
                            params={"fields": fields, "limit": 1000}) or {}
        return [row["citingPaper"] for row in data.get("data") or [] if (row.get("citingPaper") or {}).get("paperId")]

    def s2_recommend(self, positive: list[str], negative: list[str] = (), limit: int = 100,
                     fields: str = CANDIDATE_FIELDS) -> list[dict]:
        data = self.request("s2", "POST", f"{self.s2_base}/recommendations/v1/papers",
                            params={"limit": max(1, min(500, limit)), "fields": fields},
                            json={"positivePaperIds": list(positive)[:100], "negativePaperIds": list(negative)[:100]}) or {}
        return [row for row in data.get("recommendedPapers") or [] if row.get("paperId")]

    def s2_match(self, title: str) -> dict | None:
        data = self.request("s2", "GET", f"{self.s2_base}/graph/v1/paper/search/match",
                            params={"query": title[:300], "fields": "paperId,externalIds,title,citationCount"}) or {}
        rows = data.get("data") or []
        best = rows[0] if rows else None
        return best if best and normalize_title(best.get("title")) == normalize_title(title) else None

    # ---------------------------------------------------------------- OpenAlex
    def openalex_work(self, doi: str) -> dict | None:
        """Free single-record lookup: OpenAlex id, reference list and citation count of a DOI."""
        return self.request("openalex", "GET", f"{self.openalex_base}/works/doi:{doi}",
                            params={"select": "id,doi,referenced_works,cited_by_count,publication_year"})

    @property
    def s2_fast(self) -> bool:
        """Per-paper Semantic Scholar requests are affordable only with a key (dedicated rate limit)."""
        return bool(self.s2_key)

    def openalex_works(self, *, filter: str, search: str = "", per_page: int = 100, pages: int = 1) -> list[dict]:
        select = "id,doi,title,publication_date,publication_year,primary_location,abstract_inverted_index,cited_by_count,referenced_works"
        rows, cursor = [], "*"
        for _ in range(max(1, pages)):
            params = {"filter": filter, "per-page": per_page, "select": select, "cursor": cursor}
            if search:
                params["search"] = search
            data = self.request("openalex", "GET", f"{self.openalex_base}/works", params=params) or {}
            rows += data.get("results") or []
            cursor = (data.get("meta") or {}).get("next_cursor")
            if not cursor:
                break
        return rows


def from_s2(row: dict) -> dict:
    ids = row.get("externalIds") or {}
    date = row.get("publicationDate") or (f"{row['year']}-01-01" if row.get("year") else "")
    doi = normalize_doi(ids.get("DOI"))
    return {"s2_id": row.get("paperId", ""), "openalex_id": "", "doi": doi, "arxiv": (ids.get("ArXiv") or "").lower(),
            "title": (row.get("title") or "").strip(), "date": date, "venue": row.get("venue") or "",
            "abstract": (row.get("abstract") or "").strip(), "citations": row.get("citationCount") or 0,
            "url": f"https://doi.org/{doi}" if doi else f"https://www.semanticscholar.org/paper/{row.get('paperId', '')}"}


def from_openalex(row: dict) -> dict:
    doi = normalize_doi(row.get("doi"))
    source = ((row.get("primary_location") or {}).get("source") or {}).get("display_name") or ""
    return {"s2_id": "", "openalex_id": short_id(row.get("id")), "doi": doi, "arxiv": "",
            "title": (row.get("title") or "").strip(), "date": row.get("publication_date") or "",
            "venue": source, "abstract": openalex_abstract(row.get("abstract_inverted_index")),
            "citations": row.get("cited_by_count") or 0,
            "url": f"https://doi.org/{doi}" if doi else (row.get("id") or "")}
