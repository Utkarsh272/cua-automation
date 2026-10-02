"""The agent-facing tool catalog for one tenant.

An agent (chat or voice) never sees steps, locators or the registry. It sees tools: one per
capability that is approved, compatible with the tenant's product version, and within the
scopes of the caller. Calling a tool means ``ReplayEngine.run`` with the typed inputs.
"""

from __future__ import annotations

from typing import Any

from cua.core.artifact import Capability, RiskClass, to_tool_definition
from cua.core.semver import parse_version, satisfies

from .store import Registry


def latest_approved(
    reg: Registry, cap_id: str, product: str, product_version: str
) -> Capability | None:
    for version in sorted(reg.versions(cap_id), key=parse_version, reverse=True):
        try:
            cap = reg.load(cap_id, version)
        except Exception:  # an invalid or tampered file is never offered as a tool
            continue
        if (
            cap.status == "approved"
            and cap.app.product == product
            and satisfies(product_version, cap.app.versions)
        ):
            return cap
    return None


def tool_catalog(
    reg: Registry,
    *,
    tenant: str,
    product: str,
    product_version: str,
    scopes: frozenset[str] = frozenset({"invoke:*"}),
    exclude: frozenset[str] = frozenset({"login"}),
) -> list[dict[str, Any]]:
    tools = []
    for cap_id in reg.ids():
        if cap_id in exclude:
            continue
        if "invoke:*" not in scopes and f"invoke:{cap_id}" not in scopes:
            continue
        cap = latest_approved(reg, cap_id, product, product_version)
        if cap is None or (cap.risk != RiskClass.READ and "write" not in scopes):
            continue
        tool = to_tool_definition(cap)
        tool["x-capability"] = {
            "ref": cap.ref,
            "content_hash": cap.content_hash,
            "risk": cap.risk.value,
            "needs_human_confirmation": any(s.requires_confirmation for s in cap.steps),
            "subject_input": cap.subject.split(".", 1)[1] if cap.subject else None,
            "tenant_overrides": tenant in cap.overrides,
            "outcomes": [o.code for o in cap.outcomes],
        }
        tools.append(tool)
    return tools
