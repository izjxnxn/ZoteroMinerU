"""Inspect only publishable files; report locations, never credential values."""
from __future__ import annotations

import argparse
import base64
import io
import json
from pathlib import Path
import re
import sys
import zipfile

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from tools.package_release import source_files  # noqa: E402
from zotero_mineru import __version__  # noqa: E402
from zotero_mineru.resources import WORKER_ARCHIVE  # noqa: E402

TOKEN = re.compile(rb"(?<![A-Za-z0-9_])(?:sk-(?!test|fake|dummy)[A-Za-z0-9_-]{24,}|gh[pousr]_[A-Za-z0-9]{30,}|"
                   rb"github_pat_[A-Za-z0-9_]{40,}|eyJ[A-Za-z0-9_-]{8,}\.eyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{10,})")
PRIVATE_NAMES = {"settings.json", "bridge.json", "job.json", "application.log", "验收报告.md"}


def private_path(name):
    path = Path(name.replace("\\", "/"))
    return (path.name in PRIVATE_NAMES or path.name.startswith(".env")
            or path.suffix.lower() in (".sqlite", ".sqlite3", ".db", ".log", ".key")
            or any(p in (".git", ".venv", ".venv-translate", "__pycache__", "qa", "work") for p in path.parts))


class Audit:
    def __init__(self, secrets=()):
        self.needles = set()
        for secret in secrets:
            if len(secret) >= 8:
                self.needles.update((secret.encode(), secret.encode("utf-16le"), base64.b64encode(secret.encode())))
        self.findings = []
        self.files = 0

    def scan(self, name, data):
        self.files += 1
        if private_path(name):
            self.findings.append({"path": name, "reason": "private file"})
        if any(needle in data for needle in self.needles):
            self.findings.append({"path": name, "reason": "saved local credential found"})
        if re.search(rb"-----BEGIN ([A-Z ]*PRIVATE KEY)-----\r?\n[A-Za-z0-9+/=\r\n]{32,}-----END \1-----", data):
            self.findings.append({"path": name, "reason": "private key found"})
        for match in TOKEN.finditer(data):
            if match.group().startswith(b"sk-") and not re.search(rb"[A-Z0-9]", match.group()):
                continue  # SPDX identifiers contain lowercase sk- license names.
            self.findings.append({"path": name, "reason": "credential-shaped string",
                                  "line": data[:match.start()].count(b"\n") + 1})

    def zip(self, name, data):
        with zipfile.ZipFile(io.BytesIO(data)) as zf:
            bad = zf.testzip()
            if bad:
                raise ValueError("ZIP CRC failed: " + name)
            for info in zf.infolist():
                if info.is_dir():
                    continue
                payload = zf.read(info)
                label = name + "/" + info.filename
                self.scan(label, payload)
                if info.filename.endswith((".zip", ".xpi")):
                    self.zip(label, payload)

    def executable(self, path):
        from PyInstaller.archive.readers import CArchiveReader
        reader = CArchiveReader(str(path))
        for name in reader.toc:
            label = path.name + "!/" + name
            self.scan(label, reader.extract(name))
            if name.endswith(".pyz"):
                pyz = reader.open_embedded_archive(name)
                for module in pyz.toc:
                    data = pyz.extract(module, raw=True)
                    if data:
                        self.scan(label + "/" + module.replace(".", "/") + ".pyc", data)


def local_credentials():
    from zotero_mineru.config import Config
    config = Config()
    if config.load_error:
        raise RuntimeError("无法完整读取本机密钥进行比对：" + config.load_error)
    secrets = [config.token, config.pairing.get("token", ""), *config.llm_keys.values(), *config.scholar_keys.values()]
    if config.path.is_file():
        saved = json.loads(config.path.read_text(encoding="utf-8"))
        secrets.append(saved.get("token_dpapi", ""))
        for key in ("llm_keys_dpapi", "scholar_keys_dpapi"):
            secrets.extend((saved.get(key) or {}).values())
    return [secret for secret in secrets if secret]


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--check-local-config", action="store_true", help="在内存中比对本机已保存密钥，不输出密钥")
    parser.add_argument("--sources-only", action="store_true")
    args = parser.parse_args()
    secrets = local_credentials() if args.check_local_config else []
    audit = Audit(secrets)
    sources = source_files()
    for path in sources:
        audit.scan(path.relative_to(ROOT).as_posix(), path.read_bytes())
    if not args.sources_only:
        dist = ROOT / "dist"
        portable = dist / f"ZoteroMinerU-{__version__}-Windows-x64.zip"
        with zipfile.ZipFile(portable) as zf:
            names = set(zf.namelist())
        expected = {"ZoteroMinerU.exe", "mineru-zotero-bridge.xpi", "LICENSE", "使用说明.md"}
        expected.update("source/" + p.relative_to(ROOT).as_posix() for p in sources)
        if not expected <= names:
            raise ValueError("主程序 ZIP 缺少插件或完整源码。")
        for path in (portable, dist / f"ZoteroMinerU-{__version__}-source.zip", dist / WORKER_ARCHIVE):
            audit.zip(path.name, path.read_bytes())
        audit.executable(dist / "ZoteroMinerU.exe")
        worker = dist / "BabelDocWorker" / "BabelDocWorker.exe"
        if worker.is_file():
            audit.executable(worker)
    result = {"ok": not audit.findings, "source_files": len(sources), "scanned_entries": audit.files,
              "local_credentials_compared": bool(secrets), "findings": audit.findings}
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return int(bool(audit.findings))


if __name__ == "__main__":
    raise SystemExit(main())
