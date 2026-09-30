"""Redaction for everything that leaves the process: logs, evidence, artifacts, LLM prompts.

Two layers, applied in this order:

1. **Contract-driven, by value.** Every input and output value whose field is tagged with a
   non-public ``x-sensitivity`` is masked wherever it appears, including its common display
   formats (``1204.5`` also masks ``$1,204.50``). Secrets are always masked. Identifiers are
   replaced with a short stable hash so runs can still be correlated without the raw value.
2. **Patterns, as a safety net.** SSNs, Luhn-valid card numbers, emails, US phone numbers, plus
   app-specific patterns from the app profile (for CU Core, account numbers).

Dict keys that name credentials (``password``, ``token``, ``cookie``...) have their whole value
masked regardless of content.

There is one entry point for writers (``Redactor.redact``). A test scans every file the suite
writes for known synthetic secrets, so "never persist secrets" is checked, not just claimed.
"""

from __future__ import annotations

import hashlib
import re
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation
from functools import partial
from typing import Any

SENSITIVE_KEY = re.compile(
    r"(?i)(pass(word|wd)?|pwd|secret|token|api[_-]?key|authorization|cookie|session[_-]?id)"
)

# Sensitivity labels that are masked. "public" is never masked.
MASKED = ("identifier", "pii", "financial", "secret")


@dataclass(frozen=True)
class Pattern:
    label: str
    regex: re.Pattern[str]
    luhn: bool = False


def _luhn_ok(digits: str) -> bool:
    total = 0
    for i, ch in enumerate(reversed(digits)):
        d = int(ch)
        if i % 2 == 1:
            d *= 2
            if d > 9:
                d -= 9
        total += d
    return total % 10 == 0


DEFAULT_PATTERNS: tuple[Pattern, ...] = (
    Pattern("ssn", re.compile(r"\b\d{3}-\d{2}-\d{4}\b")),
    Pattern("card", re.compile(r"\b(?:\d[ -]?){12,18}\d\b"), luhn=True),
    Pattern("email", re.compile(r"\b[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}\b")),
    Pattern("phone", re.compile(r"(?:\(\d{3}\)\s?|\b\d{3}[-.])\d{3}[-.]\d{4}\b")),
)


def compile_patterns(spec: Mapping[str, str]) -> tuple[Pattern, ...]:
    """App-profile patterns, e.g. ``{"account_number": r"\\b\\d{4}-\\d{3}-\\d{4}\\b"}``."""
    return tuple(Pattern(label, re.compile(rx)) for label, rx in spec.items())


def stable_tag(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()[:8]


def _variants(value: Any) -> set[str]:
    """Display forms a value can take on screen or in a log."""
    out: set[str] = set()
    if isinstance(value, bool) or value is None:
        return out
    text = str(value).strip()
    if text:
        out.add(text)
    try:
        num = Decimal(text.replace("$", "").replace(",", ""))
    except InvalidOperation:
        return out
    if not num.is_finite():
        return out
    for n in (num, abs(num)):
        out.update({f"{n:.2f}", f"{n:,.2f}", f"${n:,.2f}", format(n.normalize(), "f")})
    if num < 0:
        out.add(f"({abs(num):,.2f})")
    return {v for v in out if len(v) >= 3}


@dataclass
class Redactor:
    patterns: tuple[Pattern, ...] = DEFAULT_PATTERNS
    _known: dict[str, str] = field(default_factory=dict)  # display variant -> replacement

    # --- building ---------------------------------------------------------------------------

    def add(self, value: Any, sensitivity: str) -> None:
        if sensitivity not in MASKED:
            return
        for variant in _variants(value):
            if sensitivity == "identifier":
                self._known[variant] = f"[identifier:{stable_tag(str(value))}]"
            else:
                self._known[variant] = f"[REDACTED:{sensitivity}]"

    def add_secrets(self, secrets: Iterable[str]) -> None:
        for s in secrets:
            if s:
                self._known[s] = "[REDACTED:secret]"

    @classmethod
    def for_run(
        cls,
        sensitivities: Mapping[str, str],
        values: Mapping[str, Any],
        *,
        secrets: Iterable[str] = (),
        extra_patterns: Mapping[str, str] | None = None,
    ) -> Redactor:
        """Build from a capability's field sensitivities and the concrete values of this run."""
        r = cls(patterns=compile_patterns(extra_patterns or {}) + DEFAULT_PATTERNS)
        for name, value in values.items():
            r.add(value, sensitivities.get(name, "pii"))  # unknown fields: assume sensitive
        r.add_secrets(secrets)
        return r

    # --- applying ---------------------------------------------------------------------------

    def redact_text(self, text: str) -> str:
        for known in sorted(self._known, key=len, reverse=True):
            if known in text:
                text = text.replace(known, self._known[known])
        for p in self.patterns:
            text = p.regex.sub(partial(self._pattern_sub, p=p), text)
        return text

    def _pattern_sub(self, m: re.Match[str], p: Pattern) -> str:
        if p.luhn:
            digits = re.sub(r"\D", "", m.group(0))
            if not _luhn_ok(digits):
                return m.group(0)
        return f"[REDACTED:{p.label}]"

    def redact(self, obj: Any) -> Any:
        """Recursively redact strings, numbers, lists and dicts. Returns a new object."""
        if isinstance(obj, str):
            return self.redact_text(obj)
        if isinstance(obj, Mapping):
            out: dict[Any, Any] = {}
            for k, v in obj.items():
                if isinstance(k, str) and SENSITIVE_KEY.search(k) and v not in (None, ""):
                    out[k] = "[REDACTED:secret]"
                else:
                    out[k] = self.redact(v)
            return out
        if isinstance(obj, list | tuple):
            return type(obj)(self.redact(v) for v in obj)
        if isinstance(obj, int | float | Decimal) and not isinstance(obj, bool):
            masked = self.redact_text(str(obj))
            return obj if masked == str(obj) else masked
        return obj


def find_sensitive(text: str, patterns: tuple[Pattern, ...] = DEFAULT_PATTERNS) -> list[str]:
    """Labels of sensitive-looking literals in ``text``. Used to keep them out of artifacts."""
    found: list[str] = []
    for p in patterns:
        for m in p.regex.finditer(text):
            if p.luhn and not _luhn_ok(re.sub(r"\D", "", m.group(0))):
                continue
            found.append(p.label)
    return found
