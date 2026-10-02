"""Compiled artifacts must run on the live app: discovery -> compile -> execute, end to end.

Day 6 adds the full replay engine; these tests already prove the compiled steps, waits and
detectors work against real CU Core pages (using the shared StepRunner).
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from cua.compiler.compile import compile_trace
from cua.config import tenant_context
from cua.core.artifact import Capability, OutcomeThen, RecoverThen
from cua.core.conditions import evaluate
from cua.core.parse import parse_value
from cua.core.templating import env_secrets
from cua.discovery.spec import load_spec
from cua.discovery.trace import DiscoveryTrace
from cua.llm.base import ToolCall
from cua.runtime.steps import StepRunner, run_login
from cua.surface.web import WebSurface

from .conftest import ROOT, SECRETS, SurfaceFactory
from .test_discovery_agent import call, discover, find, lookup_policy, spec

REAL = ROOT / "tests" / "fixtures" / "traces" / "groq_lookup_success.json"
GOAL = load_spec(ROOT / "goals" / "lookup_savings_balance.yaml")
SECRET_FN = env_secrets(
    {
        "CU_CORE_USERNAME": SECRETS["cu_core.username"],
        "CU_CORE_PASSWORD": SECRETS["cu_core.password"],
    }
)


def compile_(trace: DiscoveryTrace, *outcomes: DiscoveryTrace) -> Capability:
    ctx = tenant_context("tenant-a")
    return compile_trace(
        trace,
        content_frame=ctx.profile.content_frame,
        login=ctx.profile.login,
        outcome_traces=outcomes,
        example_inputs=GOAL.example_inputs,
        declared_outcomes=GOAL.outcomes,
        subject=GOAL.subject,
    ).capability


def execute(s: WebSurface, cap: Capability, inputs: dict[str, str]) -> tuple[dict[str, Any], Any]:
    """Run every step with the capability's detectors; stop at the first business outcome."""
    run_login(s, tenant_context("tenant-a").login, SECRET_FN)
    runner = StepRunner(s, inputs, SECRET_FN, cap.detectors)
    outputs: dict[str, Any] = {}
    state = None
    for step in cap.steps:
        out = runner.run(step, outputs)
        state = out.state
        if out.raw is not None and step.output:
            outputs[step.output] = parse_value(step.parse, out.raw)
        fired = [
            d
            for d in cap.detectors
            if d.id in state.fired_detectors and isinstance(d.then, OutcomeThen)
        ]
        if fired:
            return {"outcome": fired[0].then.outcome}, state  # type: ignore[union-attr]
    return outputs, state


@pytest.mark.parametrize(
    ("member", "expected"),
    [
        ("10077", {"member_name": "James Okafor", "savings_balance": 12940.33}),
        ("10123", {"member_name": "Chen Wei", "savings_balance": 25.0}),
    ],
)
def test_artifact_compiled_from_the_real_groq_run_executes_on_live_app(
    open_surface: SurfaceFactory, member: str, expected: dict[str, Any]
) -> None:
    cap = compile_(DiscoveryTrace.model_validate(json.loads(REAL.read_text())))
    s = open_surface()
    outputs, state = execute(s, cap, {"member_id": member})
    assert outputs == expected  # a member the model never saw
    assert evaluate(cap.success, state.__class__(**{**state.__dict__, "outputs": outputs}))


def test_outcome_discovered_once_is_detected_on_replay(
    servers: Any, browser: Any, tmp_path: Path, open_surface: SurfaceFactory
) -> None:
    def not_found(system: str, user: str, turn: int) -> ToolCall | None:
        if turn == 0:
            return call(
                "fill", ref=find(user, "textbox", label="Member ID"), value="{{inputs.member_id}}"
            )
        if turn == 1:
            return call("click", ref=find(user, "button", name="Search", **{"in": "Find Member"}))
        return call(
            "declare_outcome",
            code="MEMBER_NOT_FOUND",
            evidence="No member found for the ID entered.",
        )

    outcome_run, _, _ = discover(
        servers, browser, tmp_path, not_found, spec(example_inputs={"member_id": "99999"})
    )
    cap = compile_(DiscoveryTrace.model_validate(json.loads(REAL.read_text())), outcome_run)
    assert [d.id for d in cap.detectors] == ["member_not_found"]

    outputs, _ = execute(open_surface(), cap, {"member_id": "55555"})  # a different unknown ID
    assert outputs == {"outcome": "MEMBER_NOT_FOUND"}


def test_interstitial_seen_in_discovery_becomes_a_recovery(
    servers: Any, browser: Any, tmp_path: Path, open_surface: SurfaceFactory
) -> None:
    def with_notice(system: str, user: str, turn: int) -> ToolCall | None:
        if "dialog='System Notice'" in user:
            return call("click", ref=find(user, "button", name="Acknowledge"))
        return lookup_policy(system, user, turn)

    from cua.config import tenant_context as tc
    from cua.discovery.run import run_discovery
    from cua.llm.scripted import ScriptedClient

    from .conftest import guard_for

    for srv in servers.values():
        srv.reset()
    trace, _ = run_discovery(
        spec(),
        browser=browser,
        llm=ScriptedClient(with_notice),
        ctx=tc("tenant-a"),
        base_url=servers["a"].url,
        secrets=SECRET_FN,
        evidence_root=tmp_path,
        guard=guard_for(servers["a"].url),
        extra_headers={"X-Fault": "notice"},
    )
    assert trace.status == "succeeded", trace.reason
    assert any(st.element and st.element.dialog == "System Notice" for st in trace.steps)

    cap = compile_(trace)
    assert "click_acknowledge" not in [st.id for st in cap.steps]
    rec = next(d for d in cap.detectors if d.id == "dismiss_system_notice")
    assert isinstance(rec.then, RecoverThen)

    # On a fresh session the notice appears again; the recovery target resolves and clears it.
    s = open_surface(faults="notice")
    run_login(s, tc("tenant-a").login, SECRET_FN)
    runner = StepRunner(s, {"member_id": "10042"}, SECRET_FN, cap.detectors)
    for step in cap.steps[:2]:
        runner.run(step)
    s.act(s.resolve(cap.step("click_search").target), "click")  # type: ignore[arg-type]
    assert "System Notice" in s.page_state().dialog_titles
    assert rec.then.target is not None
    s.act(s.resolve(rec.then.target), "click")
    assert "System Notice" not in s.page_state().dialog_titles
    assert "Member Detail" in s.page_state().title
