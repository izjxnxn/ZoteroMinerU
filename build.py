"""Build the XPI, a windowed single-file Windows executable and (with --worker) the BabelDOC translation engine."""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import zipfile


ROOT = Path(__file__).resolve().parent


def build_bridge():
    resources = ROOT / "resources"
    resources.mkdir(exist_ok=True)
    target = resources / "mineru-zotero-bridge.xpi"
    with zipfile.ZipFile(target, "w", zipfile.ZIP_DEFLATED) as zf:
        for filename in ("manifest.json", "bootstrap.js", "bridge.js"):
            zf.write(ROOT / "bridge" / filename, filename)
    return target


def build_worker():
    """dist/BabelDocWorker: the translation engine as its own console program (run hidden by the main program).

    It is built from .venv-translate, whose packages are pinned in translate_worker/requirements-lock.txt, because
    BabelDOC breaks with newer releases of some dependencies (bitstring 5 for BabelDOC 0.6.4)."""
    python = ROOT / ".venv-translate" / "Scripts" / "python.exe"
    if not python.is_file():
        raise RuntimeError("先按 translate_worker/requirements-lock.txt 创建 .venv-translate（见 README.md）")
    work = ROOT / "build" / "worker"
    command = [str(python), "-m", "PyInstaller", "--noconfirm", "--clean", "--onedir", "--console",
               "--name", "BabelDocWorker", "--distpath", str(ROOT / "dist"), "--workpath", str(work),
               "--specpath", str(work), "--paths", str(ROOT), "--icon", str(ROOT / "resources" / "app.ico"),
               "--additional-hooks-dir", str(ROOT / "translate_worker" / "pyinstaller_hooks"),
               "--hidden-import", "tiktoken_ext.openai_public", "--hidden-import", "zotero_mineru.llm"]
    for package in ("babeldoc", "freetype", "rtree", "hyperscan", "tiktoken_ext"):
        command += ["--collect-all", package]
    for unused in ("tkinter", "matplotlib", "IPython", "pytest", "pymupdf.layout", "pymupdf4llm"):
        command += ["--exclude-module", unused]
    subprocess.run([*command, str(ROOT / "translate_worker" / "worker.py")], cwd=ROOT, check=True)
    collect_worker_licenses(python)
    from zotero_mineru.resources import WORKER_ARCHIVE
    (ROOT / "dist" / WORKER_ARCHIVE).unlink(missing_ok=True)
    return ROOT / "dist" / "BabelDocWorker" / "BabelDocWorker.exe"


def collect_worker_licenses(python: Path):
    """Copy every bundled package's license files to dist/BabelDocWorker/licenses/<package>/."""
    script = r'''
import shutil, sys
from importlib import metadata
from pathlib import Path
target = Path(sys.argv[1])
shutil.rmtree(target, ignore_errors=True)
for distribution in metadata.distributions():
    name = distribution.metadata["Name"]
    for filename in distribution.files or []:
        path = str(filename).replace("\\", "/").lower()
        if "/licenses/" in path or path.rsplit("/", 1)[-1].startswith(("license", "licence", "copying", "notice")):
            source = Path(distribution.locate_file(filename))
            if source.is_file() and ".dist-info" in str(filename):
                (target / name).mkdir(parents=True, exist_ok=True)
                shutil.copy2(source, target / name / source.name)
'''
    subprocess.run([str(python), "-I", "-c", script, str(ROOT / "dist" / "BabelDocWorker" / "licenses")], check=True)
    shutil.copy2(ROOT / "translate_worker" / "requirements-lock.txt", ROOT / "dist" / "BabelDocWorker" / "requirements-lock.txt")


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--bridge-only", action="store_true")
    parser.add_argument("--worker", action="store_true", help="同时重新打包翻译引擎 BabelDocWorker（较慢）")
    args = parser.parse_args()
    xpi = build_bridge()
    print("Bridge:", xpi, flush=True)
    if args.bridge_only:
        return
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    from PySide6.QtWidgets import QApplication
    from zotero_mineru.gui import app_icon
    app = QApplication([])
    icon = ROOT / "resources" / "app.ico"
    if not app_icon().pixmap(64, 64).save(str(icon), "ICO"):
        raise RuntimeError("Could not create executable icon")
    command = [sys.executable, "-m", "PyInstaller", "--noconfirm", "--clean", "--onefile", "--windowed",
        "--name", "ZoteroMinerU", "--icon", str(icon),
        "--add-data", str(ROOT / "resources") + os.pathsep + "resources", str(ROOT / "main.py")]
    # Other document tools on PATH can supply incompatible ICU DLLs to dependency scanning.
    import PySide6
    import shiboken6
    windows = Path(os.environ.get("WINDIR", "C:/Windows"))
    build_env = dict(os.environ)
    build_env["PATH"] = os.pathsep.join(str(p) for p in (
        Path(sys.executable).parent, Path(sys.base_prefix), Path(PySide6.__file__).parent,
        Path(shiboken6.__file__).parent, windows / "System32", windows))
    subprocess.run(command, cwd=ROOT, env=build_env, check=True)
    if args.worker:
        print("Translation engine:", build_worker(), flush=True)
    delivery = ROOT / "dist"
    shutil.copy2(xpi, delivery / xpi.name)
    shutil.copy2(ROOT / "使用说明.md", delivery / "使用说明.md")
    shutil.copy2(ROOT / "THIRD_PARTY_NOTICES.md", delivery / "THIRD_PARTY_NOTICES.md")
    subprocess.run([sys.executable, str(ROOT / "tools/package_release.py")], cwd=ROOT, check=True)
    print("Executable:", delivery / "ZoteroMinerU.exe", flush=True)


if __name__ == "__main__":
    main()
