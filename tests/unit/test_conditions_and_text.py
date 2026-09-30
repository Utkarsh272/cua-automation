from __future__ import annotations

from typing import Any

import pytest
from pydantic import TypeAdapter, ValidationError

from cua.core.conditions import (
    Condition,
    ConditionError,
    PageState,
    evaluate,
    referenced_detectors,
    referenced_outputs,
)
from cua.core.targets import TargetSpec
from cua.core.text import canonicalize_route, match_route, normalize

C: TypeAdapter[Condition] = TypeAdapter(Condition)


def cond(data: Any) -> Condition:
    return C.validate_python(data)


DETAIL = PageState(
    route="/member/10042",
    title="CU Core - Member Detail",
    visible_text="Member Detail  Accounts Savings $1,204.50",
    dialog_titles=("System Notice",),
    outputs={"savings_balance": 1204.5, "member_name": None},
    fired_detectors=frozenset({"system_notice"}),
)


@pytest.mark.parametrize(
    ("data", "expected"),
    [
        ({"title_contains": "member detail"}, True),
        ({"text_visible": "savings  $1,204.50"}, True),
        ({"text_visible": "No member found"}, False),
        ({"route": "/member/:member_id"}, True),
        ({"route": "/search"}, False),
        ({"dialog_title": "system notice:"}, True),
        ({"detector": "system_notice"}, True),
        ({"output_present": "savings_balance"}, True),
        ({"output_present": "member_name"}, False),
        ({"all_of": [{"route": "/member/:id"}, {"title_contains": "Detail"}]}, True),
        ({"any_of": [{"route": "/search"}, {"detector": "nope"}]}, False),
        ({"not": {"route": "/search"}}, True),
    ],
)
def test_evaluate(data: Any, expected: bool) -> None:
    assert evaluate(cond(data), DETAIL) is expected


def test_single_key_conditions_only() -> None:
    with pytest.raises(ValidationError):
        cond({"title_contains": "a", "route": "/x"})
    with pytest.raises(ValidationError):
        cond({"sleep": 5})


def test_element_needs_probe_and_uses_it() -> None:
    target = {"description": "Savings row", "strategies": [{"by": "text", "text": "Savings"}]}
    c = cond({"not": {"element": target}})
    with pytest.raises(ConditionError):
        evaluate(c, DETAIL)
    seen: list[TargetSpec] = []

    def probe(t: TargetSpec) -> bool:
        seen.append(t)
        return False

    state = PageState(element_probe=probe)
    assert evaluate(c, state) is True and seen[0].description == "Savings row"


def test_short_circuit_avoids_probe() -> None:
    target = {"description": "Savings row", "strategies": [{"by": "text", "text": "Savings"}]}
    c = cond({"all_of": [{"title_contains": "Member Detail"}, {"element": target}]})
    assert evaluate(c, PageState(title="Sign On")) is False  # probe never needed


def test_references() -> None:
    c = cond(
        {
            "any_of": [
                {"detector": "a"},
                {"not": {"all_of": [{"detector": "b"}, {"output_present": "x"}]}},
            ]
        }
    )
    assert referenced_detectors(c) == {"a", "b"}
    assert referenced_outputs(c) == {"x"}


def test_serialization_uses_not_alias() -> None:
    assert C.dump_python(cond({"not": {"route": "/x"}}), by_alias=True) == {"not": {"route": "/x"}}


@pytest.mark.parametrize(
    ("a", "b"),
    [
        ("Member ID:", "member id"),
        ("  Member   ID ", "Member ID"),
        ("Share Savings", "share savings"),
    ],
)
def test_normalize(a: str, b: str) -> None:
    assert normalize(a) == normalize(b)


def test_match_route() -> None:
    assert match_route("/member/:member_id", "/member/10042?x=1") == {"member_id": "10042"}
    assert match_route("/member/:member_id", "/member/10042/accounts/new") is None
    assert match_route("/frame/*", "/frame/top") == {}
    assert match_route("/", "http://localhost:8080/") == {}
    assert match_route("/search", "/search/quick") is None


def test_canonicalize_route() -> None:
    assert canonicalize_route("/member/10042/accounts/new", {"member_id": "10042"}) == (
        "/member/:member_id/accounts/new"
    )
