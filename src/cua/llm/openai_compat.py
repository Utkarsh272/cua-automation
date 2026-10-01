"""Client for any OpenAI-compatible chat-completions API with tool calling.

Covers the free tiers that matter here (Groq, Google Gemini's OpenAI endpoint, OpenRouter,
Cerebras) and local models through Ollama, with one implementation. Uses plain ``httpx`` so
there is no extra SDK dependency.

Free tiers are rate limited by requests *and* tokens per minute, so 429s are expected, not
exceptional: the client honours ``Retry-After`` (or backs off exponentially) up to a bound.
"""

from __future__ import annotations

import base64
import json
import time
from typing import Any

import httpx

from .base import LLMError, LLMResponse, ToolCall, ToolSpec

RETRYABLE = {408, 409, 429, 500, 502, 503, 504}


class OpenAICompatClient:
    def __init__(
        self,
        *,
        provider: str,
        base_url: str,
        model: str,
        api_key: str | None,
        supports_images: bool = False,
        extra_body: dict[str, Any] | None = None,
        timeout_s: float = 120.0,
        max_retries: int = 6,
        max_backoff_s: float = 65.0,
        transport: httpx.BaseTransport | None = None,
        sleep: Any = time.sleep,
    ) -> None:
        self.provider = provider
        self.model = model
        self.supports_images = supports_images
        self._url = base_url.rstrip("/") + "/chat/completions"
        self._extra = extra_body or {}
        self._max_retries = max_retries
        self._max_backoff = max_backoff_s
        self._sleep = sleep
        headers = {"Content-Type": "application/json"}
        if api_key:
            headers["Authorization"] = f"Bearer {api_key}"
        self._http = httpx.Client(headers=headers, timeout=timeout_s, transport=transport)
        self._tool_choice: str = "required"

    def _payload(
        self, system: str, user: str, tools: list[ToolSpec], image_png: bytes | None
    ) -> dict[str, Any]:
        content: Any = user
        if image_png is not None and self.supports_images:
            b64 = base64.b64encode(image_png).decode()
            content = [
                {"type": "text", "text": user},
                {"type": "image_url", "image_url": {"url": f"data:image/png;base64,{b64}"}},
            ]
        return {
            "model": self.model,
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": content},
            ],
            "tools": [
                {
                    "type": "function",
                    "function": {
                        "name": t.name,
                        "description": t.description,
                        "parameters": t.parameters,
                    },
                }
                for t in tools
            ],
            "tool_choice": self._tool_choice,
            "temperature": 0,
            **self._extra,
        }

    def decide(
        self, system: str, user: str, tools: list[ToolSpec], image_png: bytes | None = None
    ) -> LLMResponse:
        retries = 0
        t0 = time.monotonic()
        while True:
            resp = self._http.post(self._url, json=self._payload(system, user, tools, image_png))
            if (
                resp.status_code == 400
                and self._tool_choice == "required"
                and "tool_choice" in resp.text
            ):
                # Some OpenAI-compatible endpoints only accept "auto"; fall back once.
                self._tool_choice = "auto"
                continue
            if resp.status_code in RETRYABLE and retries < self._max_retries:
                retries += 1
                self._sleep(self._backoff(resp, retries))
                continue
            if resp.status_code >= 400:
                raise LLMError(f"{self.provider} HTTP {resp.status_code}: {resp.text[:500]}")
            break
        data = resp.json()
        choice = (data.get("choices") or [{}])[0]
        message = choice.get("message") or {}
        call = None
        for tc in message.get("tool_calls") or []:
            fn = tc.get("function") or {}
            try:
                args = json.loads(fn.get("arguments") or "{}")
            except json.JSONDecodeError:
                args = {"_unparseable_arguments": fn.get("arguments")}
            call = ToolCall(name=fn.get("name", ""), args=args if isinstance(args, dict) else {})
            break  # one decision per turn; extra calls are ignored on purpose
        usage = data.get("usage") or {}
        return LLMResponse(
            call=call,
            text=message.get("content") or "",
            model=data.get("model", self.model),
            provider=self.provider,
            request_id=data.get("id") or resp.headers.get("x-request-id"),
            input_tokens=usage.get("prompt_tokens"),
            output_tokens=usage.get("completion_tokens"),
            latency_ms=int((time.monotonic() - t0) * 1000),
            retries=retries,
            raw_finish=choice.get("finish_reason"),
        )

    def _backoff(self, resp: httpx.Response, attempt: int) -> float:
        for header in ("retry-after", "x-ratelimit-reset-tokens", "x-ratelimit-reset-requests"):
            value = resp.headers.get(header)
            seconds = _parse_seconds(value) if value else None
            if seconds is not None:
                return min(self._max_backoff, max(0.5, seconds + 0.25))
        return float(min(self._max_backoff, 2.0**attempt))


def _parse_seconds(value: str) -> float | None:
    """Accepts '7', '1.5', and Groq-style durations such as '6.2s' or '1m3.5s'."""
    v = value.strip()
    try:
        return float(v)
    except ValueError:
        pass
    total, num = 0.0, ""
    units = {"h": 3600.0, "m": 60.0, "s": 1.0}
    i = 0
    while i < len(v):
        ch = v[i]
        if ch.isdigit() or ch == ".":
            num += ch
        elif v[i : i + 2] == "ms":
            total += float(num or 0) / 1000
            num = ""
            i += 1
        elif ch in units:
            total += float(num or 0) * units[ch]
            num = ""
        else:
            return None
        i += 1
    return total if not num else None
