from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

import pytest
from pydantic import ValidationError

from cua.core.results import (
    BusinessOutcome,
    Failure,
    FailureCode,
    NeedsHuman,
    Success,
    make_failure,
    parse_result,
    result_json,
)
from cua.core.semver import parse_requirement, parse_version, satisfies

META: dict[str, Any] = {
    "run_id": "run_1",
    "capability": "lookup_savings_balance",
    "version": "1.0.0",
    "tenant": "tenant-a",
    "started_at": datetime(2026, 9, 30, tzinfo=UTC),
    "duration_ms": 1200,
}


def test_discriminated_round_trip() -> None:
    for result in (
        Success(outputs={"savings_balance": 1204.5}, **META),
        BusinessOutcome(
            code="MEMBER_NOT_FOUND", detail="No member found", at_step="submit_search", **META
        ),
        make_failure(
            code=FailureCode.APP_ERROR,
            at_step="submit_search",
            expected="Member Detail",
            observed="Runtime Error",
            **META,
        ),
        NeedsHuman(intervention_id="int_1", reason="unknown dialog", **META),
    ):
        import json

        again = parse_result(json.loads(result_json(result)))
        assert again == result and type(again) is type(result)


def test_replay_records_zero_llm_calls_by_default() -> None:
    assert Success(outputs={}, **META).llm_calls == 0


def test_outcome_codes_are_upper_snake() -> None:
    with pytest.raises(ValidationError):
        BusinessOutcome(code="member not found", detail="", **META)


def test_default_retry_rules() -> None:
    timeout = make_failure(code=FailureCode.TIMEOUT, at_step="s", expected="", observed="", **META)
    assert timeout.retryable
    target = make_failure(
        code=FailureCode.TARGET_NOT_FOUND, at_step="s", expected="", observed="", **META
    )
    assert not target.retryable


def test_never_retryable_after_possible_side_effects() -> None:
    f = make_failure(
        code=FailureCode.TIMEOUT,
        at_step="confirm",
        expected="",
        observed="",
        side_effects="possible",
        **META,
    )
    assert not f.retryable
    with pytest.raises(ValidationError, match="cannot be retryable"):
        Failure(
            code=FailureCode.TIMEOUT,
            at_step="confirm",
            expected="",
            observed="",
            retryable=True,
            side_effects="committed",
            **META,
        )


def test_results_are_immutable() -> None:
    s = Success(outputs={}, **META)
    with pytest.raises(ValidationError):
        s.degraded = True  # type: ignore[misc]


# --- semver -----------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("version", "constraint", "ok"),
    [
        ("4.6.2", ">=4.2 <5", True),
        ("5.0.0", ">=4.2 <5", False),
        ("4.1.9", ">=4.2 <5", False),
        ("1.4.0", "^1", True),
        ("2.0.0", "^1", False),
        ("0.3.1", "^0.3", True),
        ("0.4.0", "^0.3", False),
        ("1.2.9", "~1.2", True),
        ("1.3.0", "~1.2", False),
        ("1.0.0", "1.0.0", True),
        ("9.9.9", "*", True),
    ],
)
def test_satisfies(version: str, constraint: str, ok: bool) -> None:
    assert satisfies(version, constraint) is ok


def test_parse() -> None:
    assert str(parse_version("4.2")) == "4.2.0"
    assert parse_requirement("login@^1") == ("login", "^1")
    with pytest.raises(ValueError):
        parse_requirement("login")
    with pytest.raises(ValueError):
        satisfies("1.0.0", "about 1")
