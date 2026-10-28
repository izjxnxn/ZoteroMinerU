"""Package an explicit software allowlist, never configuration, logs or QA libraries."""
from __future__ import annotations

import hashlib
from importlib import metadata
import json
from pathlib import Path
import shutil
import sys
import zipfile

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from zotero_mineru import __version__  # noqa: E402
from zotero_mineru.resources import WORKER_ARCHIVE, WORKER_SHA256  # noqa: E402

SOURCE_ROOT_FILES = ("main.py", "build.py", "README.md", "使用说明.md", "THIRD_PARTY_NOTICES.md",
                     "LICENSE", "SECURITY.md", "RELEASE_NOTES.md", "requirements.txt", "requirements-lock.txt",
                     ".gitignore", ".gitattributes")
SOURCE_FOLDERS = ("zotero_mineru", "bridge", "tests", "tools", "licenses", "translate_worker")


def source_files(root=ROOT):
    sources = [root / name for name in SOURCE_ROOT_FILES if (root / name).is_file()]
    for name in SOURCE_FOLDERS:
        sources.extend(p for p in (root / name).rglob("*") if p.is_file() and "__pycache__" not in p.parts
                       and p.suffix.lower() in (".py", ".js", ".json", ".txt", ".md"))
    return sorted(sources)


def digest(path):
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def main():
    dist = ROOT / "dist"
    dependency_lines = sorted(f"{d.metadata['Name']}=={d.version}" for d in metadata.distributions())
    (ROOT / "requirements-lock.txt").write_text("\n".join(dependency_lines) + "\n", encoding="utf-8")
    for name in ("使用说明.md", "THIRD_PARTY_NOTICES.md", "LICENSE", "RELEASE_NOTES.md"):
        shutil.copy2(ROOT / name, dist / name)
    license_dir = dist / "licenses"
    license_dir.mkdir(exist_ok=True)
    for source in license_dir.rglob("*"):
        if source.is_file() and source.suffix in (".py", ".pyc"):
            source.unlink()
    for source in (ROOT / "licenses").glob("*.txt"):
        shutil.copy2(source, license_dir / source.name)
    python_license = Path(sys.base_prefix) / "LICENSE.txt"
    if python_license.is_file():
        shutil.copy2(python_license, license_dir / "Python-LICENSE.txt")
    for distribution in metadata.distributions():
        for filename in distribution.files or []:
            if ".dist-info/licenses/" in str(filename).replace("\\", "/"):
                source = Path(distribution.locate_file(filename))
                if source.is_file():
                    target = license_dir / distribution.metadata["Name"] / source.name
                    target.parent.mkdir(exist_ok=True)
                    shutil.copy2(source, target)
    engine_zip = dist / WORKER_ARCHIVE
    if not engine_zip.exists() and (dist / "BabelDocWorker").is_dir():
        with zipfile.ZipFile(engine_zip, "w", zipfile.ZIP_DEFLATED, compresslevel=6) as zf:
            for source in sorted((dist / "BabelDocWorker").rglob("*")):
                if source.is_file():
                    zf.write(source, source.relative_to(dist))
    if engine_zip.exists() and digest(engine_zip) != WORKER_SHA256:
        raise RuntimeError(f"资源包 SHA-256 为 {digest(engine_zip)}，请核对版本并更新 resources.py 后重新构建。")
    sources = source_files()
    archive = dist / f"ZoteroMinerU-{__version__}-source.zip"
    with zipfile.ZipFile(archive, "w", zipfile.ZIP_DEFLATED) as zf:
        for source in sources:
            zf.write(source, source.relative_to(ROOT))
    payload = [dist / name for name in ("ZoteroMinerU.exe", "mineru-zotero-bridge.xpi", "使用说明.md",
                                       "THIRD_PARTY_NOTICES.md", "LICENSE", "RELEASE_NOTES.md")]
    payload.extend(p for p in license_dir.rglob("*") if p.is_file())
    checksums = {p.relative_to(dist).as_posix(): digest(p) for p in payload}
    checksums.update({"source/" + p.relative_to(ROOT).as_posix(): digest(p) for p in sources})
    checksum_file = dist / "SHA256.json"
    checksum_file.write_text(json.dumps(checksums, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    portable = dist / f"ZoteroMinerU-{__version__}-Windows-x64.zip"
    with zipfile.ZipFile(portable, "w", zipfile.ZIP_DEFLATED) as zf:
        for source in [*payload, checksum_file]:
            zf.write(source, source.relative_to(dist))
        for source in sources:
            zf.write(source, "source/" + source.relative_to(ROOT).as_posix())
    assets = [portable, archive, dist / "mineru-zotero-bridge.xpi"]
    if engine_zip.exists():
        assets.append(engine_zip)
    (dist / "SHA256SUMS.txt").write_text("".join(f"{digest(p)}  {p.name}\n" for p in assets), encoding="utf-8")
    print(json.dumps({"portable_zip": str(portable), "source_zip": str(archive),
                      "worker_zip": str(engine_zip), "source_files": len(sources)}, ensure_ascii=False), flush=True)


if __name__ == "__main__":
    main()
