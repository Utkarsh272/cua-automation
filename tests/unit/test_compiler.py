"""The compiler, driven by the real Groq discovery trace (tests/fixtures/traces)."""

from __future__ import annotations

import copy
import json
from pathlib import Path
from typing import Any

import pytest
from ruamel.yaml import YAML

from cua.compiler.compile import CompileError, compile_trace
from cua.core.artifact import OutcomeDecl, RecoverThen, RiskClass, compute_hash, parse_capability
from cua.core.redact import stable_tag
from cua.core.targets import FrameRef
from cua.discovery.trace import DiscoveryTrace

ROOT = Path(__file__).resolve().parents[2]
REAL = ROOT / "tests" / "fixtures" / "traces" / "groq_lookup_success.json"
MAIN = (FrameRef(name="main"),)
EXAMPLE = {"member_id": "10042"}
DECLARED = (
    OutcomeDecl(code="MEMBER_NOT_FOUND", description="No member exists with this ID."),
    OutcomeDecl(code="ACCESS_DENIED", description="Restricted record."),
)


@pytest.fixture
def raw() -> dict[str, Any]:
    return json.loads(REAL.read_text())


def trace(data: dict[str, Any]) -> DiscoveryTrace:
    return DiscoveryTrace.model_validate(data)


def compile_(data: dict[str, Any], **kw: Any) -> Any:
    kw.setdefault("content_frame", MAIN)
    kw.setdefault("login", "login@^1")
    kw.setdefault("example_inputs", EXAMPLE)
    kw.setdefault("declared_outcomes", DECLARED)
    return compile_trace(trace(data), **kw)


def outcome_trace(raw: dict[str, Any], code: str, quote: str, **extra: Any) -> DiscoveryTrace:
    t = copy.deepcopy(raw)
    t.update(
        run_id=f"discovery_outcome_{code.lower()}",
        status="business_outcome",
        verified_outputs={},
        extracted={},
        outcome={
            "code": code,
            "detail": "x",
            "evidence_text": quote,
            "page": {"route": "/search", "title": "CU Core - Member Search"},
        },
    )
    t.update(extra)
    return trace(t)


# --- the real run -----------------------------------------------------------------------------


def test_real_trace_compiles_to_a_valid_reviewable_draft(raw: dict[str, Any]) -> None:
    r = compile_(raw)
    cap = r.capability
    assert cap.status == "draft" and cap.recorded_by == "discovery:groq/openai/gpt-oss-120b"
    assert [s.id for s in cap.steps] == [
        "open_search",
        "enter_member_id",
        "click_search",
        "read_member_name",
        "read_savings_balance",
    ]
    assert cap.steps[1].value == "{{inputs.member_id}}"
    assert [st.by for st in cap.steps[1].target.strategies] == ["label", "attribute"]  # type: ignore[union-attr]
    assert cap.steps[2].wait is not None and cap.steps[4].parse == "currency_usd"
    assert cap.subject == "inputs.member_id" and cap.requires == ("login@^1",)
    assert cap.provenance.origin == "discovery"
    assert cap.provenance.recorded_from_run == "discovery_2026-10-01T16-28-20Z_eb11"
    assert cap.app.versions == ">=4.6 <5" and cap.risk == RiskClass.READ
    # Outcomes are declared, but only a run that hits one can give it a detector.
    assert cap.detectors == ()
    assert sum("has no detector yet" in n.message for n in r.notes) == 2


def test_output_round_trips_and_is_deterministic(raw: dict[str, Any]) -> None:
    a, b = compile_(raw), compile_(raw)
    assert a.yaml == b.yaml
    parsed = parse_capability(YAML(typ="safe").load(a.yaml))
    assert compute_hash(parsed) == compute_hash(a.capability)
    assert "by: label" in a.yaml and "exact: true" not in a.yaml  # discriminators kept, noise not


def test_fingerprints_never_carry_member_data(raw: dict[str, Any]) -> None:
    text = compile_(raw).yaml
    assert "REDACTED" not in text and "identifier:" not in text


# --- what gets dropped ------------------------------------------------------------------------


def test_dead_ends_and_superseded_actions_are_dropped(raw: dict[str, Any]) -> None:
    steps = raw["steps"]
    denied = copy.deepcopy(steps[1]) | {"index": 90, "status": "denied"}
    invalid = copy.deepcopy(steps[0]) | {"index": 91, "status": "invalid"}
    first_fill = copy.deepcopy(steps[0]) | {"index": 92, "args": {"ref": "e7", "value": "oops"}}
    early_extract = copy.deepcopy(steps[3]) | {"index": 93}
    raw["steps"] = [
        first_fill,
        steps[0],
        denied,
        invalid,
        steps[1],
        early_extract,
        steps[2],
        steps[3],
        steps[4],
    ]
    r = compile_(raw)
    assert [s.id for s in r.capability.steps][1:] == [
        "enter_member_id",
        "click_search",
        "read_member_name",
        "read_savings_balance",
    ]
    messages = " ".join(n.message for n in r.notes)
    assert "dead end" in messages and "set again" in messages and "read again" in messages


# --- refusals -----------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("change", "message"),
    [
        ({"status": "failed"}, "need a succeeded run"),
        ({"verified_outputs": {"member_name": True, "savings_balance": False}}, "not verified"),
    ],
)
def test_refuses_unproven_runs(raw: dict[str, Any], change: dict[str, Any], message: str) -> None:
    raw.update(change)
    with pytest.raises(CompileError, match=message):
        compile_(raw)


def test_refuses_steps_without_a_verified_locator(raw: dict[str, Any]) -> None:
    raw["steps"][1]["target"] = None
    raw["steps"][1]["target_note"] = "no unique locator"
    with pytest.raises(CompileError, match="no unique locator"):
        compile_(raw)


def test_refuses_dropdown_options_that_contain_data(raw: dict[str, Any]) -> None:
    sel = copy.deepcopy(raw["steps"][0])
    sel.update(tool="select", args={"ref": "e7", "option": "Checking - [REDACTED:account_number]"})
    raw["steps"].insert(1, sel)
    with pytest.raises(CompileError, match="needs an input"):
        compile_(raw)


# --- parameterization -------------------------------------------------------------------------


def test_identifier_in_a_route_becomes_a_placeholder(raw: dict[str, Any]) -> None:
    nav = copy.deepcopy(raw["steps"][1])
    nav.update(
        tool="navigate",
        args={"route": f"/member/[identifier:{stable_tag('10042')}]/accounts/new"},
        target=None,
        element=None,
        ref=None,
        after={"route": "/member/x/accounts/new", "title": "CU Core - Open Sub-Account"},
    )
    raw["steps"].insert(4, nav)
    r = compile_(raw)
    step = next(
        s for s in r.capability.steps if s.action.value == "navigate" and s.id != "open_search"
    )
    assert step.route == "/member/{{inputs.member_id}}/accounts/new"


def test_literal_equal_to_an_example_input_is_parameterized(raw: dict[str, Any]) -> None:
    raw["steps"][0]["args"]["value"] = "10042"
    r = compile_(raw)
    assert r.capability.steps[1].value == "{{inputs.member_id}}"
    assert any("replaced by an input placeholder" in n.message for n in r.notes)


def test_other_literals_are_flagged_for_review(raw: dict[str, Any]) -> None:
    raw["steps"][0]["args"]["value"] = "Vacation"
    r = compile_(raw)
    assert any("confirm it is a constant" in n.message for n in r.notes)


# --- outcomes, recoveries, risk -----------------------------------------------------------------


def test_outcome_runs_become_detectors_with_quoted_text(raw: dict[str, Any]) -> None:
    nf = outcome_trace(raw, "MEMBER_NOT_FOUND", "No member found for the ID entered.")
    r = compile_(raw, outcome_traces=[nf])
    (det,) = r.capability.detectors
    assert det.id == "member_not_found"
    assert det.when.text_visible == "No member found for the ID entered."  # type: ignore[union-attr]
    wait = r.capability.step("click_search").wait
    assert wait is not None and {"detector": "member_not_found"} in [
        c.model_dump()
        for c in wait.until.any_of  # type: ignore[union-attr]
    ]
    assert sum("has no detector yet" in n.message for n in r.notes) == 1  # ACCESS_DENIED only


def test_undeclared_outcome_is_added_but_flagged(raw: dict[str, Any]) -> None:
    other = outcome_trace(raw, "ACCOUNT_FROZEN", "Account frozen by compliance.")
    r = compile_(raw, outcome_traces=[other])
    assert "ACCOUNT_FROZEN" in {o.code for o in r.capability.outcomes}
    assert any("not declared in the spec" in n.message for n in r.notes)


def test_outcome_quote_containing_data_is_refused(raw: dict[str, Any]) -> None:
    bad = outcome_trace(raw, "MEMBER_NOT_FOUND", "No member [identifier:ab12cd34] here")
    with pytest.raises(CompileError, match="contains data"):
        compile_(raw, outcome_traces=[bad])


def test_outcome_run_for_another_capability_is_refused(raw: dict[str, Any]) -> None:
    other = outcome_trace(raw, "MEMBER_NOT_FOUND", "No member found.", capability_id="other_cap")
    with pytest.raises(CompileError, match="different capability"):
        compile_(raw, outcome_traces=[other])


def test_click_inside_a_dialog_becomes_a_recovery_not_a_step(raw: dict[str, Any]) -> None:
    ack = copy.deepcopy(raw["steps"][2])
    ack.update(index=50, tool="click", output=None, parser=None, args={"ref": "e30"})
    ack["element"].update(role="button", name="Acknowledge", label="", dialog="System Notice")
    ack["target"]["strategies"] = [{"by": "role", "role": "button", "name": "Acknowledge"}]
    raw["steps"].insert(2, ack)
    r = compile_(raw)
    assert "click_acknowledge" not in [s.id for s in r.capability.steps]
    (rec,) = r.capability.detectors
    assert rec.id == "dismiss_system_notice" and isinstance(rec.then, RecoverThen)
    assert rec.when.dialog_title == "System Notice"  # type: ignore[union-attr]


def test_irreversible_step_requires_confirmation(raw: dict[str, Any]) -> None:
    raw["steps"][1]["policy"] = {
        "verdict": "allow",
        "effective_risk": "irreversible",
        "reason": "x",
    }
    r = compile_(raw)
    step = r.capability.step("click_search")
    assert step.risk == RiskClass.IRREVERSIBLE and step.requires_confirmation
    assert r.capability.risk == RiskClass.IRREVERSIBLE


def test_scripted_runs_are_labelled(raw: dict[str, Any]) -> None:
    raw["provider"] = "scripted"
    assert any("scripted run" in n.message for n in compile_(raw).notes)


def test_review_notes_render(raw: dict[str, Any]) -> None:
    md = compile_(raw).review_markdown
    assert md.startswith("# Review: lookup_savings_balance@1.0.0")
    assert "cua approve lookup_savings_balance 1.0.0" in md
