"""Minimal semantic versioning: parse, compare, and match constraints.

Supported constraints: ``1.2.3`` (exact), ``^1``/``^1.2``, ``~1.2``, ``*``, and space-separated
comparator sets such as ``>=4.2 <5``. Partial versions are zero-padded (``4.2`` = ``4.2.0``).

Meaning for capabilities: patch = locator repair, minor = new optional output, major = changed
inputs or outcomes. Callers pin a major version (``lookup_savings_balance@^1``).
"""

from __future__ import annotations

import re
from dataclasses import dataclass

_VERSION = re.compile(r"^(\d+)(?:\.(\d+))?(?:\.(\d+))?$")
_COMPARATOR = re.compile(r"^(>=|<=|>|<|=)?\s*(\d+(?:\.\d+){0,2})$")
_REQUIREMENT = re.compile(r"^([a-z][a-z0-9_]*)@(.+)$")


@dataclass(frozen=True, order=True)
class Version:
    major: int
    minor: int = 0
    patch: int = 0

    def __str__(self) -> str:
        return f"{self.major}.{self.minor}.{self.patch}"


def parse_version(text: str) -> Version:
    m = _VERSION.match(text.strip())
    if not m:
        raise ValueError(f"not a version: {text!r}")
    return Version(int(m.group(1)), int(m.group(2) or 0), int(m.group(3) or 0))


def is_strict_version(text: str) -> bool:
    """True for a full ``MAJOR.MINOR.PATCH`` string, as required for artifact versions."""
    return re.fullmatch(r"\d+\.\d+\.\d+", text) is not None


def satisfies(version: str | Version, constraint: str) -> bool:
    v = version if isinstance(version, Version) else parse_version(version)
    c = constraint.strip()
    if c in ("", "*"):
        return True
    if c.startswith("^"):
        base = parse_version(c[1:])
        parts = c[1:].count(".") + 1
        if base.major > 0 or parts == 1:
            upper = Version(base.major + 1)
        elif parts == 2 or base.minor > 0:
            upper = Version(0, base.minor + 1)
        else:
            upper = Version(0, 0, base.patch + 1)
        return base <= v < upper
    if c.startswith("~"):
        base = parse_version(c[1:])
        return base <= v < Version(base.major, base.minor + 1)
    for token in c.split():
        m = _COMPARATOR.match(token)
        if not m:
            raise ValueError(f"not a version constraint: {constraint!r}")
        op, target = m.group(1) or "=", parse_version(m.group(2))
        ok = {
            ">=": v >= target,
            "<=": v <= target,
            ">": v > target,
            "<": v < target,
            "=": v == target,
        }[op]
        if not ok:
            return False
    return True


def parse_requirement(text: str) -> tuple[str, str]:
    """``"login@^1"`` -> ``("login", "^1")``. The constraint is validated."""
    m = _REQUIREMENT.match(text.strip())
    if not m:
        raise ValueError(f"requirement must look like 'capability_id@constraint': {text!r}")
    satisfies("0.0.0", m.group(2))  # raises if malformed
    return m.group(1), m.group(2)
