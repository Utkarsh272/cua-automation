"""The narrow contract between the discovery agent and any model provider.

One call = one decision: a system prompt, one user message (goal, action log, current
observation), the tool list, and optionally a screenshot. The model must answer with exactly one
tool call. Conversation state is *not* kept by the provider: the agent rebuilds the message each
turn from its own action log, which keeps prompts small (free tiers have tight token-per-minute
limits) and makes every call independently reproducible from the evidence log.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Protocol


@dataclass(frozen=True)
class ToolSpec:
    name: str
    description: str
    parameters: dict[str, Any]  # JSON Schema object


@dataclass(frozen=True)
class ToolCall:
    name: str
    args: dict[str, Any]


@dataclass
class LLMResponse:
    call: ToolCall | None
    text: str
    model: str
    provider: str
    request_id: str | None = None
    input_tokens: int | None = None
    output_tokens: int | None = None
    latency_ms: int = 0
    retries: int = 0
    raw_finish: str | None = None
    extra: dict[str, Any] = field(default_factory=dict)


class LLMError(RuntimeError):
    """The provider failed in a way retrying will not fix (auth, bad request, quota)."""


class LLMClient(Protocol):
    provider: str
    model: str
    supports_images: bool

    def decide(
        self,
        system: str,
        user: str,
        tools: list[ToolSpec],
        image_png: bytes | None = None,
    ) -> LLMResponse: ...
