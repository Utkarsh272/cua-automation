"""Provider clients, tested against a fake HTTP transport (no network, no keys)."""

from __future__ import annotations

import json
from typing import Any

import httpx
import pytest

from cua.llm.base import LLMError, ToolSpec
from cua.llm.openai_compat import OpenAICompatClient, _parse_seconds
from cua.llm.providers import PRESETS, make_client

TOOLS = [ToolSpec("click", "Click.", {"type": "object", "properties": {"ref": {"type": "string"}}})]


def ok_body(args: dict[str, Any], name: str = "click") -> dict[str, Any]:
    return {
        "id": "req_1",
        "model": "m-1",
        "choices": [
            {
                "finish_reason": "tool_calls",
                "message": {
                    "content": None,
                    "tool_calls": [
                        {
                            "type": "function",
                            "function": {"name": name, "arguments": json.dumps(args)},
                        }
                    ],
                },
            }
        ],
        "usage": {"prompt_tokens": 120, "completion_tokens": 9},
    }


def client(handler: Any, **kw: Any) -> tuple[OpenAICompatClient, list[float]]:
    slept: list[float] = []
    c = OpenAICompatClient(
        provider="groq",
        base_url="https://example.test/v1",
        model="m-1",
        api_key="k",
        transport=httpx.MockTransport(handler),
        sleep=slept.append,
        **kw,
    )
    return c, slept


def test_request_shape_and_parsing() -> None:
    seen: list[dict[str, Any]] = []

    def handler(req: httpx.Request) -> httpx.Response:
        assert req.url.path == "/v1/chat/completions"
        assert req.headers["authorization"] == "Bearer k"
        seen.append(json.loads(req.content))
        return httpx.Response(200, json=ok_body({"ref": "e3", "rationale": "go"}))

    c, _ = client(handler, extra_body={"reasoning_effort": "low"})
    r = c.decide("sys", "user", TOOLS)
    assert r.call is not None and r.call.name == "click" and r.call.args["ref"] == "e3"
    assert (r.input_tokens, r.output_tokens, r.request_id) == (120, 9, "req_1")
    body = seen[0]
    assert body["tool_choice"] == "required" and body["temperature"] == 0
    assert body["reasoning_effort"] == "low"
    assert body["messages"][0] == {"role": "system", "content": "sys"}
    assert body["tools"][0]["function"]["name"] == "click"


def test_429_is_waited_out_using_retry_after() -> None:
    calls = {"n": 0}

    def handler(req: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        if calls["n"] < 3:
            return httpx.Response(429, headers={"retry-after": "2"}, json={"error": "rate"})
        return httpx.Response(200, json=ok_body({"ref": "e1"}))

    c, slept = client(handler)
    r = c.decide("s", "u", TOOLS)
    assert r.retries == 2 and slept == [2.25, 2.25]


def test_groq_style_reset_header_and_bounded_retries() -> None:
    def handler(req: httpx.Request) -> httpx.Response:
        return httpx.Response(429, headers={"x-ratelimit-reset-tokens": "1m3.5s"})

    c, slept = client(handler, max_retries=2)
    with pytest.raises(LLMError, match="429"):
        c.decide("s", "u", TOOLS)
    assert slept == [63.75, 63.75]


def test_tool_choice_falls_back_to_auto_once() -> None:
    seen: list[str] = []

    def handler(req: httpx.Request) -> httpx.Response:
        choice = json.loads(req.content)["tool_choice"]
        seen.append(choice)
        if choice == "required":
            return httpx.Response(400, json={"error": "tool_choice 'required' not supported"})
        return httpx.Response(200, json=ok_body({"ref": "e1"}))

    c, _ = client(handler)
    c.decide("s", "u", TOOLS)
    c.decide("s", "u", TOOLS)
    assert seen == ["required", "auto", "auto"]


def test_auth_errors_are_not_retried() -> None:
    c, slept = client(lambda req: httpx.Response(401, json={"error": "bad key"}))
    with pytest.raises(LLMError, match="401"):
        c.decide("s", "u", TOOLS)
    assert slept == []


def test_text_only_answer_and_bad_arguments() -> None:
    def text_only(req: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"choices": [{"message": {"content": "I would click"}}]})

    c, _ = client(text_only)
    r = c.decide("s", "u", TOOLS)
    assert r.call is None and r.text == "I would click"

    def garbled(req: httpx.Request) -> httpx.Response:
        body = ok_body({})
        body["choices"][0]["message"]["tool_calls"][0]["function"]["arguments"] = "{not json"
        return httpx.Response(200, json=body)

    c2, _ = client(garbled)
    r2 = c2.decide("s", "u", TOOLS)
    assert r2.call is not None and "_unparseable_arguments" in r2.call.args


def test_images_only_when_supported() -> None:
    bodies: list[dict[str, Any]] = []

    def handler(req: httpx.Request) -> httpx.Response:
        bodies.append(json.loads(req.content))
        return httpx.Response(200, json=ok_body({}))

    c, _ = client(handler, supports_images=True)
    c.decide("s", "u", TOOLS, image_png=b"\x89PNG")
    content = bodies[0]["messages"][1]["content"]
    assert content[1]["image_url"]["url"].startswith("data:image/png;base64,")

    c2, _ = client(handler, supports_images=False)
    c2.decide("s", "u", TOOLS, image_png=b"\x89PNG")
    assert bodies[1]["messages"][1]["content"] == "u"


@pytest.mark.parametrize(
    ("raw", "seconds"),
    [("7", 7.0), ("1.5", 1.5), ("6.2s", 6.2), ("1m3.5s", 63.5), ("250ms", 0.25), ("bogus", None)],
)
def test_parse_seconds(raw: str, seconds: float | None) -> None:
    got = _parse_seconds(raw)
    assert got == pytest.approx(seconds) if seconds is not None else got is None


def test_make_client_requires_key(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("GROQ_API_KEY", raising=False)
    with pytest.raises(LLMError, match="GROQ_API_KEY"):
        make_client("groq")
    with pytest.raises(LLMError, match="unknown provider"):
        make_client("acme")
    monkeypatch.setenv("GROQ_API_KEY", "x")
    monkeypatch.delenv("GROQ_MODEL", raising=False)
    monkeypatch.setenv("ANTHROPIC_MODEL", "claude-x")  # must not leak into another provider
    c = make_client("groq")
    assert c.provider == "groq" and c.model == PRESETS["groq"].default_model
    monkeypatch.setenv("GROQ_MODEL", "qwen/other")
    assert make_client("groq").model == "qwen/other"
    assert make_client("ollama").provider == "ollama"  # local: no key needed
