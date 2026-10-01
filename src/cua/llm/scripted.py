"""A deterministic stand-in for a model, for tests and offline demos.

It is *not* evidence of a real run: every response is tagged ``provider="scripted"`` and the
discovery trace records that, so a scripted run can never be mistaken for the genuine LLM run the
brief requires.
"""

from __future__ import annotations

from collections.abc import Callable

from .base import LLMResponse, ToolCall, ToolSpec

Policy = Callable[[str, str, int], ToolCall | None]
"""(system, user message, turn index) -> the tool call to make."""


class ScriptedClient:
    provider = "scripted"
    supports_images = False

    def __init__(self, policy: Policy, model: str = "scripted-policy") -> None:
        self.model = model
        self._policy = policy
        self.calls: list[tuple[str, str]] = []

    def decide(
        self, system: str, user: str, tools: list[ToolSpec], image_png: bytes | None = None
    ) -> LLMResponse:
        turn = len(self.calls)
        self.calls.append((system, user))
        call = self._policy(system, user, turn)
        return LLMResponse(call=call, text="", model=self.model, provider=self.provider)
