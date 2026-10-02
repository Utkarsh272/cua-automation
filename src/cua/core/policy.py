"""The policy guard: every action, in discovery and in replay, is checked here first.

* **Deny by default.** Origins, routes and action types must be allowlisted; a click that
  navigates somewhere unlisted is caught by :meth:`PolicyGuard.check_url` on the resulting URL.
* **Risk can only go up.** The effective risk of an action is the maximum of what the artifact
  declares and what the policy's heuristics infer (a control named "Confirm" is irreversible
  whatever the artifact says). A mislabeled artifact cannot make money movement look safe.
* **Irreversible actions need a confirmation token**, verified and spent here. With no token
  the verdict is ``escalate`` (a human decides); with a bad token it is ``deny`` (a security
  event, not a pause).

The model and the prompt never enforce anything. Page text that says "ignore previous
instructions" can at most make the agent *propose* an action; the guard decides.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from typing import Literal
from urllib.parse import urlsplit

from pydantic import BaseModel, ConfigDict, Field, model_validator
from ruamel.yaml import YAML

from .artifact import ActionKind, RiskClass, max_risk
from .confirmation import Confirmation, ConfirmationTokens, TokenError
from .text import match_route, normalize, route_of

WRITE_SCOPE = "write"


class Handling(StrEnum):
    ALLOW = "allow"
    ALLOW_WITH_WRITE_SCOPE = "allow_with_write_scope"
    REQUIRE_CONFIRMATION = "require_confirmation"


class RiskWhen(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    control_name_matches: str | None = None
    route_matches: str | None = None

    @model_validator(mode="after")
    def _at_least_one(self) -> RiskWhen:
        if self.control_name_matches is None and self.route_matches is None:
            raise ValueError("a risk rule needs control_name_matches and/or route_matches")
        for rx in (self.control_name_matches, self.route_matches):
            if rx is not None:
                re.compile(rx)
        return self


class RiskRule(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    when: RiskWhen
    risk: RiskClass


class PolicyConfig(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    app: str
    allowed_origins: tuple[str, ...] = Field(min_length=1)
    allowed_routes: tuple[str, ...] = Field(min_length=1)
    allowed_actions: frozenset[ActionKind]
    blocked_actions: tuple[str, ...] = ()
    risk_rules: tuple[RiskRule, ...] = ()
    risk_handling: dict[RiskClass, Handling] = Field(
        default_factory=lambda: {
            RiskClass.READ: Handling.ALLOW,
            RiskClass.REVERSIBLE_WRITE: Handling.ALLOW_WITH_WRITE_SCOPE,
            RiskClass.IRREVERSIBLE: Handling.REQUIRE_CONFIRMATION,
        }
    )

    @model_validator(mode="after")
    def _irreversible_never_plain_allow(self) -> PolicyConfig:
        if self.risk_handling.get(RiskClass.IRREVERSIBLE) != Handling.REQUIRE_CONFIRMATION:
            raise ValueError("irreversible actions must use require_confirmation")
        return self


def load_policy(path: str | Path) -> PolicyConfig:
    with open(path, encoding="utf-8") as fh:
        return PolicyConfig.model_validate(YAML(typ="safe").load(fh))


@dataclass(frozen=True)
class ProposedAction:
    kind: ActionKind | str
    url: str
    """For navigate: the destination. Otherwise: the page the action happens on."""
    declared_risk: RiskClass = RiskClass.READ
    control_name: str | None = None
    step_id: str | None = None


Verdict = Literal["allow", "deny", "escalate"]


@dataclass(frozen=True)
class PolicyDecision:
    verdict: Verdict
    effective_risk: RiskClass
    reason: str
    confirmed_by: str | None = None

    @property
    def allowed(self) -> bool:
        return self.verdict == "allow"


class PolicyGuard:
    def __init__(self, config: PolicyConfig, tokens: ConfirmationTokens | None = None) -> None:
        self.config = config
        self._tokens = tokens
        self._rules = [
            (
                re.compile(r.when.control_name_matches, re.I)
                if r.when.control_name_matches
                else None,
                re.compile(r.when.route_matches, re.I) if r.when.route_matches else None,
                r.risk,
            )
            for r in config.risk_rules
        ]

    @property
    def tokens(self) -> ConfirmationTokens | None:
        return self._tokens

    # --- location ---------------------------------------------------------------------------

    def check_url(self, url: str) -> PolicyDecision:
        parts = urlsplit(url)
        origin = f"{parts.scheme}://{parts.netloc}" if parts.netloc else None
        if origin is not None and origin not in self.config.allowed_origins:
            return PolicyDecision("deny", RiskClass.READ, f"origin {origin} is not allowlisted")
        route = route_of(url)
        if not any(match_route(p, route) is not None for p in self.config.allowed_routes):
            return PolicyDecision("deny", RiskClass.READ, f"route {route} is not allowlisted")
        return PolicyDecision("allow", RiskClass.READ, "location allowed")

    # --- risk -------------------------------------------------------------------------------

    def effective_risk(self, action: ProposedAction) -> RiskClass:
        risks = [action.declared_risk]
        name = normalize(action.control_name or "")
        route = route_of(action.url)
        for name_rx, route_rx, risk in self._rules:
            name_ok = name_rx is None or (bool(name) and bool(name_rx.search(name)))
            route_ok = route_rx is None or bool(route_rx.search(route))
            if name_ok and route_ok:
                risks.append(risk)
        return max_risk(*risks)

    # --- the decision -----------------------------------------------------------------------

    def evaluate(
        self,
        action: ProposedAction,
        *,
        scopes: frozenset[str],
        confirmation: Confirmation | None = None,
    ) -> PolicyDecision:
        kind = action.kind.value if isinstance(action.kind, ActionKind) else action.kind
        risk = self.effective_risk(action)

        if kind in self.config.blocked_actions:
            return PolicyDecision("deny", risk, f"action {kind!r} is blocked")
        if kind not in {a.value for a in self.config.allowed_actions}:
            return PolicyDecision("deny", risk, f"action {kind!r} is not allowlisted")
        where = self.check_url(action.url)
        if not where.allowed:
            return PolicyDecision("deny", risk, where.reason)

        handling = self.config.risk_handling.get(risk, Handling.REQUIRE_CONFIRMATION)
        if handling == Handling.ALLOW:
            return PolicyDecision("allow", risk, "read action")
        if WRITE_SCOPE not in scopes:
            if risk == RiskClass.IRREVERSIBLE:
                return PolicyDecision(
                    "escalate", risk, "irreversible action and caller has no write scope"
                )
            return PolicyDecision("deny", risk, "write action requires the 'write' scope")
        if handling == Handling.ALLOW_WITH_WRITE_SCOPE:
            return PolicyDecision("allow", risk, "reversible write with write scope")

        # require_confirmation
        if confirmation is None or self._tokens is None:
            return PolicyDecision("escalate", risk, "irreversible action needs confirmation")
        if confirmation.step_id != action.step_id:
            return PolicyDecision("deny", risk, "confirmation is for a different step")
        try:
            who = self._tokens.verify(confirmation)
        except TokenError as exc:
            return PolicyDecision("deny", risk, f"confirmation rejected: {exc.reason}")
        return PolicyDecision("allow", risk, "irreversible action confirmed", confirmed_by=who)
