"""Download the pinned translation engine without distributing personal settings."""
from __future__ import annotations

import hashlib
from pathlib import Path, PurePosixPath
import stat
import tempfile
import threading
import zipfile

import requests

from .config import app_home

REPOSITORY = "izjxnxn/ZoteroMinerU"
WORKER_VERSION = "2.8.4"
WORKER_ARCHIVE = f"BabelDocWorker-{WORKER_VERSION}-Windows-x64.zip"
WORKER_SHA256 = "a6b3d9bbe50772e4b18530c6b9e190a5761df875748a45f251dbdaef959ffa63"
RELEASE_URL = f"https://github.com/{REPOSITORY}/releases/tag/v{WORKER_VERSION}"
WORKER_URL = f"https://github.com/{REPOSITORY}/releases/download/v{WORKER_VERSION}/{WORKER_ARCHIVE}"
MAX_DOWNLOAD = 512 * 1024 * 1024
MAX_UNPACKED = 2 * 1024 * 1024 * 1024


def installed_worker(home: Path | None = None) -> Path:
    return Path(home or app_home()) / "engines" / WORKER_VERSION / "BabelDocWorker" / "BabelDocWorker.exe"


def unpack_worker(archive: Path, home: Path, expected_hash: str, cancelled: threading.Event, progress=None) -> Path:
    """Validate before extraction, then publish a complete directory in one rename."""
    with archive.open("rb") as stream:
        if hashlib.file_digest(stream, "sha256").hexdigest() != expected_hash:
            raise ValueError("翻译资源包 SHA-256 校验失败，请重新下载。")
    target = installed_worker(home).parent.parent
    target.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix=".unpack-", dir=target.parent) as folder:
        staging = Path(folder) / "new"
        staging.mkdir()
        with zipfile.ZipFile(archive) as zf:
            entries = zf.infolist()
            total = sum(info.file_size for info in entries)
            if len(entries) > 20000 or total > MAX_UNPACKED:
                raise ValueError("翻译资源包文件数量或解压大小超出上限。")
            names = set()
            extracted, last_percent = 0, -1
            for info in entries:
                path = PurePosixPath(info.filename)
                name = info.filename.rstrip("/").casefold()
                if (not path.parts or path.parts[0] != "BabelDocWorker" or path.is_absolute()
                        or ".." in path.parts or "\\" in info.orig_filename or ":" in info.orig_filename
                        or name in names or stat.S_ISLNK(info.external_attr >> 16)):
                    raise ValueError("翻译资源包包含不安全的文件路径。")
                names.add(name)
                if cancelled.is_set():
                    raise InterruptedError("已取消下载。")
                zf.extract(info, staging)
                extracted += info.file_size
                percent = min(99, extracted * 100 // max(1, total))
                if progress and percent != last_percent:
                    last_percent = percent
                    progress({"phase": "install", "text": "正在解压并配置翻译资源包…", "percent": percent})
        if not (staging / "BabelDocWorker" / "BabelDocWorker.exe").is_file() or not (
                staging / "BabelDocWorker" / "_internal" / "python312.dll").is_file():
            raise ValueError("翻译资源包不完整，缺少引擎或 Python 运行库。")
        if cancelled.is_set():
            raise InterruptedError("已取消下载。")
        if progress:
            progress({"phase": "install", "text": "正在完成翻译引擎配置…", "percent": 99})
        previous = Path(folder) / "previous"
        if target.exists():
            target.rename(previous)
        try:
            staging.rename(target)
        except OSError:
            if previous.exists():
                previous.rename(target)
            raise
    return installed_worker(home)


def download_worker(home: Path, progress, cancelled: threading.Event) -> Path:
    cache = Path(home) / "engines"
    cache.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix=".download-", dir=cache) as folder:
        archive = Path(folder) / WORKER_ARCHIVE
        progress({"phase": "download", "text": "正在连接 GitHub 下载翻译资源包…", "percent": None})
        with requests.get(WORKER_URL, stream=True, timeout=(15, 30),
                          headers={"User-Agent": "ZoteroMinerU"}) as response:
            response.raise_for_status()
            total = int(response.headers.get("Content-Length", 0))
            if total > MAX_DOWNLOAD:
                raise ValueError("翻译资源包下载大小超出上限。")
            received, last_marker = 0, -1
            with archive.open("wb") as stream:
                for chunk in response.iter_content(1024 * 1024):
                    if cancelled.is_set():
                        raise InterruptedError("已取消下载。")
                    received += len(chunk)
                    if received > MAX_DOWNLOAD:
                        raise ValueError("翻译资源包下载大小超出上限。")
                    stream.write(chunk)
                    mb = received // (1024 * 1024)
                    percent = min(100, received * 100 // total) if total else None
                    marker = percent if total else mb
                    if marker != last_marker:
                        last_marker = marker
                        suffix = f" / {total / 1024 / 1024:.0f} MB" if total else " MB"
                        progress({"phase": "download", "text": f"下载翻译资源包：{mb}{suffix}", "percent": percent})
        progress({"phase": "verify", "text": "正在校验翻译资源包…", "percent": None})
        return unpack_worker(archive, home, WORKER_SHA256, cancelled, progress)
