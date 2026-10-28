"""Durable per-paper job stages: a failed attachment never requires another cloud parse."""
from __future__ import annotations

import json
import sqlite3
import threading
import time
from pathlib import Path


ACTIVE = ("queued", "upload", "parsing", "download", "build", "attach")
CLOUD = ("upload", "parsing")
STATES = {
    "queued": "排队中", "upload": "上传 PDF", "parsing": "云端解析",
    "download": "下载结果", "build": "生成 Markdown", "attach": "添加到 Zotero",
    "done": "已完成", "failed": "失败，待重试",
}
FIELDS = {"stage", "resume_stage", "batch_id", "parts", "markdown", "md_name", "result_key",
          "pdf_key", "pdf_name", "title", "creator", "error", "progress", "attempts", "next_try"}


class Store:
    def __init__(self, path: Path):
        self.lock = threading.RLock()
        self.db = sqlite3.connect(path, check_same_thread=False)
        self.db.row_factory = sqlite3.Row
        with self.db:
            self.db.executescript("""
                PRAGMA journal_mode=WAL;
                CREATE TABLE IF NOT EXISTS jobs (
                    key TEXT PRIMARY KEY, title TEXT NOT NULL DEFAULT '', creator TEXT NOT NULL DEFAULT '',
                    pdf_key TEXT NOT NULL DEFAULT '', pdf_name TEXT NOT NULL DEFAULT '',
                    md_name TEXT NOT NULL DEFAULT '', options TEXT NOT NULL DEFAULT '{}',
                    stage TEXT NOT NULL DEFAULT 'queued', resume_stage TEXT NOT NULL DEFAULT 'queued',
                    batch_id TEXT NOT NULL DEFAULT '', parts TEXT NOT NULL DEFAULT '[]',
                    markdown TEXT NOT NULL DEFAULT '', result_key TEXT NOT NULL DEFAULT '',
                    replace_key TEXT NOT NULL DEFAULT '', legacy_path TEXT NOT NULL DEFAULT '',
                    error TEXT NOT NULL DEFAULT '', progress TEXT NOT NULL DEFAULT '',
                    attempts INTEGER NOT NULL DEFAULT 0, next_try REAL NOT NULL DEFAULT 0,
                    created REAL NOT NULL, updated REAL NOT NULL
                );
                -- Papers the user ticked for conversion; anything not listed is unticked.
                CREATE TABLE IF NOT EXISTS selections (key TEXT PRIMARY KEY, enabled INTEGER NOT NULL);
                -- AI summary cards: queued -> attach (file written) -> done; failed keeps resume_stage.
                CREATE TABLE IF NOT EXISTS summaries (
                    key TEXT PRIMARY KEY, title TEXT NOT NULL DEFAULT '',
                    stage TEXT NOT NULL DEFAULT 'queued', resume_stage TEXT NOT NULL DEFAULT 'queued',
                    file TEXT NOT NULL DEFAULT '', result_key TEXT NOT NULL DEFAULT '', note_key TEXT NOT NULL DEFAULT '',
                    model TEXT NOT NULL DEFAULT '', card TEXT NOT NULL DEFAULT '{}', meta TEXT NOT NULL DEFAULT '{}',
                    error TEXT NOT NULL DEFAULT '', progress TEXT NOT NULL DEFAULT '',
                    attempts INTEGER NOT NULL DEFAULT 0, next_try REAL NOT NULL DEFAULT 0,
                    created REAL NOT NULL, updated REAL NOT NULL
                );
                -- Bilingual PDFs (translation left, original right): queued (engine runs) -> attach -> done.
                CREATE TABLE IF NOT EXISTS translations (
                    key TEXT PRIMARY KEY, title TEXT NOT NULL DEFAULT '',
                    stage TEXT NOT NULL DEFAULT 'queued', resume_stage TEXT NOT NULL DEFAULT 'queued',
                    file TEXT NOT NULL DEFAULT '', mono TEXT NOT NULL DEFAULT '', result_key TEXT NOT NULL DEFAULT '',
                    meta TEXT NOT NULL DEFAULT '{}', error TEXT NOT NULL DEFAULT '', progress TEXT NOT NULL DEFAULT '',
                    attempts INTEGER NOT NULL DEFAULT 0, next_try REAL NOT NULL DEFAULT 0,
                    created REAL NOT NULL, updated REAL NOT NULL
                );
                -- Collection reviews: the model's JSON, the papers it covered and the files written for it.
                CREATE TABLE IF NOT EXISTS research (
                    key TEXT PRIMARY KEY, name TEXT NOT NULL DEFAULT '', path TEXT NOT NULL DEFAULT '',
                    fingerprint TEXT NOT NULL DEFAULT '', report TEXT NOT NULL DEFAULT '{}', papers TEXT NOT NULL DEFAULT '[]',
                    info TEXT NOT NULL DEFAULT '{}', files TEXT NOT NULL DEFAULT '{}', model TEXT NOT NULL DEFAULT '',
                    status TEXT NOT NULL DEFAULT '', error TEXT NOT NULL DEFAULT '', generated REAL NOT NULL DEFAULT 0,
                    updated REAL NOT NULL DEFAULT 0
                );
                -- New-paper tracking per collection: recent runs, papers already reviewed, Semantic Scholar cache.
                CREATE TABLE IF NOT EXISTS tracking (
                    key TEXT PRIMARY KEY, runs TEXT NOT NULL DEFAULT '[]', seen TEXT NOT NULL DEFAULT '[]',
                    cache TEXT NOT NULL DEFAULT '{}', last_run REAL NOT NULL DEFAULT 0, error TEXT NOT NULL DEFAULT ''
                );
                -- OpenAlex lookups of library papers by DOI (id and reference list), shared by all collections.
                CREATE TABLE IF NOT EXISTS scholar_cache (id TEXT PRIMARY KEY, data TEXT NOT NULL, updated REAL NOT NULL);
                CREATE TABLE IF NOT EXISTS kv (name TEXT PRIMARY KEY, value TEXT NOT NULL);
            """)

    def update(self, key: str, **values) -> None:
        if not values.keys() <= FIELDS:
            raise ValueError("Invalid job fields")
        if "parts" in values and not isinstance(values["parts"], str):
            values["parts"] = json.dumps(values["parts"], ensure_ascii=False)
        values["updated"] = time.time()
        with self.lock, self.db:
            self.db.execute("UPDATE jobs SET " + ",".join(name + "=?" for name in values) + " WHERE key=?",
                            [*values.values(), key])

    @staticmethod
    def _row(row) -> dict:
        job = dict(row)
        job["parts"] = json.loads(job["parts"] or "[]")
        job["options"] = json.loads(job["options"] or "{}")
        return job

    def jobs(self) -> dict[str, dict]:
        with self.lock:
            return {row["key"]: self._row(row) for row in self.db.execute("SELECT * FROM jobs ORDER BY created")}

    def job(self, key: str) -> dict | None:
        with self.lock:
            row = self.db.execute("SELECT * FROM jobs WHERE key=?", (key,)).fetchone()
        return self._row(row) if row else None

    def enqueue(self, paper: dict, options: dict) -> bool:
        """Create a job for a paper unless one already exists. Returns True when created."""
        now = time.time()
        with self.lock, self.db:
            cursor = self.db.execute(
                """INSERT OR IGNORE INTO jobs (key,title,creator,pdf_key,pdf_name,md_name,options,created,updated)
                   VALUES (?,?,?,?,?,?,?,?,?)""",
                (paper["key"], paper["title"], paper.get("creator", ""), paper.get("pdf_key", ""), paper.get("pdf_name", ""),
                 paper.get("md_name", ""), json.dumps(options, sort_keys=True), now, now))
            return cursor.rowcount == 1

    def refresh(self, paper: dict) -> None:
        """Keep display fields of not-yet-submitted jobs in step with Zotero."""
        with self.lock, self.db:
            self.db.execute("UPDATE jobs SET title=?,creator=?,md_name=?,pdf_key=?,pdf_name=? WHERE key=? AND stage='queued'",
                            (paper["title"], paper.get("creator", ""), paper.get("md_name", ""),
                             paper.get("pdf_key", ""), paper.get("pdf_name", ""), paper["key"]))
            self.db.execute("UPDATE jobs SET title=?,creator=? WHERE key=?",
                            (paper["title"], paper.get("creator", ""), paper["key"]))

    def remove(self, key: str) -> None:
        with self.lock, self.db:
            self.db.execute("DELETE FROM jobs WHERE key=?", (key,))

    def selected(self, key: str) -> bool:
        with self.lock:
            row = self.db.execute("SELECT enabled FROM selections WHERE key=?", (key,)).fetchone()
        return bool(row and row[0])

    def set_selected(self, key: str, enabled: bool) -> None:
        with self.lock, self.db:
            self.db.execute("INSERT OR REPLACE INTO selections VALUES (?,?)", (key, int(enabled)))

    def retry_failed(self) -> int:
        with self.lock, self.db:
            cursor = self.db.execute("UPDATE jobs SET stage=resume_stage,error='',attempts=0,next_try=0,updated=? "
                                     "WHERE stage='failed'", (time.time(),))
            summaries = self.db.execute("UPDATE summaries SET stage=resume_stage,error='',attempts=0,next_try=0,updated=? "
                                        "WHERE stage='failed'", (time.time(),))
            translations = self.db.execute("UPDATE translations SET stage=resume_stage,error='',attempts=0,next_try=0,"
                                           "updated=? WHERE stage='failed'", (time.time(),))
            return cursor.rowcount + summaries.rowcount + translations.rowcount

    # ------------------------------------------------------------------ AI summaries
    def enqueue_summary(self, key: str, title: str, *, again: bool = False) -> bool:
        """Queue a summary unless one is queued or running (or done, unless `again`: its card was deleted)."""
        now = time.time()
        with self.lock, self.db:
            row = self.db.execute("SELECT stage FROM summaries WHERE key=?", (key,)).fetchone()
            if row and (row["stage"] in ("queued", "attach") or (row["stage"] == "done" and not again)):
                return False
            self.db.execute("INSERT OR REPLACE INTO summaries (key,title,created,updated) VALUES (?,?,?,?)", (key, title, now, now))
            return True

    def summaries(self) -> dict[str, dict]:
        with self.lock:
            rows = self.db.execute("SELECT * FROM summaries ORDER BY created").fetchall()
        result = {}
        for row in rows:
            entry = dict(row)
            entry["card"] = json.loads(entry["card"] or "{}")
            entry["meta"] = json.loads(entry["meta"] or "{}")
            result[entry["key"]] = entry
        return result

    def update_summary(self, key: str, **values) -> None:
        allowed = {"stage", "resume_stage", "file", "result_key", "note_key", "model", "card", "meta",
                   "error", "progress", "attempts", "next_try", "title"}
        if not values.keys() <= allowed:
            raise ValueError("Invalid summary fields")
        for name in ("card", "meta"):
            if name in values and not isinstance(values[name], str):
                values[name] = json.dumps(values[name], ensure_ascii=False)
        values["updated"] = time.time()
        with self.lock, self.db:
            self.db.execute("UPDATE summaries SET " + ",".join(name + "=?" for name in values) + " WHERE key=?",
                            [*values.values(), key])

    def remove_summary(self, key: str) -> None:
        with self.lock, self.db:
            self.db.execute("DELETE FROM summaries WHERE key=?", (key,))

    def cancel_summaries(self, keep: str = "") -> int:
        """Drop queued summaries (the paper being read is still 'queued'); `keep` spares that one."""
        with self.lock, self.db:
            return self.db.execute("DELETE FROM summaries WHERE stage='queued' AND key<>?", (keep,)).rowcount

    # ------------------------------------------------------------------ bilingual PDFs
    def enqueue_translation(self, key: str, title: str) -> bool:
        """Queue a translation unless one is queued or being attached (a finished one is redone: its PDF was deleted)."""
        now = time.time()
        with self.lock, self.db:
            row = self.db.execute("SELECT stage FROM translations WHERE key=?", (key,)).fetchone()
            if row and row["stage"] in ("queued", "attach"):
                return False
            self.db.execute("INSERT OR REPLACE INTO translations (key,title,created,updated) VALUES (?,?,?,?)",
                            (key, title, now, now))
            return True

    def translations(self) -> dict[str, dict]:
        with self.lock:
            rows = self.db.execute("SELECT * FROM translations ORDER BY created").fetchall()
        result = {}
        for row in rows:
            entry = dict(row)
            entry["meta"] = json.loads(entry["meta"] or "{}")
            result[entry["key"]] = entry
        return result

    def update_translation(self, key: str, **values) -> None:
        allowed = {"stage", "resume_stage", "file", "mono", "result_key", "meta", "error", "progress", "attempts",
                   "next_try", "title"}
        if not values.keys() <= allowed:
            raise ValueError("Invalid translation fields")
        if "meta" in values and not isinstance(values["meta"], str):
            values["meta"] = json.dumps(values["meta"], ensure_ascii=False)
        values["updated"] = time.time()
        with self.lock, self.db:
            self.db.execute("UPDATE translations SET " + ",".join(name + "=?" for name in values) + " WHERE key=?",
                            [*values.values(), key])

    def remove_translation(self, key: str) -> None:
        with self.lock, self.db:
            self.db.execute("DELETE FROM translations WHERE key=?", (key,))

    def cancel_translations(self) -> int:
        """Drop queued translations, including the one the engine is working on (it is stopped)."""
        with self.lock, self.db:
            return self.db.execute("DELETE FROM translations WHERE stage='queued'").rowcount

    # ------------------------------------------------------------------ research reviews and tracking
    JSON_FIELDS = {"report", "papers", "info", "files", "runs", "seen", "cache"}

    def _decode(self, row) -> dict:
        entry = dict(row)
        for name in self.JSON_FIELDS & entry.keys():
            entry[name] = json.loads(entry[name] or ("[]" if name in ("papers", "runs", "seen") else "{}"))
        return entry

    def research(self) -> dict[str, dict]:
        with self.lock:
            return {row["key"]: self._decode(row) for row in self.db.execute("SELECT * FROM research")}

    def save_research(self, key: str, **values) -> None:
        allowed = {"name", "path", "fingerprint", "report", "papers", "info", "files", "model", "status", "error", "generated"}
        if not values.keys() <= allowed:
            raise ValueError("Invalid research fields")
        for name in self.JSON_FIELDS & values.keys():
            if not isinstance(values[name], str):
                values[name] = json.dumps(values[name], ensure_ascii=False)
        values["updated"] = time.time()
        with self.lock, self.db:
            self.db.execute("INSERT OR IGNORE INTO research (key) VALUES (?)", (key,))
            self.db.execute("UPDATE research SET " + ",".join(name + "=?" for name in values) + " WHERE key=?",
                            [*values.values(), key])

    def tracking(self, key: str) -> dict:
        with self.lock:
            row = self.db.execute("SELECT * FROM tracking WHERE key=?", (key,)).fetchone()
        return self._decode(row) if row else {"key": key, "runs": [], "seen": [], "cache": {}, "last_run": 0, "error": ""}

    def save_tracking(self, key: str, **values) -> None:
        if not values.keys() <= {"runs", "seen", "cache", "last_run", "error"}:
            raise ValueError("Invalid tracking fields")
        for name in self.JSON_FIELDS & values.keys():
            if not isinstance(values[name], str):
                values[name] = json.dumps(values[name], ensure_ascii=False)
        with self.lock, self.db:
            self.db.execute("INSERT OR IGNORE INTO tracking (key) VALUES (?)", (key,))
            self.db.execute("UPDATE tracking SET " + ",".join(name + "=?" for name in values) + " WHERE key=?",
                            [*values.values(), key])

    def cached(self, key: str, max_age: float) -> dict | None:
        with self.lock:
            row = self.db.execute("SELECT data, updated FROM scholar_cache WHERE id=?", (key,)).fetchone()
        return json.loads(row["data"]) if row and time.time() - row["updated"] <= max_age else None

    def cache(self, key: str, data: dict) -> None:
        with self.lock, self.db:
            self.db.execute("INSERT OR REPLACE INTO scholar_cache VALUES (?,?,?)", (key, json.dumps(data), time.time()))

    def get_value(self, name: str, default: str = "") -> str:
        with self.lock:
            row = self.db.execute("SELECT value FROM kv WHERE name=?", (name,)).fetchone()
        return row["value"] if row else default

    def set_value(self, name: str, value) -> None:
        with self.lock, self.db:
            self.db.execute("INSERT OR REPLACE INTO kv VALUES (?,?)", (name, str(value)))

    def close(self) -> None:
        with self.lock:
            self.db.close()
