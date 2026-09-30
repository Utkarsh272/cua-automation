from __future__ import annotations

from typing import Any

import pytest

from cua.core.artifact import (
    ArtifactError,
    Capability,
    RiskClass,
    compute_hash,
    dump_capability,
    parse_capability,
    to_tool_definition,
    validate_inputs,
    validate_outputs,
    with_hash,
)
from cua.core.results import FailureCode


def build(data: dict[str, Any]) -> Capability:
    return parse_capability(data)


def rejects(data: dict[str, Any], fragment: str) -> None:
    with pytest.raises(ArtifactError) as info:
        parse_capability(data)
    assert fragment in str(info.value)


# --- the reference artifact -------------------------------------------------------------------


def test_reference_artifact_parses(lookup_data: dict[str, Any]) -> None:
    cap = build(lookup_data)
    assert cap.ref == "lookup_savings_balance@1.0.0"
    assert [s.id for s in cap.steps][:2] == ["open_search", "enter_member_id"]
    assert cap.sensitivities() == {
        "member_id": "identifier",
        "member_name": "pii",
        "savings_balance": "financial",
    }
    assert cap.step("read_savings_balance").parse == "currency_usd"


def test_detector_then_kinds_are_distinguished(lookup_data: dict[str, Any]) -> None:
    cap = build(lookup_data)
    kinds = {d.id: type(d.then).__name__ for d in cap.detectors}
    assert kinds == {
        "member_not_found": "OutcomeThen",
        "access_denied": "OutcomeThen",
        "no_savings_account": "OutcomeThen",
        "system_notice": "RecoverThen",
    }


def test_round_trip_through_yaml_is_stable(lookup_data: dict[str, Any]) -> None:
    from ruamel.yaml import YAML

    cap = build(lookup_data)
    again = parse_capability(YAML(typ="safe").load(dump_capability(cap)))
    assert again.content_hash == compute_hash(cap)
    assert compute_hash(again) == compute_hash(cap)


# --- hashing ----------------------------------------------------------------------------------


def test_hash_ignores_approval_but_not_behavior(lookup_data: dict[str, Any]) -> None:
    cap = with_hash(build(lookup_data))
    approved = cap.model_copy(update={"status": "approved", "approved_by": "reviewer"})
    assert compute_hash(approved) == cap.content_hash

    lookup_data["steps"][1]["target"]["strategies"][0]["text"] = "Member Number"
    assert compute_hash(build(lookup_data)) != cap.content_hash


def test_tampered_artifact_is_refused(lookup_data: dict[str, Any]) -> None:
    lookup_data["content_hash"] = compute_hash(build(lookup_data))
    lookup_data["steps"][0]["route"] = "/admin/transfer"
    rejects(lookup_data, "content_hash mismatch")


def test_approved_needs_hash_and_approver(lookup_data: dict[str, Any]) -> None:
    lookup_data["status"] = "approved"
    rejects(lookup_data, "approved_by")
    lookup_data["approved_by"] = "reviewer"
    rejects(lookup_data, "must carry a content_hash")


# --- consistency rules ------------------------------------------------------------------------


def test_unknown_input_in_template(lookup_data: dict[str, Any]) -> None:
    lookup_data["steps"][1]["value"] = "{{inputs.account_no}}"
    rejects(lookup_data, "unknown input")


def test_bad_template_reference(lookup_data: dict[str, Any]) -> None:
    lookup_data["steps"][1]["value"] = "{{env.HOME}}"
    rejects(lookup_data, "bad template")


def test_secret_template_is_allowed(lookup_data: dict[str, Any]) -> None:
    lookup_data["steps"][1]["value"] = "{{secrets.cu_core.username}}"
    build(lookup_data)


def test_sensitive_literal_is_rejected(lookup_data: dict[str, Any]) -> None:
    lookup_data["steps"][1]["value"] = "123-45-6789"
    rejects(lookup_data, "sensitive literal")


def test_detector_outcome_must_be_declared(lookup_data: dict[str, Any]) -> None:
    lookup_data["outcomes"] = [o for o in lookup_data["outcomes"] if o["code"] != "ACCESS_DENIED"]
    rejects(lookup_data, "undeclared outcome ACCESS_DENIED")


def test_unknown_detector_reference(lookup_data: dict[str, Any]) -> None:
    lookup_data["steps"][2]["wait"]["until"]["any_of"].append({"detector": "ghost"})
    rejects(lookup_data, "unknown detector 'ghost'")


def test_required_output_must_be_extracted(lookup_data: dict[str, Any]) -> None:
    lookup_data["steps"] = [s for s in lookup_data["steps"] if s["id"] != "read_savings_balance"]
    rejects(lookup_data, "required outputs never extracted")


def test_extract_must_target_declared_output(lookup_data: dict[str, Any]) -> None:
    lookup_data["steps"][3]["output"] = "ssn"
    rejects(lookup_data, "undeclared outputs")


def test_duplicate_step_ids(lookup_data: dict[str, Any]) -> None:
    lookup_data["steps"][1]["id"] = "open_search"
    rejects(lookup_data, "duplicate step ids")


def test_capability_risk_must_cover_riskiest_step(lookup_data: dict[str, Any]) -> None:
    lookup_data["steps"][2]["risk"] = "reversible_write"
    rejects(lookup_data, "below its riskiest step")


def test_irreversible_step_must_require_confirmation(lookup_data: dict[str, Any]) -> None:
    lookup_data["risk"] = "irreversible"
    lookup_data["steps"][2]["risk"] = "irreversible"
    rejects(lookup_data, "must set requires_confirmation")
    lookup_data["steps"][2]["requires_confirmation"] = True
    cap = build(lookup_data)
    assert cap.risk == RiskClass.IRREVERSIBLE
    assert not cap.step("submit_search").repeatable


def test_irreversible_step_is_never_safe_to_repeat(lookup_data: dict[str, Any]) -> None:
    lookup_data["risk"] = "irreversible"
    lookup_data["steps"][2].update(
        risk="irreversible", requires_confirmation=True, safe_to_repeat=True
    )
    rejects(lookup_data, "never safe to repeat")


def test_step_shape_rules(lookup_data: dict[str, Any]) -> None:
    lookup_data["steps"][0].pop("route")
    rejects(lookup_data, "navigate needs a route")


def test_extract_steps_are_read_only(lookup_data: dict[str, Any]) -> None:
    lookup_data["risk"] = "reversible_write"
    lookup_data["steps"][4]["risk"] = "reversible_write"
    rejects(lookup_data, "extract steps are always read")


def test_coordinates_only_as_last_strategy(lookup_data: dict[str, Any]) -> None:
    strategies = lookup_data["steps"][1]["target"]["strategies"]
    strategies.insert(0, {"by": "coordinates", "x": 10, "y": 20})
    rejects(lookup_data, "coordinates can only be the last strategy")


def test_subject_must_be_a_declared_input(lookup_data: dict[str, Any]) -> None:
    lookup_data["subject"] = "inputs.account_number"
    rejects(lookup_data, "subject must be")


def test_override_must_target_known_step(lookup_data: dict[str, Any]) -> None:
    lookup_data["overrides"]["tenant-b"][0]["path"] = "steps.ghost.target.strategies[0].text"
    rejects(lookup_data, "unknown step 'ghost'")


def test_override_may_target_an_inserted_step(lookup_data: dict[str, Any]) -> None:
    lookup_data["overrides"]["tenant-b"].append(
        {"op": "set", "path": "steps.acknowledge_privacy_notice.description", "value": "x"}
    )
    build(lookup_data)


def test_detector_cannot_depend_on_detectors(lookup_data: dict[str, Any]) -> None:
    lookup_data["detectors"][0]["when"] = {"detector": "access_denied"}
    rejects(lookup_data, "cannot depend on other detectors")


def test_extra_keys_are_rejected(lookup_data: dict[str, Any]) -> None:
    lookup_data["steps"][1]["sleep_ms"] = 2000
    rejects(lookup_data, "sleep_ms")


def test_fields_need_sensitivity(lookup_data: dict[str, Any]) -> None:
    del lookup_data["inputs"]["properties"]["member_id"]["x-sensitivity"]
    rejects(lookup_data, "x-sensitivity")


def test_fail_detector_uses_failure_codes(lookup_data: dict[str, Any]) -> None:
    lookup_data["detectors"][0]["then"] = {"fail": "APP_ERROR"}
    cap = build(lookup_data)
    assert cap.detectors[0].then.fail == FailureCode.APP_ERROR  # type: ignore[union-attr]
    lookup_data["detectors"][0]["then"] = {"fail": "EXPLODED"}
    rejects(lookup_data, "then")


# --- contract helpers -------------------------------------------------------------------------


def test_input_validation(lookup_data: dict[str, Any]) -> None:
    cap = build(lookup_data)
    assert validate_inputs(cap, {"member_id": "10042"}) == []
    assert any("does not match" in e for e in validate_inputs(cap, {"member_id": "12ab"}))
    assert any("required" in e for e in validate_inputs(cap, {}))
    assert any(
        "Additional properties" in e for e in validate_inputs(cap, {"member_id": "10042", "x": 1})
    )


def test_output_validation(lookup_data: dict[str, Any]) -> None:
    cap = build(lookup_data)
    assert validate_outputs(cap, {"savings_balance": 1204.5, "member_name": "M"}) == []
    assert validate_outputs(cap, {"savings_balance": "$1,204.50"}) != []


def test_tool_definition_hides_extensions(lookup_data: dict[str, Any]) -> None:
    tool = to_tool_definition(build(lookup_data))
    assert tool["name"] == "lookup_savings_balance"
    assert "x-sensitivity" not in str(tool["input_schema"])
    assert "MEMBER_NOT_FOUND" in tool["description"] and ".." not in tool["description"]
