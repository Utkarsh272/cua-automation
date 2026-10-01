"""Claude via the Anthropic SDK, with forced tool use (``tool_choice: any``)."""

from __future__ import annotations

import base64
import time
from typing import Any

from .base import LLMError, LLMResponse, ToolCall, ToolSpec


class AnthropicClient:
    provider = "anthropic"
    supports_images = True

    def __init__(self, *, model: str, api_key: str | None, max_tokens: int = 1024) -> None:
        import anthropic

        self.model = model
        self._max_tokens = max_tokens
        self._client = anthropic.Anthropic(api_key=api_key, max_retries=6)
        self._errors = anthropic.APIError

    def decide(
        self, system: str, user: str, tools: list[ToolSpec], image_png: bytes | None = None
    ) -> LLMResponse:
        content: list[dict[str, Any]] = [{"type": "text", "text": user}]
        if image_png is not None:
            content.append(
                {
                    "type": "image",
                    "source": {
                        "type": "base64",
                        "media_type": "image/png",
                        "data": base64.b64encode(image_png).decode(),
                    },
                }
            )
        # Plain dicts; the SDK's TypedDicts are checked by the API itself.
        tool_params: Any = [
            {"name": t.name, "description": t.description, "input_schema": t.parameters}
            for t in tools
        ]
        messages: Any = [{"role": "user", "content": content}]
        t0 = time.monotonic()
        try:
            msg = self._client.messages.create(
                model=self.model,
                max_tokens=self._max_tokens,
                system=system,
                tools=tool_params,
                tool_choice={"type": "any"},
                messages=messages,
            )
        except self._errors as exc:
            raise LLMError(f"anthropic: {exc}") from exc
        call = None
        text = ""
        for block in msg.content:
            if block.type == "tool_use" and call is None:
                call = ToolCall(name=block.name, args=dict(block.input))
            elif block.type == "text":
                text += block.text
        return LLMResponse(
            call=call,
            text=text,
            model=msg.model,
            provider=self.provider,
            request_id=msg.id,
            input_tokens=msg.usage.input_tokens,
            output_tokens=msg.usage.output_tokens,
            latency_ms=int((time.monotonic() - t0) * 1000),
            raw_finish=msg.stop_reason,
        )
