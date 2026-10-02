"""Discovery of a write flow with a human at the irreversible step, then compile and replay."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from cua.compiler.compile import CompileResult, compile_trace
from cua.config import tenant_context
from cua.core.artifact import RiskClass
from cua.core.confirmation import ConfirmationTokens
from cua.core.policy import PolicyGuard, load_policy
from cua.core.results import Success
from cua.discovery.run import run_discovery
from cua.discovery.spec import load_spec
from cua.discovery.trace import DiscoveryTrace
from cua.handoff.session import Handoff
from cua.handoff.store import Intervention, InterventionStore
from cua.llm.base import ToolCall
from cua.llm.scripted import ScriptedClient
from cua.replay.engine import ReplayEngine

from .conftest import ROOT
from .test_discovery_agent import call, find, route
from .test_handoff import FakeOperator, click_in_main
from .test_replay import NEW_ACCOUNT, OWNERS, SECRET_FN, TOKEN_SECRET, WRITER, accounts

GOAL = ROOT / "goals" / "open_sub_account.yaml"


def done_so_far(user: str) -> str:
    return user.split("ACTIONS SO FAR:")[1].split("LAST RESULT")[0]


def has(user: str, role: str, **want: str) -> bool:
    try:
        find(user, role, **want)
    except AssertionError:
        return False
    return True


def model(system: str, user: str, turn: int) -> ToolCall | None:
    """Scripted stand-in for the model: decides from what is on the page, like the real one."""
    log = done_so_far(user)
    needed = user.split("STILL NEEDED:")[1].split("\n")[0]
    if route(user) == "/search":
        if "{{inputs.member_id}}" not in log:
            box = find(user, "textbox", label="Member ID")
            return call("fill", ref=box, value="{{inputs.member_id}}")
        return call("click", ref=find(user, "button", name="Search", **{"in": "Find Member"}))
    if has(user, "cell", label="New Account Number"):
        if "new_account_number" in needed:
            cell = find(user, "cell", label="New Account Number")
            return call("extract", ref=cell, output="new_account_number")
        return call("done")
    if has(user, "button", name="Confirm"):
        return call("click", ref=find(user, "button", name="Confirm"))
    if has(user, "combobox", label="Account Type"):
        if "select" not in log:
            return call(
                "select", ref=find(user, "combobox", label="Account Type"), option="Savings"
            )
        if "{{inputs.nickname}}" not in log:
            box = find(user, "textbox", label="Nickname")
            return call("fill", ref=box, value="{{inputs.nickname}}")
        if "{{inputs.initial_deposit}}" not in log:
            box = find(user, "textbox", label="Initial Deposit")
            return call("fill", ref=box, value="{{inputs.initial_deposit}}")
        if "Funding Source" not in log:
            box = find(user, "combobox", label="Funding Source")
            return call("select", ref=box, option="{{inputs.funding_account}}")
        return call("click", ref=find(user, "button", name="Continue"))
    return call("click", ref=find(user, "link", name="Open Sub-Account"))


def compiled(trace: DiscoveryTrace) -> CompileResult:
    ctx, goal = tenant_context("tenant-a"), load_spec(GOAL)
    return compile_trace(
        trace,
        content_frame=ctx.profile.content_frame,
        login=ctx.profile.login,
        example_inputs=goal.example_inputs,
        declared_outcomes=goal.outcomes,
        subject=goal.subject,
    )


@pytest.fixture
def store(tmp_path: Path) -> InterventionStore:
    return InterventionStore(tmp_path / "interventions")


def guard(url: str, tokens: ConfirmationTokens | None) -> PolicyGuard:
    policy = load_policy(ROOT / "config" / "policies" / "cu-core.yaml")
    return PolicyGuard(policy.model_copy(update={"allowed_origins": (url,)}), tokens)


def discover(
    servers: Any, browser: Any, tmp_path: Path, handoff: Handoff | None
) -> tuple[DiscoveryTrace, Path]:
    for s in servers.values():
        s.reset()
    return run_discovery(
        load_spec(GOAL),
        browser=browser,
        llm=ScriptedClient(model),
        ctx=tenant_context("tenant-a"),
        base_url=servers["a"].url,
        secrets=SECRET_FN,
        evidence_root=tmp_path / "runs",
        guard=guard(servers["a"].url, ConfirmationTokens(TOKEN_SECRET)),
        handoff=handoff,
    )


def test_without_an_operator_discovery_stops_before_the_irreversible_click(
    servers: Any, browser: Any, tmp_path: Path
) -> None:
    trace, _ = discover(servers, browser, tmp_path, None)
    assert trace.status == "escalated" and "confirmation" in trace.reason
    assert (
        trace.steps[-1].policy and trace.steps[-1].policy.effective_risk == RiskClass.IRREVERSIBLE
    )
    assert len(accounts(servers, "10042")) == 3


def test_operator_approves_and_the_flow_compiles_and_replays(
    servers: Any, browser: Any, tmp_path: Path, store: InterventionStore
) -> None:
    op = FakeOperator(store, lambda _s, _i, _n: "approve")
    trace, _ = discover(servers, browser, tmp_path, Handoff(store, pump_hook=op))
    assert trace.status == "succeeded", trace.reason
    assert len(accounts(servers, "10042")) == 4
    confirm = next(s for s in trace.steps if s.element and s.element.name == "Confirm")
    assert (confirm.actor, confirm.approved_by) == ("agent", "ops.alex")
    assert confirm.intervention == store.list()[0].id and store.list()[0].mode == "discovery"

    result = compiled(trace)
    cap = result.capability
    assert cap.risk == RiskClass.IRREVERSIBLE
    by_action = [(s.action.value, s.risk.value, s.requires_confirmation) for s in cap.steps]
    assert ("click", "irreversible", True) in by_action
    options = [s.option for s in cap.steps if s.action.value == "select"]
    assert options == ["{{inputs.account_type}}", "{{inputs.funding_account}}"]
    assert "approved by ops.alex" in result.review_markdown

    # The compiled draft replays with no model; the commit again waits for a person.
    servers["a"].reset()
    op2 = FakeOperator(store, lambda _s, _i, _n: "approve")
    engine = ReplayEngine(
        browser=browser,
        ctx=tenant_context("tenant-a"),
        secrets=SECRET_FN,
        evidence_root=tmp_path / "replays",
        guard=guard(servers["a"].url, ConfirmationTokens(TOKEN_SECRET)),
        base_url=servers["a"].url,
        max_wait_ms=1500,
        settle_ms=1000,
        handoff=Handoff(store, pump_hook=op2),
    )
    r = engine.run(cap, NEW_ACCOUNT, caller=WRITER, ownership=OWNERS, attended=True)
    assert isinstance(r, Success), r
    assert r.outputs["new_account_number"].startswith("0042-")
    assert len(accounts(servers, "10042")) == 4


def test_operator_performs_the_step_and_it_is_recorded_as_human(
    servers: Any, browser: Any, tmp_path: Path, store: InterventionStore
) -> None:
    def script(surface: Any, _item: Intervention, _n: int) -> str:
        click_in_main(surface, "Confirm")
        return "step_done"

    trace, _ = discover(
        servers, browser, tmp_path, Handoff(store, pump_hook=FakeOperator(store, script))
    )
    assert trace.status == "succeeded", trace.reason
    assert len(accounts(servers, "10042")) == 4  # the agent did not click Confirm as well
    confirm = next(s for s in trace.steps if s.element and s.element.name == "Confirm")
    assert confirm.actor == "human:ops.alex" and confirm.approved_by is None
    assert [a["name"] for a in confirm.human_actions] == ["Confirm"]
    result = compiled(trace)
    assert "performed by human:ops.alex" in result.review_markdown
    assert any(s.requires_confirmation for s in result.capability.steps)


def test_operator_abort_ends_discovery_without_committing(
    servers: Any, browser: Any, tmp_path: Path, store: InterventionStore
) -> None:
    op = FakeOperator(store, lambda _s, _i, _n: "abort")
    trace, _ = discover(servers, browser, tmp_path, Handoff(store, pump_hook=op))
    assert trace.status == "escalated" and "aborted by the operator" in trace.reason
    assert len(accounts(servers, "10042")) == 3
