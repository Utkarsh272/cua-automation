from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from cua.core.artifact import ActionKind, RiskClass, parse_capability
from cua.core.authz import (
    Caller,
    Operator,
    StaticOwnership,
    authorize_invocation,
    authorize_operator,
)
from cua.core.confirmation import Confirmation, ConfirmationTokens, TokenError
from cua.core.policy import PolicyConfig, PolicyGuard, ProposedAction, load_policy

ROOT = Path(__file__).resolve().parents[2]
BASE = "http://localhost:8080"
SECRET = b"test-secret-at-least-16-bytes"


@pytest.fixture
def policy() -> PolicyConfig:
    return load_policy(ROOT / "config" / "policies" / "cu-core.yaml")


class Clock:
    def __init__(self) -> None:
        self.now = 1_000_000.0

    def __call__(self) -> float:
        return self.now


# --- confirmation tokens ----------------------------------------------------------------------

INPUTS = {"member_id": "10042", "deposit": "250.00"}


def test_token_happy_path_and_single_use() -> None:
    tokens = ConfirmationTokens(SECRET)
    tok = tokens.issue(run_id="r1", step_id="confirm", inputs=INPUTS, issued_to="op:alice")
    c = Confirmation(tok, "r1", "confirm", INPUTS)
    assert tokens.verify(c) == "op:alice"
    with pytest.raises(TokenError, match="replayed"):
        tokens.verify(c)


@pytest.mark.parametrize(
    ("run", "step", "inputs"),
    [
        ("r2", "confirm", INPUTS),
        ("r1", "other", INPUTS),
        ("r1", "confirm", {**INPUTS, "member_id": "10077"}),
        ("r1", "confirm", {**INPUTS, "deposit": "9000.00"}),
    ],
)
def test_token_is_bound_to_run_step_and_inputs(run: str, step: str, inputs: dict[str, Any]) -> None:
    tokens = ConfirmationTokens(SECRET)
    tok = tokens.issue(run_id="r1", step_id="confirm", inputs=INPUTS, issued_to="op:alice")
    with pytest.raises(TokenError, match="wrong_binding"):
        tokens.verify(Confirmation(tok, run, step, inputs))


def test_token_expiry_forgery_and_garbage() -> None:
    clock = Clock()
    tokens = ConfirmationTokens(SECRET, ttl_s=60, clock=clock)
    tok = tokens.issue(run_id="r1", step_id="s", inputs={}, issued_to="x")
    clock.now += 61
    with pytest.raises(TokenError, match="expired"):
        tokens.verify(Confirmation(tok, "r1", "s", {}))
    other = ConfirmationTokens(b"a-different-secret-entirely")
    forged = other.issue(run_id="r1", step_id="s", inputs={}, issued_to="x")
    with pytest.raises(TokenError, match="bad_signature"):
        tokens.verify(Confirmation(forged, "r1", "s", {}))
    with pytest.raises(TokenError, match="malformed"):
        tokens.verify(Confirmation("not-a-token", "r1", "s", {}))


def test_short_secret_rejected() -> None:
    with pytest.raises(ValueError):
        ConfirmationTokens(b"short")


# --- policy guard -----------------------------------------------------------------------------


def act(kind: str, path: str, **kw: Any) -> ProposedAction:
    return ProposedAction(kind=ActionKind(kind), url=BASE + path, **kw)


def test_read_actions_allowed_on_allowlisted_routes(policy: PolicyConfig) -> None:
    guard = PolicyGuard(policy)
    d = guard.evaluate(act("fill", "/search", control_name="Member ID"), scopes=frozenset())
    assert d.allowed and d.effective_risk == RiskClass.READ


@pytest.mark.parametrize(
    "url",
    [
        BASE + "/admin/transfer",
        BASE + "/__test/reset",
        BASE + "/reports",
        "http://evil.example.com/search",
        "http://localhost:9999/search",
    ],
)
def test_off_allowlist_is_denied(policy: PolicyConfig, url: str) -> None:
    d = PolicyGuard(policy).evaluate(
        ProposedAction(ActionKind.NAVIGATE, url), scopes=frozenset({"write"})
    )
    assert d.verdict == "deny"


def test_blocked_and_unknown_actions(policy: PolicyConfig) -> None:
    guard = PolicyGuard(policy)
    assert (
        guard.evaluate(ProposedAction("download", BASE + "/search"), scopes=frozenset()).verdict
        == "deny"
    )
    assert (
        guard.evaluate(ProposedAction("drag", BASE + "/search"), scopes=frozenset()).verdict
        == "deny"
    )


def test_heuristics_raise_risk_even_if_artifact_says_read(policy: PolicyConfig) -> None:
    guard = PolicyGuard(policy)
    click = act("click", "/member/10042/accounts/review", control_name="Confirm", step_id="confirm")
    assert guard.effective_risk(click) == RiskClass.IRREVERSIBLE
    d = guard.evaluate(click, scopes=frozenset({"write"}))
    assert d.verdict == "escalate"


def test_heuristics_never_lower_risk(policy: PolicyConfig) -> None:
    guard = PolicyGuard(policy)
    a = act("click", "/search", control_name="Search", declared_risk=RiskClass.IRREVERSIBLE)
    assert guard.effective_risk(a) == RiskClass.IRREVERSIBLE


def test_word_boundaries_in_risk_rules(policy: PolicyConfig) -> None:
    guard = PolicyGuard(policy)
    # "Unconfirmed items" must not look like a Confirm button.
    a = act("click", "/search", control_name="Unconfirmed items")
    assert guard.effective_risk(a) == RiskClass.READ


def test_reversible_write_needs_write_scope(policy: PolicyConfig) -> None:
    guard = PolicyGuard(policy)
    a = act("click", "/member/10042/accounts/new", control_name="Continue")
    assert guard.evaluate(a, scopes=frozenset()).verdict == "deny"
    assert guard.evaluate(a, scopes=frozenset({"write"})).verdict == "allow"


def test_irreversible_with_valid_token_is_allowed_once(policy: PolicyConfig) -> None:
    tokens = ConfirmationTokens(SECRET)
    guard = PolicyGuard(policy, tokens)
    click = act("click", "/member/10042/accounts/review", control_name="Confirm", step_id="confirm")
    tok = tokens.issue(run_id="r1", step_id="confirm", inputs=INPUTS, issued_to="op:alice")
    c = Confirmation(tok, "r1", "confirm", INPUTS)
    d = guard.evaluate(click, scopes=frozenset({"write"}), confirmation=c)
    assert d.allowed and d.confirmed_by == "op:alice"
    again = guard.evaluate(click, scopes=frozenset({"write"}), confirmation=c)
    assert again.verdict == "deny" and "replayed" in again.reason


def test_irreversible_without_write_scope_escalates(policy: PolicyConfig) -> None:
    click = act("click", "/member/10042/accounts/review", control_name="Confirm")
    assert PolicyGuard(policy).evaluate(click, scopes=frozenset()).verdict == "escalate"


def test_confirmation_for_other_step_is_denied(policy: PolicyConfig) -> None:
    tokens = ConfirmationTokens(SECRET)
    guard = PolicyGuard(policy, tokens)
    tok = tokens.issue(run_id="r1", step_id="other", inputs=INPUTS, issued_to="x")
    click = act("click", "/member/10042/accounts/review", control_name="Confirm", step_id="confirm")
    d = guard.evaluate(
        click, scopes=frozenset({"write"}), confirmation=Confirmation(tok, "r1", "other", INPUTS)
    )
    assert d.verdict == "deny"


def test_policy_refuses_plain_allow_for_irreversible() -> None:
    with pytest.raises(ValueError):
        PolicyConfig(
            app="x",
            allowed_origins=(BASE,),
            allowed_routes=("/",),
            allowed_actions=frozenset({ActionKind.CLICK}),
            risk_handling={RiskClass.IRREVERSIBLE: "allow"},  # type: ignore[dict-item]
        )


# --- authorization ----------------------------------------------------------------------------

OWNERS = StaticOwnership({"member:maria": frozenset({"10042"})})


@pytest.fixture
def cap(lookup_data: dict[str, Any]) -> Any:
    lookup_data["status"] = "draft"
    return parse_capability(lookup_data)


def caller(**kw: Any) -> Caller:
    defaults: dict[str, Any] = {
        "principal": "agent:chat",
        "scopes": frozenset({"invoke:lookup_savings_balance"}),
        "tenants": frozenset({"tenant-a"}),
        "on_behalf_of": "member:maria",
    }
    return Caller(**{**defaults, **kw})


def authz(
    cap: Any, c: Caller, member: str = "10042", tenant: str = "tenant-a", attended: bool = True
) -> Any:
    return authorize_invocation(
        cap,
        tenant=tenant,
        inputs={"member_id": member},
        caller=c,
        ownership=OWNERS,
        attended=attended,
    )


def test_subject_binding_allows_own_data(cap: Any) -> None:
    assert authz(cap, caller()).allowed


def test_subject_binding_blocks_confused_deputy(cap: Any) -> None:
    d = authz(cap, caller(), member="10077")
    assert not d.allowed and "not the subject" in d.reason


def test_subject_binding_requires_on_behalf_of(cap: Any) -> None:
    assert not authz(cap, caller(on_behalf_of=None)).allowed


def test_staff_scope_is_audited(cap: Any) -> None:
    c = caller(on_behalf_of=None, scopes=frozenset({"invoke:*", "staff"}))
    d = authz(cap, c, member="10077")
    assert d.allowed and any("staff access" in a for a in d.audit)


def test_scope_and_tenant_checks(cap: Any) -> None:
    assert not authz(cap, caller(scopes=frozenset({"invoke:other"}))).allowed
    assert not authz(cap, caller(), tenant="tenant-b").allowed


def test_drafts_only_run_attended(cap: Any) -> None:
    d = authz(cap, caller(), attended=False)
    assert not d.allowed and "draft" in d.reason


def test_operator_authorization() -> None:
    op = Operator("op:alice", frozenset({"tenant-a"}))
    assert authorize_operator(op, tenant="tenant-a", action="claim").allowed
    assert not authorize_operator(op, tenant="tenant-b", action="claim").allowed
    viewer = Operator("op:bob", frozenset({"*"}), roles=frozenset({"viewer"}))
    assert not authorize_operator(viewer, tenant="tenant-a", action="resume").allowed
