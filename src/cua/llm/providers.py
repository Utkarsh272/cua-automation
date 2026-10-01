"""Provider presets. Pick one with ``--provider``; override the model with ``--model``.

Free-tier notes (limits change; check the provider's console):

* ``groq``: OpenAI-compatible, tool calling, fast. Free tier is limited by requests and
  *tokens* per minute, so expect a few 429 pauses on a 10-15 step run; the client waits them out.
* ``gemini``: Google AI Studio key, OpenAI-compatible endpoint, accepts screenshots. On the free
  tier Google may use prompts to improve its products: fine for CU Core's synthetic data, not for
  real member data.
* ``openrouter``: many models behind one key; free models carry a ``:free`` suffix and a small
  daily request cap.
* ``cerebras``: OpenAI-compatible, generous token allowance.
* ``ollama``: a local model on your machine. Free, private, no rate limits; quality depends on
  the model you pull (it must support tool calling).
* ``anthropic``: Claude, paid API.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from typing import Any

from .base import LLMClient, LLMError


@dataclass(frozen=True)
class Preset:
    base_url: str | None
    key_env: str | None
    default_model: str
    supports_images: bool = False
    extra_body: dict[str, Any] = field(default_factory=dict)


PRESETS: dict[str, Preset] = {
    "groq": Preset(
        "https://api.groq.com/openai/v1",
        "GROQ_API_KEY",
        "openai/gpt-oss-120b",
        extra_body={"reasoning_effort": "low"},
    ),
    "gemini": Preset(
        "https://generativelanguage.googleapis.com/v1beta/openai",
        "GEMINI_API_KEY",
        "gemini-2.5-flash",
        supports_images=True,
    ),
    "openrouter": Preset("https://openrouter.ai/api/v1", "OPENROUTER_API_KEY", "openrouter/auto"),
    "cerebras": Preset("https://api.cerebras.ai/v1", "CEREBRAS_API_KEY", "gpt-oss-120b"),
    "ollama": Preset("http://localhost:11434/v1", None, "qwen3:8b"),
    "anthropic": Preset(None, "ANTHROPIC_API_KEY", "claude-sonnet-5-5", supports_images=True),
}


def make_client(provider: str, model: str | None = None) -> LLMClient:
    if provider not in PRESETS:
        raise LLMError(f"unknown provider {provider!r}; choose from {sorted(PRESETS)}")
    p = PRESETS[provider]
    # Per-provider override (GROQ_MODEL, GEMINI_MODEL, ...), so a model name meant for one
    # provider is never sent to another.
    model = model or os.environ.get(f"{provider.upper()}_MODEL") or p.default_model
    key = os.environ.get(p.key_env) if p.key_env else None
    if p.key_env and not key:
        raise LLMError(f"{p.key_env} is not set (add it to .env)")
    if provider == "anthropic":
        from .anthropic_client import AnthropicClient

        return AnthropicClient(model=model, api_key=key)
    from .openai_compat import OpenAICompatClient

    assert p.base_url is not None
    base_url = os.environ.get(f"{provider.upper()}_BASE_URL", p.base_url)
    return OpenAICompatClient(
        provider=provider,
        base_url=base_url,
        model=model,
        api_key=key,
        supports_images=p.supports_images,
        extra_body=p.extra_body if model == p.default_model else {},
    )
