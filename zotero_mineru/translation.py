"""Background queue that turns a paper's PDF into a layout-preserving bilingual PDF and attaches it to the paper.

The BabelDOC engine runs as a separate program (BabelDocWorker, see translate_worker/worker.py): it keeps formulas,
figures and graphics of the original page and re-typesets only the paragraphs, translated by the user's own model.
Each page of the result holds the translation on the left and the original on the right (双语_作者_论文名称.pdf).
"""
from __future__ import annotations

import copy
import json
import os
import queue
import shutil
import subprocess
import sys
import threading
import time
from pathlib import Path

from .clients import (BILINGUAL_PREFIX, MONO_PREFIX, ServiceError, ZoteroClient, is_bilingual_attachment,
                      is_markdown_attachment, is_translated_pdf, safe_message)
from .config import BRIDGE_TRANSLATION_PROTOCOL
from .llm import PROVIDERS
from .markdown import fallback_base, page_count, unique_path, valid_filename
from .resources import installed_worker

GLOSSARY = "翻译术语表.csv"
OLD_PLUGIN = "Zotero 中的插件需要更新到 2.3（设置页 → 安装/更新 Zotero 插件），更新后自动继续"
NO_ENGINE = "没有找到翻译引擎 BabelDocWorker，请在“设置 → 保留排版翻译”点击“下载翻译资源包”。"
# Input + output per page including term extraction, for the cost hint (measured: DeepSeek, Ambati 2022, 25 pages).
TOKENS_PER_PAGE = 8000


def pdf_filename(base: str, prefix: str = BILINGUAL_PREFIX) -> str:
    """双语_作者_论文名称.pdf — base is Zotero's '{firstCreator}_{title}' rendering, as for MD_ and SUM_ files."""
    stem = valid_filename(prefix + base.strip().strip("_"))[:180].rstrip(" .")
    return stem + ".pdf"


def engine_command() -> list[str] | None:
    """How to start the engine: BabelDocWorker beside this program, else a development build or environment."""
    override = os.environ.get("MINERU_ZOTERO_TRANSLATOR")
    if override:
        return json.loads(override)
    root = Path(__file__).resolve().parents[1]
    candidates = [[installed_worker()]]
    if getattr(sys, "frozen", False):
        candidates.append([Path(sys.executable).parent / "BabelDocWorker" / "BabelDocWorker.exe"])
    candidates += [[root / "dist" / "BabelDocWorker" / "BabelDocWorker.exe"],
                   [root / ".venv-translate" / "Scripts" / "python.exe", root / "translate_worker" / "worker.py"]]
    for command in candidates:
        if all(part.is_file() for part in command):
            return [str(part) for part in command]
    return None


def translation_model(settings: dict) -> tuple[str, str, str]:
    """(provider, model, base URL) for translating: its own choice, or the AI-summary model."""
    provider = settings.get("translate_provider") or settings["llm_provider"]
    if provider not in PROVIDERS:
        provider = settings["llm_provider"]
    models = PROVIDERS[provider][3]
    model = (settings.get("translate_model") if settings.get("translate_provider") else "") \
        or settings["llm_models"].get(provider) or (models[0] if models else "")
    return provider, model, settings["llm_bases"].get(provider, "")


def outcome(result: dict) -> tuple[str, list[str]]:
    """Summary line and the warnings the user should look at, from the engine's report."""
    usage, paragraphs = result.get("usage", {}), result.get("paragraphs", {})
    minutes = max(1, round(result.get("seconds", 0) / 60))
    line = (f"{result.get('pages', 0)} 页，{usage.get('calls', 0)} 次调用，输入约 {usage.get('input', 0)} / "
            f"输出 {usage.get('output', 0)} tokens，用时约 {minutes} 分钟")
    warnings = []
    if result.get("missing_glyphs"):
        warnings.append("字体缺字：" + "".join(result["missing_glyphs"][:10]))
    if result.get("broken_chars"):
        warnings.append(f"{result['broken_chars']} 个无法识别的字符")
    if result.get("untranslated_pages"):
        warnings.append("第 " + "、".join(map(str, result["untranslated_pages"][:12])) + " 页疑似没有翻译")
    if result.get("glossary_error"):
        warnings.append(result["glossary_error"])
    if usage.get("failed"):
        warnings.append(f"{usage['failed']} 次大模型调用失败（{result.get('last_error', '')[:80]}），失败的段落保留原文")
    total, fallback = paragraphs.get("total", 0), paragraphs.get("fallback", 0)
    if total and fallback / total > 0.1:
        warnings.append(f"{fallback}/{total} 段经过单段重译")
    return line, warnings


class Translator(threading.Thread):
    def __init__(self, store, settings: dict, keys: dict, bridge_token: str, work_dir: Path, home: Path, emit,
                 *, zotero_factory=ZoteroClient, command=engine_command):
        super().__init__(name="zotero-mineru-translator", daemon=True)
        self.store, self.emit = store, emit
        self.settings, self.keys = copy.deepcopy(settings), dict(keys)
        self.bridge_token = bridge_token
        self.work_dir = Path(work_dir) / "translations"
        self.glossary = Path(home) / GLOSSARY
        self.zotero_factory, self.command_factory = zotero_factory, command
        self.zotero = zotero_factory(self.settings["zotero_port"], bridge_token)
        self.commands = queue.Queue()
        self.stopping = threading.Event()
        self.cancelled = threading.Event()
        self.process: subprocess.Popen | None = None
        self.outdated_warned = False

    def command(self, action: str, data=None):
        if action == "cancel":
            self.cancelled.set()  # stop the running engine at once; the queue itself is cleared by the thread
            self._kill()
        self.commands.put((action, data))

    def close(self):
        self.stopping.set()
        self._kill()
        self.commands.put(("stop", None))

    def _kill(self):
        process = self.process
        if process and process.poll() is None:
            process.kill()

    def _secret(self) -> str:
        return self.keys.get(translation_model(self.settings)[0], "")

    def log(self, value):
        self.emit("log", safe_message(value, self._secret()))

    def run(self):
        while not self.stopping.is_set():
            try:
                action, data = self.commands.get(timeout=0.5)
                if action == "stop":
                    break
                self.handle(action, data)
            except queue.Empty:
                pass
            except Exception as error:
                self.emit("translate_check", safe_message(str(error), self._secret()))
            try:
                self.process_one()
            except Exception as error:
                self.log("翻译队列错误：" + str(error))

    def handle(self, action: str, data):
        if action == "configure":
            self.settings, self.keys = copy.deepcopy(data[0]), dict(data[1])
            self.zotero = self.zotero_factory(self.settings["zotero_port"], self.bridge_token)
        elif action == "enqueue":
            self.cancelled.clear()
            count = sum(self.store.enqueue_translation(key, title) for key, title in data)
            provider, model, _ = translation_model(self.settings)
            self.log(f"已将 {count} 篇论文加入保留排版翻译队列（{PROVIDERS[provider][0]} · {model}）。")
            self.emit("translations", None)
        elif action == "cancel":
            count = self.store.cancel_translations()
            self.cancelled.clear()
            self.log(f"已停止翻译，取消 {count} 篇（包括正在翻译的一篇）。")
            self.emit("translations", None)
        elif action == "check":
            self.emit("translate_check", self.check_engine())

    def check_engine(self) -> str:
        command = self.command_factory()
        if not command:
            return NO_ENGINE
        try:
            completed = subprocess.run([*command, "--check"], capture_output=True, timeout=1800,
                                       creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        except (OSError, subprocess.TimeoutExpired) as error:
            return "翻译引擎无法启动：" + str(error)
        for line in reversed(completed.stdout.decode("utf-8", "replace").splitlines()):
            try:
                event = json.loads(line)
            except ValueError:
                continue
            if event.get("type") == "ready":
                return f"翻译引擎正常：BabelDOC {event.get('babeldoc', '')}，版面模型和字体（{event.get('fonts', 0)} 个）已就绪。"
            if event.get("type") == "error":
                return "翻译引擎检查失败：" + event.get("message", "")
        return f"翻译引擎检查失败（退出代码 {completed.returncode}）。"

    def process_one(self):
        if self.stopping.is_set():
            return
        now = time.time()
        jobs = [j for j in self.store.translations().values() if j["stage"] in ("queued", "attach") and j["next_try"] <= now]
        if not jobs:
            return
        job = min(jobs, key=lambda j: (j["stage"] != "attach", j["created"]))
        try:
            self.advance(job)
        except Exception as error:
            current = self.store.translations().get(job["key"])
            if self.cancelled.is_set() and job["stage"] == "queued":
                self.store.remove_translation(job["key"])
                return
            if not current or current["stage"] not in ("queued", "attach") or self.stopping.is_set():
                return  # cancelled (or the program is closing): nothing to report
            attempt = job["attempts"] + 1
            message = safe_message(str(error) or type(error).__name__, self._secret())
            if isinstance(error, ServiceError) and error.retryable and attempt <= 3:
                self.store.update_translation(job["key"], attempts=attempt, error=message, next_try=time.time() + 60 * attempt)
            else:
                self.store.update_translation(job["key"], stage="failed", resume_stage=job["stage"], error=message,
                                              attempts=attempt)
            self.log(f"保留排版翻译失败：{job['title']}：{message}")
        finally:
            self.emit("translations", None)

    def _progress(self, key: str, text: str):
        self.store.update_translation(key, progress=text)
        self.emit("translations", None)

    def _plugin_ready(self, key: str, done: str = "") -> bool:
        """Only adding the PDF to Zotero needs plugin 2.3; translating never waits for it."""
        try:
            info = self.zotero.bridge("status")
        except ServiceError as error:
            self.store.update_translation(key, progress=done + "等待连接 Zotero：" + str(error), next_try=time.time() + 30)
            return False
        if info.get("protocol", 0) >= BRIDGE_TRANSLATION_PROTOCOL:
            return True
        self.store.update_translation(key, progress=done + OLD_PLUGIN, next_try=time.time() + 30)
        if not self.outdated_warned:
            self.outdated_warned = True
            self.log("双语 PDF 已生成，但 " + OLD_PLUGIN)
            self.emit("translation_plugin_outdated", info.get("version") or "旧")
        return False

    def _children(self, key: str) -> list[dict]:
        return [c for c in self.zotero.children(key)
                if c["data"].get("itemType") == "attachment" and not c["data"].get("deleted")]

    def _source_pdf(self, key: str, children: list[dict]) -> tuple[Path, str]:
        """The paper's original PDF on this computer (never a translation) and its file name."""
        originals = [c for c in children if c["data"].get("contentType") == "application/pdf"
                     and c["data"].get("linkMode") != "linked_url" and not is_translated_pdf(c["data"])]
        if not originals:
            raise ServiceError("条目下没有原文 PDF（只有译文或没有 PDF）。")
        url = self.zotero.item(key).get("data", {}).get("url", "")
        originals.sort(key=lambda c: (not (url and c["data"].get("url") == url), c["data"].get("dateAdded", "")))
        child = originals[0]
        path = self.zotero.file_path(child["key"], child.get("links", {}).get("enclosure", {}).get("href", ""))
        if not path.is_file():
            raise ServiceError("PDF 暂不在本机（可在 Zotero 中双击条目下载）。", retryable=True)
        with path.open("rb") as stream:
            if b"%PDF-" not in stream.read(1024):
                raise ServiceError("附件内容不是有效的 PDF 文件。")
        return path, child["data"].get("filename") or path.name

    def advance(self, job: dict):
        if job["stage"] == "queued":
            self.translate(job)
        elif job["stage"] == "attach":
            self.attach(job)

    def translate(self, job: dict):
        key = job["key"]
        provider, model, base = translation_model(self.settings)
        secret = self.keys.get(provider, "")
        if not secret and provider != "ollama":
            self.store.update_translation(key, progress="等待在设置中填写大模型 API Key", next_try=time.time() + 30)
            return
        command = self.command_factory()
        if not command:
            raise ServiceError(NO_ENGINE)
        item = self.zotero.item(key)
        children = self._children(key)
        if any(is_bilingual_attachment(c["data"]) for c in children):
            self.store.update_translation(key, stage="done", progress="条目已有双语 PDF，跳过")
            self.emit("translation_attached", key)  # the list may not know this PDF yet
            return
        source, source_name = self._source_pdf(key, children)
        data = item.get("data", {})
        creator = item.get("meta", {}).get("creatorSummary", "")
        try:
            base_name = self.zotero.bridge("names", keys=[key]).get("names", {}).get(key) or ""
        except ServiceError:
            base_name = ""
        base_name = base_name or fallback_base(creator, data.get("title", ""), source_name)
        folder = self.work_dir / key
        shutil.rmtree(folder, ignore_errors=True)
        (folder / "out").mkdir(parents=True)
        shutil.copy2(source, folder / "source.pdf")  # Zotero's file stays untouched and unlocked
        pages = page_count(folder / "source.pdf") or 0
        request = {"input": str(folder / "source.pdf"), "output_dir": str(folder / "out"), "work_dir": str(folder / "tmp"),
                   "name": pdf_filename(base_name), "mono_name": pdf_filename(base_name, MONO_PREFIX),
                   "mono": self.settings.get("translate_mono", False), "provider": provider, "model": model, "base": base,
                   "lang_in": "en", "lang_out": "zh", "qps": self.settings.get("translate_qps", 8),
                   "font": self.settings.get("translate_font", "serif"),
                   "keep_references": self.settings.get("translate_keep_refs", False),
                   "auto_terms": self.settings.get("translate_auto_terms", True),
                   "glossary": str(self.glossary) if self.glossary.is_file() else "", "page_count": pages}
        (folder / "job.json").write_text(json.dumps(request, ensure_ascii=False, indent=2), encoding="utf-8")
        self._progress(key, f"启动翻译引擎（{PROVIDERS[provider][0]} · {model}，{pages or '?'} 页）…")
        result = self._run_engine(key, command, folder, secret)
        if key not in self.store.translations():
            return  # stopped while the engine was working
        line, warnings = outcome(result)
        dual, mono = Path(result["dual"]), Path(result["mono"]) if result.get("mono") else None
        if self.settings["storage_mode"] == "link":
            # Link mode keeps every file in the output folder, like the Markdown files.
            output_root = Path(self.settings["output_root"])
            output_root.mkdir(parents=True, exist_ok=True)
            for source in [dual] + ([mono] if mono else []):
                placed = unique_path(output_root, source.name, key)
                shutil.move(str(source), placed)
                dual, mono = (placed, mono) if source == dual else (dual, placed)
            shutil.rmtree(folder, ignore_errors=True)
        meta = {"source": str(source), "base_name": base_name, "model": result.get("model", ""), "warnings": warnings,
                **{name: result.get(name) for name in ("pages", "usage", "paragraphs", "seconds", "untranslated_pages",
                                                       "missing_glyphs", "broken_chars")}}
        self.store.update_translation(key, stage="attach", resume_stage="attach", file=str(dual),
                                      mono=str(mono or ""), meta=meta, attempts=0, error="", next_try=0,
                                      progress="已生成（" + line + "）" + ("；需检查：" + "；".join(warnings) if warnings else ""))
        self.log(f"双语 PDF 已生成：{job['title']}（{result.get('model', '')}，{line}）"
                 + ("。请检查：" + "；".join(warnings) if warnings else ""))

    def _run_engine(self, key: str, command: list[str], folder: Path, secret: str) -> dict:
        environment = dict(os.environ, ZMT_LLM_KEY=secret, PYTHONIOENCODING="utf-8")
        with (folder / "engine.log").open("wb") as log:
            process = self.process = subprocess.Popen(
                [*command, "--job", str(folder / "job.json")], stdout=subprocess.PIPE, stderr=log,
                stdin=subprocess.DEVNULL, env=environment, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
            result, error = None, None
            try:
                for raw in process.stdout:
                    if self.cancelled.is_set() or self.stopping.is_set():
                        self._kill()
                        break
                    try:
                        event = json.loads(raw.decode("utf-8", "replace"))
                    except ValueError:
                        continue
                    if event.get("type") == "progress":
                        self._progress(key, f"翻译中 {event.get('percent', 0):.0f}%：{event.get('stage', '')}")
                    elif event.get("type") == "done":
                        result = event
                    elif event.get("type") == "error":
                        error = event
                code = process.wait()
            finally:
                process.stdout.close()
                self.process = None
        if self.cancelled.is_set() or self.stopping.is_set():
            raise ServiceError("已停止翻译。")
        if error:
            raise ServiceError(error.get("message") or "翻译引擎出错。", retryable=bool(error.get("retryable")))
        if not result or not Path(result.get("dual", "")).is_file():
            raise ServiceError(f"翻译引擎异常退出（代码 {code}），详见 {folder / 'engine.log'}。", retryable=True)
        return result

    def attach(self, job: dict):
        key = job["key"]
        target = Path(job["file"]) if job["file"] else None
        if not self._plugin_ready(key, f"双语 PDF 已生成（{target}），" if target else ""):
            return
        if not (target and target.is_file()):
            self.store.update_translation(key, stage="queued", resume_stage="queued", file="", progress="双语 PDF 文件丢失，重新翻译")
            return
        output_root = Path(self.settings["output_root"])
        link = target.parent.resolve() == output_root.resolve()
        files = [target] + ([Path(job["mono"])] if job["mono"] and Path(job["mono"]).is_file() else [])
        attached = []
        for source in files:
            result = self.zotero.bridge("attach", kind="translation", parent_key=key, file=str(source),
                                        mode="link" if link else "import", title=source.stem)
            attached.append((source, result))
            if not link and not result.get("skipped") and self.settings.get("export_copy"):
                output_root.mkdir(parents=True, exist_ok=True)
                shutil.copy2(source, unique_path(output_root, source.name, key))
        final, result = attached[0]
        if not link:
            shutil.rmtree(self.work_dir / key, ignore_errors=True)
        warnings = job["meta"].get("warnings", [])
        if result.get("skipped"):
            progress = "条目已有双语 PDF，未重复添加"
        else:
            progress = "已添加到 Zotero：" + Path(result.get("path") or final).name \
                + ("；需检查：" + "；".join(warnings) if warnings else "")
        self.store.update_translation(key, stage="done", result_key=result.get("key", ""), file=str(result.get("path") or final),
                                      progress=progress, error="")
        if any(is_markdown_attachment(c["data"]) for c in self._children(key)):
            self.store.set_selected(key, False)  # a paper still waiting for conversion stays ticked
        self.log(f"双语 PDF 已添加到 Zotero：{Path(result.get('path') or final).name}")
        self.emit("translation_attached", key)
