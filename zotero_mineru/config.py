"""User settings and Windows user-bound encryption; no secret in logs."""
from __future__ import annotations

import base64
import copy
import ctypes
import json
import os
import secrets
from pathlib import Path

BRIDGE_PROTOCOL = 5  # what the bundled plugin (2.3) speaks
BRIDGE_TRASH_PROTOCOL = 4  # plugin 2.2: moving attachments/notes to the trash
BRIDGE_TRANSLATION_PROTOCOL = 5  # plugin 2.3: bilingual PDF attachments
BRIDGE_MIN_PROTOCOL = 3  # plugin 2.1: enough for converting and summarising


def atomic_json(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(temporary, path)


def app_home() -> Path:
    override = os.environ.get("MINERU_ZOTERO_HOME")
    return Path(override) if override else Path(os.environ.get("LOCALAPPDATA", Path.home())) / "ZoteroMinerU"


class _Blob(ctypes.Structure):
    _fields_ = [("size", ctypes.c_ulong), ("data", ctypes.POINTER(ctypes.c_ubyte))]


def _dpapi(data: bytes, decrypt: bool = False) -> bytes:
    if os.name != "nt":
        raise RuntimeError("密钥加密仅支持 Windows。")
    buffer = ctypes.create_string_buffer(data)
    source = _Blob(len(data), ctypes.cast(buffer, ctypes.POINTER(ctypes.c_ubyte)))
    target = _Blob()
    crypt = ctypes.WinDLL("crypt32", use_last_error=True)
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel.LocalFree.argtypes = [ctypes.c_void_p]
    kernel.LocalFree.restype = ctypes.c_void_p
    function = crypt.CryptUnprotectData if decrypt else crypt.CryptProtectData
    function.argtypes = [ctypes.POINTER(_Blob), ctypes.c_void_p, ctypes.c_void_p,
                         ctypes.c_void_p, ctypes.c_void_p, ctypes.c_ulong, ctypes.POINTER(_Blob)]
    function.restype = ctypes.c_int
    if not function(ctypes.byref(source), None, None, None, None, 1, ctypes.byref(target)):
        raise ctypes.WinError(ctypes.get_last_error())
    try:
        return ctypes.string_at(target.data, target.size)
    finally:
        kernel.LocalFree(target.data)


DEFAULTS = {
    "collections": [], "include_subcollections": True,
    "model": "vlm", "language": "ch", "ocr": False, "formula": True, "table": True,
    "interval": 60, "zotero_port": 23119,
    # import: 存入 Zotero 存储（随文库同步）；link: 文件集中保存在输出目录，Zotero 中为链接附件。
    "storage_mode": "import", "export_copy": False,
    "output_root": str(Path.home() / "Documents" / "ZoteroMarkdown"),
    "resume_switch": False,
    # AI 论文总结：服务商、各服务商的接口地址与模型（API Key 另行加密保存）。
    "llm_provider": "deepseek", "llm_bases": {}, "llm_models": {},
    "summary_language": "zh", "summary_auto": True, "summary_note": True,
    # deep: 逐段精读全文 → 综合撰写 → 核对补全；quick: 单次通读。
    # 0 = 自动：不自行限制输入/输出长度，按模型上下文与输出上限处理；超长论文才分段（每段 summary_part_tokens）。
    "summary_depth": "deep", "summary_part_tokens": 30000,
    "summary_budget": 0, "summary_max_output": 0,
    # AI 总结正文字数上限（中文按字计，英文单词和数字按词计）。
    "summary_max_words": 3000,
    # 看图：把论文中的图片随全文一起发给支持看图的模型（不支持的模型自动只读文字）。
    "summary_vision": False, "summary_vision_max": 30,
    # 研究综述与新文献追踪：总开关；综述范围（勾选的分类及其全部子分类）；服务商/模型留空表示与 AI 总结相同。
    "research_enabled": False, "research_roots": [], "research_provider": "", "research_model": "",
    "research_interval_days": 7, "research_reread": 6, "research_html": True,
    "tracking_enabled": True, "tracking_interval_days": 7, "tracking_window_days": 180,
    "tracking_budget": 15, "tracking_min_score": 6,
    # 保留排版翻译（双语对照 PDF，译左原右）：服务商/模型留空表示与 AI 总结相同；qps = 同时发出的翻译请求数。
    "translate_provider": "", "translate_model": "", "translate_qps": 8, "translate_font": "serif",
    "translate_keep_refs": False, "translate_auto_terms": True, "translate_mono": False,
    "settings_version": 3,
}


class Config:
    def __init__(self, home: Path | None = None):
        self.home = Path(home or app_home()).resolve()
        self.home.mkdir(parents=True, exist_ok=True)
        self.work_dir = self.home / "work"
        self.path = self.home / "settings.json"
        self.data = copy.deepcopy(DEFAULTS)
        self.token = ""
        self.llm_keys: dict[str, str] = {}
        # Optional keys for the scholarly databases used by new-paper tracking: {"s2": …, "openalex": …}.
        self.scholar_keys: dict[str, str] = {}
        self.load_error = ""
        if self.path.exists():
            try:
                saved = json.loads(self.path.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                saved = {}
                self.load_error = "设置文件损坏，已恢复默认设置，请重新填写并保存。"
            self.data.update({key: value for key, value in saved.items() if key in DEFAULTS})
            if saved.get("settings_version", 0) < 3:
                # 2.1 capped output at 8192 tokens, which truncated thinking models; lengths are automatic now.
                self.data.update(summary_budget=0, summary_max_output=0, summary_part_tokens=30000, settings_version=3)
            if saved.get("token_dpapi"):
                try:
                    self.token = _dpapi(base64.b64decode(saved["token_dpapi"]), True).decode("utf-8")
                except (OSError, ValueError, RuntimeError):
                    self.load_error = "无法解密原 Token，请在设置中重新填写并保存。"
            for provider, blob in (saved.get("llm_keys_dpapi") or {}).items():
                try:
                    self.llm_keys[provider] = _dpapi(base64.b64decode(blob), True).decode("utf-8")
                except (OSError, ValueError, RuntimeError):
                    self.load_error = "无法解密大模型 API Key，请在设置中重新填写并保存。"
            for name, blob in (saved.get("scholar_keys_dpapi") or {}).items():
                try:
                    self.scholar_keys[name] = _dpapi(base64.b64decode(blob), True).decode("utf-8")
                except (OSError, ValueError, RuntimeError):
                    self.load_error = "无法解密文献数据库 API Key，请在研究综述页重新填写并保存。"
        if self.data["storage_mode"] not in ("import", "link"):
            self.data["storage_mode"] = "import"
        pairing_path = self.home / "bridge.json"
        try:
            self.pairing = json.loads(pairing_path.read_text(encoding="utf-8")) if pairing_path.exists() else {}
        except (OSError, ValueError):
            self.pairing = {}
        if len(self.pairing.get("token", "")) < 32:
            self.pairing["token"] = secrets.token_urlsafe(32)

    def allowed_roots(self) -> list[str]:
        return list(dict.fromkeys([str(self.work_dir.resolve()), str(Path(self.data["output_root"]).resolve())]))

    def save(self) -> None:
        saved = dict(self.data)
        saved["token_dpapi"] = base64.b64encode(_dpapi(self.token.encode("utf-8"))).decode("ascii") if self.token else ""
        saved["llm_keys_dpapi"] = {provider: base64.b64encode(_dpapi(key.encode("utf-8"))).decode("ascii")
                                   for provider, key in self.llm_keys.items() if key}
        saved["scholar_keys_dpapi"] = {name: base64.b64encode(_dpapi(key.encode("utf-8"))).decode("ascii")
                                       for name, key in self.scholar_keys.items() if key}
        atomic_json(self.path, saved)
        # The Zotero bridge reads this file to authenticate requests and limit which folders it may attach from.
        self.pairing.pop("output_roots", None)
        self.pairing.update({"allowed_roots": self.allowed_roots(), "protocol": BRIDGE_PROTOCOL})
        atomic_json(self.home / "bridge.json", self.pairing)
