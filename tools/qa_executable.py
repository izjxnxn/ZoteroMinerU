"""Launch the packaged executable using Windows Qt, without uploading documents."""
from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from zotero_mineru.config import Config


def main():
    home = ROOT / "qa" / "exe-state"
    image = ROOT / "qa" / "exe-native.png"
    config = Config(home)
    config.token = ""
    config.data.update({"collections": ["VIC9NVX2"], "research_roots": ["VIC9NVX2"], "output_root": str(ROOT / "qa/exe-output"),
                        "resume_switch": False})
    config.save()
    env = dict(os.environ, QT_QPA_PLATFORM="windows")
    startup = subprocess.STARTUPINFO()
    startup.dwFlags |= subprocess.STARTF_USESHOWWINDOW
    startup.wShowWindow = 0
    process = subprocess.Popen([str(ROOT / "dist/ZoteroMinerU.exe"), "--data-dir", str(home),
        "--smoke-test", str(image)], env=env, startupinfo=startup)
    code = process.wait(timeout=45)
    if code:
        raise RuntimeError(f"Executable failed with exit code {code}")
    data = json.loads(image.with_suffix(".json").read_text(encoding="utf-8"))
    assert data["packaged_executable"]
    assert data["embedded_xpi"]
    assert data["token_masked"] == "Password"
    assert data["pdf_rows"] > 0
    print(json.dumps(data, ensure_ascii=False, indent=2), flush=True)


if __name__ == "__main__":
    main()
