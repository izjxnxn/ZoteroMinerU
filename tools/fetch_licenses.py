"""Populate distributable standard license texts from the official publisher."""
from pathlib import Path
import requests

root = Path(__file__).resolve().parents[1] / "licenses"
root.mkdir(exist_ok=True)
for filename, url in {
    "LGPL-3.0.txt": "https://www.gnu.org/licenses/lgpl-3.0.txt",
    "GPL-3.0.txt": "https://www.gnu.org/licenses/gpl-3.0.txt",
    "GPL-2.0.txt": "https://www.gnu.org/licenses/old-licenses/gpl-2.0.txt",
}.items():
    source_name = {"LGPL-3.0.txt": "LGPL-3.0-only.txt", "GPL-3.0.txt": "GPL-3.0-only.txt", "GPL-2.0.txt": "GPL-2.0-only.txt"}[filename]
    session = requests.Session()
    session.trust_env = False
    try:
        response = session.get("https://raw.githubusercontent.com/qt/qtbase/dev/LICENSES/" + source_name, timeout=(5, 15))
    except requests.RequestException:
        response = requests.get(url, timeout=(5, 15))
    response.raise_for_status()
    if b"GNU" not in response.content[:200]:
        raise RuntimeError("License text not returned")
    (root / filename).write_bytes(response.content)
    print(filename)
