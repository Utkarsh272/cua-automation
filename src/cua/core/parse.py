"""Turn text read off a screen into typed output values.

Parsers are strict: text that does not look like the declared type raises ``ParseError``
instead of guessing, so a layout change surfaces as ``OUTPUT_INVALID`` rather than a wrong
number returned to a caller.
"""

from __future__ import annotations

import re
from datetime import date
from decimal import Decimal, InvalidOperation

_WS = re.compile(r"\s+")
_CURRENCY = re.compile(
    r"^(?P<neg1>-)?\(?(?P<neg2>-)?\$?\s*"
    r"(?P<num>\d{1,3}(?:,\d{3})*|\d+)(?P<dec>\.\d{1,2})?\)?$"
)
_DATE_MDY = re.compile(r"^(\d{1,2})/(\d{1,2})/(\d{4})$")


class ParseError(ValueError):
    pass


def clean_text(raw: str) -> str:
    return _WS.sub(" ", raw).strip()


def parse_currency_usd(raw: str) -> Decimal:
    """``$1,204.50`` -> 1204.50, ``(12.00)`` -> -12.00, ``-$5`` -> -5.00."""
    # Tolerate "$ 5.00" or "( 12.00)", but never a space inside the digits ("1 204").
    text = re.sub(r"(?<=[$(\-])\s+|\s+(?=\))", "", clean_text(raw))
    m = _CURRENCY.match(text)
    if not m:
        raise ParseError(f"not a USD amount: {raw!r}")
    parenthesized = text.startswith("(") and text.endswith(")")
    if text.count("(") != text.count(")"):
        raise ParseError(f"unbalanced parentheses: {raw!r}")
    try:
        value = Decimal(m.group("num").replace(",", "") + (m.group("dec") or ""))
    except InvalidOperation as exc:  # pragma: no cover - regex already constrains it
        raise ParseError(raw) from exc
    negative = parenthesized or bool(m.group("neg1") or m.group("neg2"))
    return (-value if negative else value).quantize(Decimal("0.01"))


def parse_integer(raw: str) -> int:
    text = clean_text(raw).replace(",", "")
    if not re.fullmatch(r"-?\d+", text):
        raise ParseError(f"not an integer: {raw!r}")
    return int(text)


def parse_date_mdy(raw: str) -> str:
    m = _DATE_MDY.match(clean_text(raw))
    if not m:
        raise ParseError(f"not a MM/DD/YYYY date: {raw!r}")
    try:
        return date(int(m.group(3)), int(m.group(1)), int(m.group(2))).isoformat()
    except ValueError as exc:
        raise ParseError(f"not a real date: {raw!r}") from exc


def parse_value(parser: str | None, raw: str) -> str | int | float:
    """Apply a named parser. Currency is returned as float for JSON outputs (2-dp exact)."""
    if parser in (None, "text"):
        return clean_text(raw)
    if parser == "currency_usd":
        return float(parse_currency_usd(raw))
    if parser == "integer":
        return parse_integer(raw)
    if parser == "date_mdy":
        return parse_date_mdy(raw)
    raise ParseError(f"unknown parser {parser!r}")
