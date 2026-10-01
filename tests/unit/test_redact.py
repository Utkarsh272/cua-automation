from __future__ import annotations

from decimal import Decimal

import pytest

from cua.core.redact import Redactor, find_sensitive, stable_tag

ACCOUNT_RX = {"account_number": r"\b\d{4}-\d{3}-\d{4}\b"}


def run_redactor() -> Redactor:
    return Redactor.for_run(
        {"member_id": "identifier", "member_name": "pii", "savings_balance": "financial"},
        {
            "member_id": "10042",
            "member_name": "Maria Delgado",
            "savings_balance": Decimal("1204.50"),
        },
        secrets=["Tr0ub4dor&3-synthetic"],
        extra_patterns=ACCOUNT_RX,
    )


def test_contract_values_masked_in_all_display_forms() -> None:
    r = run_redactor()
    out = r.redact_text("Maria Delgado savings $1,204.50 (raw 1204.50, 1204.5)")
    assert "Maria" not in out and "1,204" not in out and "1204" not in out
    assert out.count("[REDACTED:financial]") == 3 and "[REDACTED:pii]" in out


def test_identifiers_become_stable_tags() -> None:
    out = run_redactor().redact_text("member 10042 opened")
    assert out == f"member [identifier:{stable_tag('10042')}] opened"


def test_secrets_always_masked() -> None:
    assert "Tr0ub4dor" not in run_redactor().redact_text("pwd=Tr0ub4dor&3-synthetic")


@pytest.mark.parametrize(
    ("text", "label"),
    [
        ("ssn 123-45-6789", "ssn"),
        ("card 4111 1111 1111 1111", "card"),
        ("mail maria.delgado@example.com", "email"),
        ("call (555) 201-4432", "phone"),
        ("call 555-201-4432", "phone"),
        ("acct 0042-002-7731", "account_number"),
    ],
)
def test_patterns(text: str, label: str) -> None:
    assert f"[REDACTED:{label}]" in run_redactor().redact_text(text)


def test_card_pattern_requires_luhn() -> None:
    assert "1234 5678 9012 3456" in Redactor().redact_text("ref 1234 5678 9012 3456")


def test_dates_and_member_ids_are_not_pattern_matched() -> None:
    text = "2026-09-30 member 10077 step 3"
    assert Redactor().redact_text(text) == text


def test_structures_and_sensitive_keys() -> None:
    r = run_redactor()
    event = {
        "step": "enter_member_id",
        "value": "10042",
        "headers": {"Cookie": "cu_sid=abc", "Authorization": "Bearer x"},
        "password": "whatever",
        "outputs": {"savings_balance": Decimal("1204.50")},
        "list": ["Maria Delgado", 3],
        "empty_token": "",
    }
    out = r.redact(event)
    assert out["headers"] == {"Cookie": "[REDACTED:secret]", "Authorization": "[REDACTED:secret]"}
    assert out["password"] == "[REDACTED:secret]"
    assert out["outputs"]["savings_balance"] == "[REDACTED:financial]"
    assert out["list"] == ["[REDACTED:pii]", 3]
    assert out["value"].startswith("[identifier:")
    assert out["empty_token"] == ""
    assert event["password"] == "whatever"  # input untouched


def test_public_fields_are_not_masked() -> None:
    r = Redactor.for_run({"status": "public"}, {"status": "Open"})
    assert r.redact_text("Status Open") == "Status Open"


def test_unknown_fields_default_to_sensitive() -> None:
    r = Redactor.for_run({}, {"mystery": "Hidden Value"})
    assert "Hidden" not in r.redact_text("the Hidden Value")


def test_find_sensitive_for_artifact_literals() -> None:
    assert find_sensitive("123-45-6789") == ["ssn"]
    assert find_sensitive("{{inputs.member_id}}") == []


def test_sensitive_keys_are_whole_names_not_substrings() -> None:
    r = Redactor()
    out = r.redact(
        {
            "input_tokens": 120,
            "output_tokens": 9,
            "access_token": "abc",
            "Set-Cookie": "sid=1",
            "api_key": "k",
            "password": "p",
            "tokenizer": "bpe",
        }
    )
    assert out["input_tokens"] == 120 and out["output_tokens"] == 9 and out["tokenizer"] == "bpe"
    assert {out[k] for k in ("access_token", "Set-Cookie", "api_key", "password")} == {
        "[REDACTED:secret]"
    }
