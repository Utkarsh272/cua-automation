"""Authorization of *who may invoke what, for whom*, checked before a browser is opened.

The main risk is a confused deputy: the automation signs in to the bank's app as a powerful
staff service account, but acts for an agent that serves one member. Being signed in is not
the same as being allowed. Four checks, in order:

1. **Tenant**: the caller is allowed on this tenant.
2. **Scope**: ``invoke:<capability>`` (or ``invoke:*``); non-read capabilities also need ``write``.
3. **Artifact state**: only ``approved`` artifacts run unattended; drafts only in attended mode.
4. **Subject binding**: if the capability declares ``subject: inputs.member_id``, the caller must
   act ``on_behalf_of`` a principal who owns that member, unless it holds the audited ``staff``
   scope (for staff-assist tools).

Operators are authorized separately, per tenant, for claim/resume/abort.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any, Literal, Protocol

from .artifact import Capability, RiskClass
from .policy import WRITE_SCOPE

STAFF_SCOPE = "staff"


class SubjectOwnership(Protocol):
    def owns(self, principal: str, subject: str) -> bool: ...


@dataclass(frozen=True)
class StaticOwnership:
    """Demo ownership table: principal -> subjects. Production would ask the bank's IdP/core."""

    table: Mapping[str, frozenset[str]]

    def owns(self, principal: str, subject: str) -> bool:
        return subject in self.table.get(principal, frozenset())


@dataclass(frozen=True)
class Caller:
    principal: str
    scopes: frozenset[str]
    tenants: frozenset[str]
    on_behalf_of: str | None = None


@dataclass(frozen=True)
class AuthzDecision:
    allowed: bool
    reason: str
    audit: tuple[str, ...] = field(default=())


def _deny(reason: str) -> AuthzDecision:
    return AuthzDecision(False, reason)


def authorize_invocation(
    cap: Capability,
    *,
    tenant: str,
    inputs: Mapping[str, Any],
    caller: Caller,
    ownership: SubjectOwnership,
    attended: bool = False,
) -> AuthzDecision:
    audit: list[str] = []

    if tenant not in caller.tenants and "*" not in caller.tenants:
        return _deny(f"{caller.principal} is not allowed on tenant {tenant}")

    if f"invoke:{cap.id}" not in caller.scopes and "invoke:*" not in caller.scopes:
        return _deny(f"{caller.principal} lacks scope invoke:{cap.id}")
    if cap.risk != RiskClass.READ and WRITE_SCOPE not in caller.scopes:
        return _deny(f"{cap.ref} is {cap.risk.value}; the 'write' scope is required")

    if cap.status == "draft" and not attended:
        return _deny(f"{cap.ref} is a draft; drafts run only in attended mode")
    if cap.status == "deprecated":
        audit.append(f"{cap.ref} is deprecated")

    if cap.subject is not None:
        field_name = cap.subject.split(".", 1)[1]
        subject = str(inputs.get(field_name, ""))
        if STAFF_SCOPE in caller.scopes:
            audit.append(f"staff access by {caller.principal} to subject {field_name}")
        elif caller.on_behalf_of is None:
            return _deny(f"{cap.ref} touches member data; on_behalf_of is required")
        elif not ownership.owns(caller.on_behalf_of, subject):
            return _deny(f"{caller.on_behalf_of} is not the subject of this request")

    return AuthzDecision(True, "allowed", tuple(audit))


OperatorAction = Literal["claim", "resume", "abort"]


@dataclass(frozen=True)
class Operator:
    principal: str
    tenants: frozenset[str]
    roles: frozenset[str] = frozenset({"operator"})


def authorize_operator(op: Operator, *, tenant: str, action: OperatorAction) -> AuthzDecision:
    if "operator" not in op.roles:
        return _deny(f"{op.principal} is not an operator")
    if tenant not in op.tenants and "*" not in op.tenants:
        return _deny(f"{op.principal} may not {action} sessions on tenant {tenant}")
    return AuthzDecision(True, "allowed", (f"operator {op.principal} {action} on {tenant}",))
