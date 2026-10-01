from __future__ import annotations

from decimal import Decimal

import pytest

from cua.core.parse import ParseError, parse_currency_usd, parse_value


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("$1,204.50", "1204.50"),
        ("(12.00)", "-12.00"),
        ("-$5", "-5.00"),
        ("$ 5,000.00", "5000.00"),
        ("25", "25.00"),
        ("  $12,940.33 ", "12940.33"),
    ],
)
def test_currency(raw: str, expected: str) -> None:
    assert parse_currency_usd(raw) == Decimal(expected)


@pytest.mark.parametrize("raw", ["$1,20.50", "abc", "(12.00", "$1.234", "", "1 204"])
def test_currency_rejects(raw: str) -> None:
    with pytest.raises(ParseError):
        parse_currency_usd(raw)


def test_parse_value_dispatch() -> None:
    assert parse_value("text", "  Maria   Delgado ") == "Maria Delgado"
    assert parse_value(None, "x") == "x"
    assert parse_value("currency_usd", "(12.00)") == -12.0
    assert parse_value("integer", "1,024") == 1024
    assert parse_value("date_mdy", "03/14/1986") == "1986-03-14"
    with pytest.raises(ParseError):
        parse_value("date_mdy", "02/30/2020")
    with pytest.raises(ParseError):
        parse_value("roman", "XII")
