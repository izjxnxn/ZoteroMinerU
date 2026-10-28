"""One client for mainstream LLM vendors: OpenAI-compatible Chat Completions plus Anthropic Messages.

DeepSeek, Qwen (阿里云百炼), Xiaomi MiMo, Kimi, GLM, Gemini, SiliconFlow, OpenRouter and Ollama all speak the
OpenAI Chat Completions dialect, so two protocols cover every provider; model names stay user-editable.
Truncation, reasoning-tag and parameter handling follow zotero-AI-Butler's provider layer.
"""
from __future__ import annotations

import re
import time

import requests

from .clients import ServiceError, safe_message

# key: (label, protocol, default base URL, suggested models — the first is the default)
PROVIDERS = {
    "deepseek": ("DeepSeek（深度求索）", "openai", "https://api.deepseek.com", ["deepseek-v4-flash", "deepseek-v4-pro"]),
    "qwen": ("通义千问（阿里云百炼）", "openai", "https://dashscope.aliyuncs.com/compatible-mode/v1",
             ["qwen-plus", "qwen3-max", "qwen-flash", "qwen-long", "qwen3-vl-plus", "qwen-vl-max"]),
    "mimo": ("小米 MiMo", "openai", "https://api.xiaomimimo.com/v1", ["mimo-v2.5", "mimo-v2.5-pro", "mimo-v2-flash"]),
    "anthropic": ("Anthropic（Claude）", "anthropic", "https://api.anthropic.com",
                  ["claude-sonnet-5-5", "claude-opus-5-5", "claude-haiku-5-5"]),
    "openai": ("OpenAI（ChatGPT）", "openai", "https://api.openai.com/v1", ["gpt-5.6", "gpt-5.6-terra", "gpt-5.6-luna"]),
    "gemini": ("Google Gemini", "openai", "https://generativelanguage.googleapis.com/v1beta/openai",
               ["gemini-2.5-flash", "gemini-2.5-pro"]),
    "moonshot": ("Kimi（月之暗面）", "openai", "https://api.moonshot.cn/v1", ["kimi-k2-turbo-preview", "kimi-latest"]),
    "zhipu": ("智谱 GLM", "openai", "https://open.bigmodel.cn/api/paas/v4", ["glm-4.6", "glm-4.5-air", "glm-4.5v"]),
    "siliconflow": ("硅基流动 SiliconFlow", "openai", "https://api.siliconflow.cn/v1",
                    ["deepseek-ai/DeepSeek-V3.2", "Qwen/Qwen3-235B-A22B-Instruct-2507"]),
    "openrouter": ("OpenRouter", "openai", "https://openrouter.ai/api/v1", ["anthropic/claude-sonnet-5-5", "openai/gpt-5.6"]),
    "ollama": ("Ollama（本机模型）", "openai", "http://127.0.0.1:11434/v1", ["qwen3:14b", "llama3.1:8b"]),
    "custom": ("其他 OpenAI 兼容接口", "openai", "", []),
}
REASONING = re.compile(r"<\s*(think|thinking|reasoning)\b[^>]*>.*?<\s*/\s*\1\s*>", re.DOTALL | re.IGNORECASE)
LENGTH_REASONS = {"length", "max_tokens", "max_output_tokens", "max_completion_tokens", "token_limit_exceeded"}
CONTEXT_HINTS = ("context_length", "context window", "context_window", "maximum context", "too many tokens",
                 "prompt is too long", "input is too long", "超出上下文", "上下文长度", "输入过长", "tokens exceed")
CONTINUE = "上一条回答因长度限制被截断。请从中断处继续输出剩余内容：不要重复已输出的文字，不要添加任何说明。"


LIMIT_WORDS = ("<=", "less than", "maximum", "exceed", "range", "at most", "too large", "greater than", "超过", "范围", "上限")
# Output ceilings providers reported, per (provider, base URL, model), so later calls ask for the right amount directly.
OUTPUT_CAPS: dict[tuple, int] = {}
# Models that rejected image input; later calls to them send text only.
TEXT_ONLY: set[tuple] = set()
VISION_HINTS = ("image", "vision", "multimodal", "unknown variant", "content type", "多模态", "图片", "图像", "视觉")
FIGURES_INTRO = "以下为论文中的图片（共 {count} 张），编号与正文中的 [图 N] 标记一一对应："


class ContextTooLong(ServiceError):
    """The input exceeded the model's context window; the caller should split it into smaller parts."""


def _reported_limit(message: str, requested: int) -> int | None:
    """Pick the output limit out of messages such as "valid range of max_tokens is [1, 8192]"."""
    numbers = [int(n) for n in re.findall(r"\d{3,7}", message.replace(",", ""))]
    candidates = [n for n in numbers if 256 <= n < requested]
    return max(candidates) if candidates else None


def _context_room(message: str) -> int | None:
    """Output room left in "maximum context length is 131072 ... (90000 in the messages, ...)" style errors."""
    text = message.lower().replace(",", "")
    limit = re.search(r"context (?:length|window)[^\d]{0,30}(\d{4,8})", text)
    used = re.search(r"(\d{3,8})\s*(?:tokens\s*)?in the messages", text)
    if limit and used:
        return int(limit.group(1)) - int(used.group(1)) - 256
    return None


class LLMClient:
    def __init__(self, provider: str, api_key: str, model: str, base: str = "", *, timeout: int = 600):
        if provider not in PROVIDERS:
            raise ServiceError("未知的大模型服务商。")
        label, self.protocol, default_base, models = PROVIDERS[provider]
        self.provider, self.label = provider, label
        self.key = (api_key or "").strip()
        self.model = (model or "").strip() or (models[0] if models else "")
        self.base = (base or default_base).strip().rstrip("/")
        self.timeout = timeout
        # Parameters this model rejected; learned from 400 responses and not sent again.
        self.dropped: set[str] = set()
        # Optional provider-specific body fields (e.g. switching thinking off for translation); rejected ones are dropped.
        self.extra: dict = {}
        if not self.base:
            raise ServiceError("请填写大模型接口地址（Base URL）。")
        if not self.model:
            raise ServiceError("请填写模型名称。")
        if not self.key and provider != "ollama":
            raise ServiceError("请在设置中填写大模型 API Key。", auth=True)
        self.session = requests.Session()

    @property
    def display(self) -> str:
        return f"{self.label} · {self.model}"

    def _headers(self) -> dict:
        if self.protocol == "anthropic":
            return {"x-api-key": self.key, "anthropic-version": "2023-06-01", "content-type": "application/json"}
        headers = {"Content-Type": "application/json"}
        if self.key:
            headers["Authorization"] = "Bearer " + self.key
        return headers

    def _url(self, path: str) -> str:
        if self.protocol == "anthropic" and not self.base.endswith("/v1"):
            return self.base + "/v1" + path
        return self.base + path

    def _post(self, path: str, body: dict) -> dict:
        try:
            response = self.session.post(self._url(path), json=body, headers=self._headers(), timeout=(20, self.timeout))
        except requests.Timeout:
            raise ServiceError("大模型响应超时，稍后自动重试。", retryable=True) from None
        except requests.RequestException:
            raise ServiceError("无法连接大模型接口，请检查网络、代理或接口地址。", retryable=True) from None
        return self._decode(response)

    def _decode(self, response) -> dict:
        try:
            data = response.json()
        except ValueError:
            data = {}
        if response.ok:
            if not data:
                raise ServiceError("大模型返回的数据不是 JSON。", retryable=True)
            return data
        error = data.get("error") if isinstance(data, dict) else None
        detail = (error.get("message") if isinstance(error, dict) else error) \
            or (data.get("message") if isinstance(data, dict) else "") or response.text[:300]
        detail = safe_message(str(detail), self.key)
        if response.status_code in (401, 403):
            raise ServiceError(f"大模型 API Key 被拒绝（HTTP {response.status_code}）：{detail}", auth=True)
        if response.status_code == 404:
            raise ServiceError(f"接口地址或模型名称不存在（HTTP 404）：{detail}")
        if response.status_code in (400, 413, 422) and any(h in detail.lower() for h in CONTEXT_HINTS):
            raise ContextTooLong(f"论文内容超过模型上下文长度：{detail}")
        retryable = response.status_code in (408, 409, 429) or response.status_code >= 500
        raise ServiceError(f"大模型返回 HTTP {response.status_code}：{detail}", retryable=retryable)

    @property
    def _cap_key(self):
        return (self.provider, self.base, self.model)

    def _user_content(self, user: str, images: list[dict]):
        """Text plus labelled images: [{"label": "[图 1]", "mime": "image/jpeg", "data": base64}, ...]."""
        if not images:
            return user
        if self.protocol == "anthropic":
            parts = [{"type": "text", "text": user}, {"type": "text", "text": FIGURES_INTRO.format(count=len(images))}]
            for image in images:
                parts += [{"type": "text", "text": image["label"]},
                          {"type": "image", "source": {"type": "base64", "media_type": image["mime"], "data": image["data"]}}]
            return parts
        parts = [{"type": "text", "text": user}, {"type": "text", "text": FIGURES_INTRO.format(count=len(images))}]
        for image in images:
            parts += [{"type": "text", "text": image["label"]},
                      {"type": "image_url", "image_url": {"url": f"data:{image['mime']};base64,{image['data']}"}}]
        return parts

    @property
    def sees_images(self) -> bool:
        return self._cap_key not in TEXT_ONLY

    def chat(self, system: str, user: str, *, max_tokens: int | None = None, temperature: float = 0.2,
             images: list[dict] | None = None) -> tuple[str, dict]:
        """Return (text, usage). Continues truncated answers, retries transient errors, adapts rejected parameters.

        max_tokens None means "no limit of our own": ask for a large output budget and settle on the model's real
        ceiling when the provider reports it (the ceiling is remembered for later calls). `images` are sent with
        the prompt when the model accepts pictures; a model that rejects them is remembered and gets text only.
        """
        if not max_tokens:
            max_tokens = OUTPUT_CAPS.get(self._cap_key) or (32000 if self.protocol == "anthropic" else 65536)
        images = images if images and self.sees_images else []
        messages = [{"role": "user", "content": self._user_content(user, images)}]
        output, usage = "", {"input": 0, "output": 0}
        continuations = 0
        attempt = 0
        while True:
            try:
                call = self._anthropic if self.protocol == "anthropic" else self._openai
                text, truncated, used = call(system, messages, max_tokens, temperature)
            except ContextTooLong as error:
                # A large output budget can itself overflow the context window; shrink the budget, not the paper.
                room = _context_room(str(error))
                if room and 1024 <= room < max_tokens:
                    max_tokens = room
                    continue
                if max_tokens > 8192 and any(w in str(error).lower() for w in ("completion", "max_tokens", "output")):
                    max_tokens //= 2
                    continue
                raise
            except ServiceError as error:
                message = str(error).lower()
                if images and any(f"http {code}" in message for code in (400, 413, 415, 422)) \
                        and any(hint in message for hint in VISION_HINTS):
                    # This model cannot take pictures (e.g. DeepSeek): remember it and continue with text only.
                    TEXT_ONLY.add(self._cap_key)
                    images = []
                    messages = [{"role": "user", "content": user}, *messages[1:]]
                    continue
                if "http 400" in message or "http 422" in message:
                    if any(name in message for name in ("max_tokens", "max_completion_tokens", "max_output", "output tokens")) \
                            and any(word in message for word in LIMIT_WORDS) and max_tokens > 1024:
                        # The model's own output ceiling is lower than requested: use the number it reports.
                        max_tokens = _reported_limit(message, max_tokens) or max_tokens // 2
                        OUTPUT_CAPS[self._cap_key] = max_tokens
                        continue
                    adapted = [name for name in ("temperature", "max_tokens", *self.extra)
                               if name.lower() in message and name not in self.dropped]
                    if not adapted:
                        # Optional fields are the usual suspect even when the message does not name them.
                        adapted = [name for name in self.extra if name not in self.dropped]
                    if adapted:
                        self.dropped.update(adapted)
                        continue
                attempt += 1
                if error.retryable and attempt <= 3:
                    time.sleep(5 * 2 ** (attempt - 1))
                    continue
                raise
            usage = {k: usage[k] + used[k] for k in usage}
            if truncated and not REASONING.sub("", text).strip():
                # Thinking models can spend the whole budget on reasoning; ask again with more room instead.
                cap = OUTPUT_CAPS.get(self._cap_key, 262144)
                if max_tokens < cap and continuations < 3:
                    continuations += 1
                    max_tokens = min(max_tokens * 2, cap)
                    continue
                raise ServiceError(f"模型把约 {max_tokens} tokens 的输出额度全部用在了思考过程上，没有给出正文。"
                                   "可换用非思考模型，或在设置中改为“快速通读”。", retryable=True)
            output += text
            if not truncated:
                break
            if continuations >= 4:
                raise ServiceError(f"大模型输出多次被截断（单次输出上限约 {max_tokens} tokens）。可换用输出上限更大的模型。")
            continuations += 1
            messages = [{"role": "user", "content": self._user_content(user, images)}, {"role": "assistant", "content": output},
                        {"role": "user", "content": CONTINUE}]
        output = REASONING.sub("", output).strip()
        if not output:
            raise ServiceError("大模型返回了空内容。", retryable=True)
        return output, usage

    def _openai(self, system, messages, max_tokens, temperature):
        body = {"model": self.model, "messages": [{"role": "system", "content": system}, *messages], "stream": False}
        # OpenAI reasoning models (gpt-5.x, o-series) only accept max_completion_tokens and the default temperature.
        reasoning = self.provider == "openai" and re.match(r"(gpt-5|o\d)", self.model)
        if reasoning or "max_tokens" in self.dropped:
            body["max_completion_tokens"] = max_tokens
        else:
            body["max_tokens"] = max_tokens
        if not reasoning and "temperature" not in self.dropped:
            body["temperature"] = temperature
        body.update({name: value for name, value in self.extra.items() if name not in self.dropped})
        data = self._post("/chat/completions", body)
        choice = (data.get("choices") or [{}])[0]
        content = (choice.get("message") or {}).get("content") or ""
        if isinstance(content, list):
            content = "".join(part.get("text", "") for part in content if isinstance(part, dict))
        usage = data.get("usage") or {}
        return content, choice.get("finish_reason") in LENGTH_REASONS, \
            {"input": usage.get("prompt_tokens") or 0, "output": usage.get("completion_tokens") or 0}

    def _anthropic(self, system, messages, max_tokens, temperature):
        body = {"model": self.model, "max_tokens": max_tokens, "system": system, "messages": messages}
        if "temperature" not in self.dropped:
            body["temperature"] = temperature
        data = self._post("/messages", body)
        content = "".join(block.get("text", "") for block in data.get("content") or [] if block.get("type") == "text")
        usage = data.get("usage") or {}
        return content, data.get("stop_reason") in LENGTH_REASONS, \
            {"input": usage.get("input_tokens") or 0, "output": usage.get("output_tokens") or 0}

    def models(self) -> list[str]:
        try:
            response = self.session.get(self._url("/models"), headers=self._headers(), timeout=(15, 60))
        except requests.RequestException:
            raise ServiceError("无法连接大模型接口，请检查网络、代理或接口地址。") from None
        data = self._decode(response)
        return sorted({row.get("id", "") for row in data.get("data") or [] if row.get("id")})

    def probe(self) -> str:
        text, _ = self.chat("你是连通性测试助手。", "请只回复：OK", max_tokens=1024)
        return f"{self.display} 连接成功，模型回复：{text[:40]}"
