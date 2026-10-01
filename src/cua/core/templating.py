"""Placeholder rendering: ``{{inputs.member_id}}`` and ``{{secrets.cu_core.password}}``.

Artifacts and LLM prompts only ever contain placeholders. Real values are substituted at the last
moment, inside the process, right before the surface types them.

Secrets resolve from the environment: ``{{secrets.cu_core.password}}`` reads ``CU_CORE_PASSWORD``.
"""

from __future__ import annotations

import os
import re
from collections.abc import Callable, Mapping
from typing import Any

TEMPLATE = re.compile(r"\{\{\s*([^}]+?)\s*\}\}")
_INPUT = re.compile(r"^inputs\.([a-z][a-z0-9_]*)$")
_SECRET = re.compile(r"^secrets\.([a-z][a-z0-9_]*)\.([a-z][a-z0-9_]*)$")


class TemplateError(KeyError):
    pass


def env_secrets(environ: Mapping[str, str] | None = None) -> Callable[[str, str], str]:
    env = os.environ if environ is None else environ

    def lookup(group: str, key: str) -> str:
        name = f"{group}_{key}".upper()
        value = env.get(name)
        if not value:
            raise TemplateError(f"secret {group}.{key} is not set (expected env {name})")
        return value

    return lookup


def render(
    text: str,
    inputs: Mapping[str, Any],
    secrets: Callable[[str, str], str] | None = None,
) -> str:
    def sub(m: re.Match[str]) -> str:
        ref = m.group(1)
        if mi := _INPUT.match(ref):
            if mi.group(1) not in inputs:
                raise TemplateError(f"unknown input {ref}")
            return str(inputs[mi.group(1)])
        if ms := _SECRET.match(ref):
            if secrets is None:
                raise TemplateError(f"no secret resolver for {ref}")
            return secrets(ms.group(1), ms.group(2))
        raise TemplateError(f"bad placeholder {{{{{ref}}}}}")

    return TEMPLATE.sub(sub, text)


def placeholders(text: str) -> list[str]:
    return TEMPLATE.findall(text)


def is_pure_placeholder(text: str) -> bool:
    return bool(TEMPLATE.fullmatch(text.strip()))
