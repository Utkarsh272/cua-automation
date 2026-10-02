"""Tenant overrides and the agent tool catalog (no browser)."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from cua.core.artifact import load_capability
from cua.core.overrides import (
    OverrideError,
    apply_ops,
    capability_dict,
    effective_capability,
    set_path,
)
from cua.registry.catalog import tool_catalog
from cua.registry.store import Registry

ROOT = Path(__file__).resolve().parents[2]
REFERENCE = ROOT / "tests/fixtures/reference/lookup_savings_balance.hand_written.yaml"
OPEN_ACCOUNT = ROOT / "tests/fixtures/reference/open_sub_account.hand_written.yaml"


def test_other_tenants_get_the_capability_unchanged() -> None:
    cap = load_capability(REFERENCE)
    same, applied = effective_capability(cap, "tenant-a")
    assert same is cap and applied == 0


def test_tenant_b_gets_relabels_and_the_inserted_step() -> None:
    cap = load_capability(REFERENCE)
    eff, applied = effective_capability(cap, "tenant-b")
    assert applied == len(cap.overrides["tenant-b"])
    ids = [s.id for s in eff.steps]
    at = ids.index("enter_member_id")
    assert ids[at + 1] == "acknowledge_privacy_notice" and len(ids) == len(cap.steps) + 1
    first = eff.step("enter_member_id").target
    assert first and first.strategies[0].model_dump()["text"] == "Member #"
    assert eff.overrides == {} and (eff.id, eff.version) == (cap.id, cap.version)
    # the original is untouched (frozen models; overrides work on a copy)
    base = cap.step("enter_member_id").target
    assert base and base.strategies[0].model_dump()["text"] == "Member ID"


def test_bad_paths_are_rejected_not_silently_ignored() -> None:
    data = capability_dict(load_capability(REFERENCE))
    for path in (
        "steps.ghost.target.strategies[0].text",
        "steps.enter_member_id.target.strategies[9].text",
        "steps.enter_member_id",
        "success.all_of",
    ):
        with pytest.raises(OverrideError):
            set_path(data, path, "x")
    with pytest.raises(OverrideError, match="no step 'ghost'"):
        apply_ops(data, [{"op": "insert_step", "after": "ghost", "step": {}}])


def test_an_override_cannot_produce_an_invalid_capability() -> None:
    data = capability_dict(load_capability(REFERENCE))
    data.pop("content_hash", None)
    data["overrides"]["tenant-b"] = [
        {"op": "set", "path": "steps.enter_member_id.action", "value": "teleport"}
    ]
    from cua.core.artifact import parse_capability

    cap = parse_capability(data, verify_hash=False)
    with pytest.raises(OverrideError, match="invalid capability"):
        effective_capability(cap, "tenant-b")


# --- tool catalog ---------------------------------------------------------------------------------


def catalog(reg: Registry, **kw: Any) -> list[dict[str, Any]]:
    kw.setdefault("tenant", "tenant-a")
    return tool_catalog(reg, product="cu-core", product_version="4.6.2", **kw)


def registry(tmp_path: Path) -> Registry:
    reg = Registry(tmp_path)
    for src in (REFERENCE, OPEN_ACCOUNT):
        cap = load_capability(src)
        reg.save_draft(cap, src.read_text())
    (tmp_path / "login").mkdir()
    (tmp_path / "login" / "1.0.0.yaml").write_text(
        (ROOT / "capabilities/login/1.0.0.yaml").read_text()
    )
    return reg


def test_catalog_offers_only_approved_capabilities_within_scope(tmp_path: Path) -> None:
    reg = registry(tmp_path)
    assert catalog(reg) == []  # drafts are never tools; login is plumbing, not a tool
    reg.approve("lookup_savings_balance", "1.0.0", approver="reviewer.one")
    reg.approve("open_sub_account", "1.0.0", approver="reviewer.one")

    (tool,) = catalog(reg)  # no write scope: the write capability is not offered
    assert tool["name"] == "lookup_savings_balance"
    assert tool["input_schema"]["required"] == ["member_id"]
    assert "x-sensitivity" not in str(tool["input_schema"])
    meta = tool["x-capability"]
    assert meta["risk"] == "read" and meta["subject_input"] == "member_id"
    assert meta["content_hash"].startswith("sha256:") and not meta["tenant_overrides"]
    assert catalog(reg, tenant="tenant-b")[0]["x-capability"]["tenant_overrides"]

    both = catalog(reg, scopes=frozenset({"invoke:*", "write"}))
    assert [t["name"] for t in both] == ["lookup_savings_balance", "open_sub_account"]
    assert both[1]["x-capability"]["needs_human_confirmation"] is True

    only = catalog(reg, scopes=frozenset({"invoke:open_sub_account", "write"}))
    assert [t["name"] for t in only] == ["open_sub_account"]
    assert tool_catalog(reg, tenant="t", product="cu-core", product_version="5.1.0") == []


def test_overrides_apply_to_an_approved_capability(tmp_path: Path) -> None:
    # Found by a real run: the derived view of an approved artifact was rejected for lacking
    # its content hash, so tenant overrides only worked on drafts.
    reg = registry(tmp_path)
    approved = reg.approve("lookup_savings_balance", "1.0.0", approver="reviewer.one")
    eff, applied = effective_capability(approved, "tenant-b")
    assert applied and eff.status == "approved" and eff.content_hash == approved.content_hash
    assert "acknowledge_privacy_notice" in [s.id for s in eff.steps]
