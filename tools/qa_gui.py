from pathlib import Path
import os
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
os.environ["QT_QPA_PLATFORM"] = "offscreen"
from zotero_mineru.config import Config

home = ROOT / "qa/gui-state"
config = Config(home)
config.data.update({"collections": ["VIC9NVX2"], "research_roots": ["VIC9NVX2"], "output_root": str(ROOT / "qa/gui-output"),
                    "resume_switch": False})
config.save()
sys.argv = [str(ROOT / "main.py"), "--data-dir", str(home), "--smoke-test", str(ROOT / "qa/gui.png")]
from main import main
raise SystemExit(main())
