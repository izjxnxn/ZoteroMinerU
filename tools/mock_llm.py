"""Local OpenAI-compatible stand-in for acceptance tests of the layout-preserving translation (no paid API).

It answers BabelDOC's prompts with pseudo-Chinese of similar length that keeps every placeholder and tag, so the
layout, fonts and Zotero attachment can be checked end to end. Run: python tools/mock_llm.py 18765
"""
from __future__ import annotations

import hashlib
import json
import re
import sys
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

KEEP = re.compile(r"(\{v?\d+\}|\{[a-z_]+\d*\}|<[^<>]{1,40}>|\[\[[^\]]*\]\]|%%[^%]*%%)")
POOL = "相场断裂模型壳体单元厚度方向裂纹能量位移计算方法结构数值分析材料刚度函数积分边界条件变分实体平面弯曲"
WORDS = [POOL[i:i + 2] for i in range(0, len(POOL) - 1, 2)]


def fake(text: str) -> str:
    out = []
    for part in KEEP.split(text):
        if not part or KEEP.fullmatch(part):
            out.append(part)
            continue

        def word(match):
            w = match.group(0)
            pick = WORDS[int(hashlib.md5(w.lower().encode()).hexdigest(), 16) % len(WORDS)]
            return pick if len(w) > 3 else w  # short tokens may be symbols such as c or g0: keep them
        part = re.sub(r"[A-Za-z][A-Za-z\-']*", word, part)
        part = re.sub(r"(?<=[一-鿿]) (?=[一-鿿])", "", part)
        out.append(part.replace(", ", "，").replace(". ", "。").replace("; ", "；"))
    return "".join(out)


def answer(prompt: str) -> str:
    start = prompt.find("[\n  {")
    if start >= 0:
        items, _ = json.JSONDecoder().raw_decode(prompt[start:])
        if items and "input" in items[0]:
            return json.dumps([{"id": item["id"], "output": fake(item["input"])} for item in items], ensure_ascii=False)
    if "extract" in prompt.lower() and "term" in prompt.lower():
        return "[]"
    for marker in ("Now translate the following text:\n\n", "Output only the translation.\n\n"):
        if marker in prompt:
            return fake(prompt.split(marker, 1)[1])
    return "OK"


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def do_POST(self):
        body = json.loads(self.rfile.read(int(self.headers.get("Content-Length", 0))) or b"{}")
        prompt = body.get("messages", [{}])[-1].get("content", "")
        text = answer(prompt if isinstance(prompt, str) else "")
        data = json.dumps({"choices": [{"message": {"role": "assistant", "content": text}, "finish_reason": "stop"}],
                           "usage": {"prompt_tokens": len(prompt) // 4, "completion_tokens": len(text) // 2}},
                          ensure_ascii=False).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)


if __name__ == "__main__":
    ThreadingHTTPServer(("127.0.0.1", int(sys.argv[1]) if len(sys.argv) > 1 else 18765), Handler).serve_forever()
