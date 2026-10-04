"""Chat-completion clients.

OpenRouterClient talks to https://openrouter.ai/api/v1/chat/completions
(OpenAI-compatible, tool calling). Stdlib only.
ScriptedLLM replays canned responses, so the agent loop can be tested offline.
"""
from __future__ import annotations

import json
import os
import time
import urllib.error
import urllib.request

from pathlib import Path

CONFIG_FILES = [Path.cwd() / "culinacompass.env",
                Path(__file__).resolve().parent.parent / "culinacompass.env",
                Path.home() / ".culinacompass.env"]


def load_config() -> Path | None:
    """Read KEY=VALUE lines from the first culinacompass.env found (current folder,
    project folder, then home). Real environment variables win over the file."""
    for path in CONFIG_FILES:
        if path.is_file():
            for line in path.read_text(encoding="utf-8").splitlines():
                line = line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                k, v = line.split("=", 1)
                os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))
            return path
    return None


CONFIG_PATH = load_config()
DEFAULT_MODEL = os.environ.get("OPENROUTER_MODEL", "qwen/qwen3.7-flash")
URL = os.environ.get("OPENROUTER_BASE_URL", "https://openrouter.ai/api/v1") + "/chat/completions"


class LLMError(RuntimeError):
    pass


class OpenRouterClient:
    def __init__(self, model: str | None = None, api_key: str | None = None,
                 temperature: float = 0.0, timeout: int = 120, retries: int = 3):
        self.model = model or DEFAULT_MODEL
        self.api_key = api_key or os.environ.get("OPENROUTER_API_KEY")
        if not self.api_key:
            raise LLMError("OPENROUTER_API_KEY not found. Put it in culinacompass.env in the project folder "
                           "(the empty OPENROUTER_API_KEY= line), or use --offline for the rule-based planner")
        self.temperature, self.timeout, self.retries = temperature, timeout, retries
        self.usage = {"prompt_tokens": 0, "completion_tokens": 0, "requests": 0}

    def chat(self, messages: list[dict], tools: list[dict]) -> dict:
        body = json.dumps({"model": self.model, "messages": messages, "tools": tools,
                           "tool_choice": "auto", "temperature": self.temperature}).encode()
        req = urllib.request.Request(URL, data=body, headers={
            "Authorization": f"Bearer {self.api_key}", "Content-Type": "application/json",
            "HTTP-Referer": "https://github.com/culinacompass", "X-Title": "CulinaCompass SG"})
        last = None
        for attempt in range(self.retries):
            try:
                with urllib.request.urlopen(req, timeout=self.timeout) as r:
                    resp = json.loads(r.read().decode())
                if "error" in resp:
                    raise LLMError(str(resp["error"]))
                u = resp.get("usage") or {}
                self.usage["prompt_tokens"] += u.get("prompt_tokens", 0)
                self.usage["completion_tokens"] += u.get("completion_tokens", 0)
                self.usage["requests"] += 1
                return resp["choices"][0]["message"]
            except urllib.error.HTTPError as e:
                last = LLMError(f"HTTP {e.code}: {e.read().decode(errors='replace')[:500]}")
                if e.code not in (408, 429, 500, 502, 503, 504):
                    raise last
            except (urllib.error.URLError, TimeoutError) as e:
                last = LLMError(f"network error: {e}")
            time.sleep(2 ** attempt)
        raise last or LLMError("unknown error")


class ScriptedLLM:
    """Returns pre-written assistant messages in order (for tests and demos)."""

    def __init__(self, responses: list[dict]):
        self.responses = list(responses)
        self.seen: list[list[dict]] = []
        self.model = "scripted"
        self.usage = {"prompt_tokens": 0, "completion_tokens": 0, "requests": 0}

    def chat(self, messages: list[dict], tools: list[dict]) -> dict:
        self.seen.append([dict(m) for m in messages])
        self.usage["requests"] += 1
        if not self.responses:
            return {"role": "assistant", "content": "(script exhausted)"}
        return self.responses.pop(0)

    @staticmethod
    def tool_call(name: str, args: dict, call_id: str = "c1") -> dict:
        return {"role": "assistant", "content": None, "tool_calls": [
            {"id": call_id, "type": "function", "function": {"name": name, "arguments": json.dumps(args)}}]}
