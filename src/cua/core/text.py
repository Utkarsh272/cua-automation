"""Text normalization and route patterns shared by locators, conditions and policy."""

from __future__ import annotations

import re
from urllib.parse import urlsplit

_WS = re.compile(r"\s+")
_PARAM = re.compile(r"^:([a-z_][a-z0-9_]*)$")


def normalize(text: str) -> str:
    """Compare labels the way a person reads them.

    Collapses whitespace, drops a trailing colon, and case-folds, so ``"Member ID:"``,
    ``"member id"`` and ``" Member  ID "`` compare equal.
    """
    collapsed = _WS.sub(" ", text).strip()
    if collapsed.endswith(":"):
        collapsed = collapsed[:-1].rstrip()
    return collapsed.casefold()


def contains(haystack: str, needle: str) -> bool:
    return normalize(needle) in normalize(haystack)


def route_of(url_or_path: str) -> str:
    """The path of a URL, without query or fragment. Accepts a bare path too."""
    path = urlsplit(url_or_path).path or "/"
    return path if path.startswith("/") else "/" + path


def match_route(pattern: str, path: str) -> dict[str, str] | None:
    """Match ``/member/:member_id`` against ``/member/10042`` and return the parameters.

    A trailing ``/*`` matches any remaining segments. Returns ``None`` when there is no match.
    """
    p_parts = [p for p in route_of(pattern).split("/") if p]
    a_parts = [p for p in route_of(path).split("/") if p]
    wildcard = bool(p_parts) and p_parts[-1] == "*"
    if wildcard:
        p_parts = p_parts[:-1]
        if len(a_parts) < len(p_parts):
            return None
        a_parts = a_parts[: len(p_parts)]
    elif len(p_parts) != len(a_parts):
        return None
    params: dict[str, str] = {}
    for pat, actual in zip(p_parts, a_parts, strict=True):
        m = _PARAM.match(pat)
        if m:
            params[m.group(1)] = actual
        elif pat != actual:
            return None
    return params


def canonicalize_route(path: str, params: dict[str, str]) -> str:
    """Replace concrete segments with named parameters: ``/member/10042`` -> ``/member/:member_id``.

    Used by the compiler so recorded routes are not tied to the member used during discovery.
    """
    by_value = {v: k for k, v in params.items() if v}
    parts = route_of(path).split("/")
    return "/".join(f":{by_value[p]}" if p in by_value else p for p in parts) or "/"
