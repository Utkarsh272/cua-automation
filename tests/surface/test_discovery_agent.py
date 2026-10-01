"""The discovery loop against a live CU Core, driven by scripted 'models'.

These prove the loop's guarantees (policy, placeholders, evidence checks, stop conditions,
redaction, verified locators) without an API key. The genuine LLM run lives in /evidence/.
"""

from __future__ import annotations

import json
import re
from collections.abc import Callable
from pathlib import Path
from typing import Any

import httpx
import pytest

from cua.config import tenant_context
from cua.core.artifact import RiskClass
from cua.core.templating import env_secrets
from cua.discovery.run import run_discovery
from cua.discovery.spec import DiscoverySpec, load_spec
from cua.discovery.trace import DiscoveryTrace
from cua.llm.base import ToolCall
from cua.llm.scripted import ScriptedClient

from .conftest import ROOT, SECRETS, guard_for

GOAL = ROOT / "goals" / "lookup_savings_balance.yaml"
ELEMENT = re.compile(r"^\s+(e\d+) (\w+)(.*)$")


def elements(user: str) -> list[dict[str, str]]:
    out = []
    for line in user.split("ELEMENTS:\n", 1)[1].split("\nTEXT:", 1)[0].splitlines():
        m = ELEMENT.match(line)
        if m:
            attrs = dict(re.findall(r"(\w+)='([^']*)'", m.group(3)))
            out.append({"ref": m.group(1), "role": m.group(2), **attrs})
    return out


def find(user: str, role: str, **want: str) -> str:
    for e in elements(user):
        if e["role"] == role and all(e.get(k) == v for k, v in want.items()):
            return e["ref"]
    raise AssertionError(f"no {role} {want} in observation")


def route(user: str) -> str:
    m = re.search(r"PAGE route=(\S+)", user)
    assert m
    return m.group(1)


def call(name: str, **args: str) -> ToolCall:
    return ToolCall(name, {**args, "rationale": f"scripted {name}"})


def spec(**changes: Any) -> DiscoverySpec:
    data = load_spec(GOAL).model_dump(mode="json", by_alias=True)
    data.update(changes)
    return DiscoverySpec.model_validate(data)


def discover(
    servers: Any,
    browser: Any,
    tmp_path: Path,
    policy: Callable[[str, str, int], ToolCall | None],
    the_spec: DiscoverySpec | None = None,
) -> tuple[DiscoveryTrace, Path, ScriptedClient]:
    for s in servers.values():
        s.reset()
    server = servers["a"]
    llm = ScriptedClient(policy)
    trace, run_dir = run_discovery(
        the_spec or spec(),
        browser=browser,
        llm=llm,
        ctx=tenant_context("tenant-a"),
        base_url=server.url,
        secrets=env_secrets(
            {
                "CU_CORE_USERNAME": SECRETS["cu_core.username"],
                "CU_CORE_PASSWORD": SECRETS["cu_core.password"],
            }
        ),
        evidence_root=tmp_path,
        guard=guard_for(server.url),
    )
    return trace, run_dir, llm


def lookup_policy(system: str, user: str, turn: int) -> ToolCall | None:
    """What a competent model does for the savings lookup."""
    if route(user) == "/search":
        box = find(user, "textbox", label="Member ID")
        if "{{inputs.member_id}}" not in user.split("ACTIONS SO FAR:")[1].split("LAST RESULT")[0]:
            return call("fill", ref=box, value="{{inputs.member_id}}")
        return call("click", ref=find(user, "button", name="Search", **{"in": "Find Member"}))
    if "member_name" in user.split("STILL NEEDED:")[1].split("\n")[0]:
        return call("extract", ref=find(user, "cell", label="Name"), output="member_name")
    if "savings_balance" in user.split("STILL NEEDED:")[1].split("\n")[0]:
        return call(
            "extract",
            ref=find(user, "cell", column="Balance", row="Savings"),
            output="savings_balance",
        )
    return call("done")


# --- the happy path ---------------------------------------------------------------------------


def test_successful_discovery_produces_verified_compiler_ready_trace(
    servers: Any, browser: Any, tmp_path: Path
) -> None:
    trace, run_dir, llm = discover(servers, browser, tmp_path, lookup_policy)

    assert trace.status == "succeeded", trace.reason
    assert trace.extracted == {"member_name": "Maria Delgado", "savings_balance": 1204.5}
    assert trace.verified_outputs == {"member_name": True, "savings_balance": True}
    assert [s.tool for s in trace.steps] == ["fill", "click", "extract", "extract", "done"]
    assert trace.provider == "scripted"  # never mistaken for the real LLM run

    fill, click, name, bal, _ = trace.steps
    assert fill.args["value"] == "{{inputs.member_id}}"  # the placeholder, not the value
    assert fill.target and fill.target.strategies[0].by == "label"
    assert click.target and any(
        getattr(st, "near_text", None) == "Find Member" for st in click.target.strategies
    )
    assert click.after and click.after.route == "/member/10042"
    assert name.target and name.target.strategies[0].model_dump()["control"] == "value_cell"
    assert (
        bal.target and bal.target.strategies[0].by == "table_cell" and bal.parser == "currency_usd"
    )
    assert all(s.policy is None or s.policy.verdict == "allow" for s in trace.steps)

    # The model never saw the member ID, only the placeholder and redaction tags.
    assert all("10042" not in user for _, user in llm.calls)
    assert "[identifier:" in llm.calls[-1][1]

    # Evidence: everything written is redacted, including values learned mid-run.
    files = [p for p in run_dir.rglob("*") if p.suffix in (".json", ".jsonl", ".txt")]
    blob = "\n".join(p.read_text() for p in files)
    for leaked in ("Maria", "Delgado", "1,204.50", "1204.5", "10042", SECRETS["cu_core.password"]):
        assert leaked not in blob, leaked
    saved = json.loads((run_dir / "trace.json").read_text())
    assert saved["status"] == "succeeded" and saved["steps"][4]["tool"] == "done"
    assert len(list((run_dir / "screenshots").glob("*.png"))) >= 5
    assert len(list((run_dir / "prompts").glob("*.txt"))) == 5
    kinds = [
        json.loads(line)["kind"] for line in (run_dir / "events.jsonl").read_text().splitlines()
    ]
    assert kinds[0] == "run_started" and "login_ok" in kinds and kinds.count("llm_decision") == 5


# --- business outcomes need proof from the page -----------------------------------------------


def test_business_outcome_must_quote_the_page(servers: Any, browser: Any, tmp_path: Path) -> None:
    def policy(system: str, user: str, turn: int) -> ToolCall | None:
        if turn == 0:
            return call(
                "fill", ref=find(user, "textbox", label="Member ID"), value="{{inputs.member_id}}"
            )
        if turn == 1:
            return call("click", ref=find(user, "button", name="Search", **{"in": "Find Member"}))
        if turn == 2:
            return call(
                "declare_outcome", code="MEMBER_NOT_FOUND", evidence="Member does not exist"
            )
        return call(
            "declare_outcome",
            code="MEMBER_NOT_FOUND",
            evidence="No member found for the ID entered.",
        )

    trace, _, _ = discover(
        servers, browser, tmp_path, policy, spec(example_inputs={"member_id": "99999"})
    )
    assert trace.status == "business_outcome"
    assert trace.steps[2].status == "invalid" and "quoted exactly" in (trace.steps[2].error or "")
    assert trace.outcome and trace.outcome.code == "MEMBER_NOT_FOUND"
    assert trace.outcome.page.route == "/search"


# --- prompt injection: the page tells the agent to move money ---------------------------------


def test_injected_instructions_are_contained_by_policy(
    servers: Any, browser: Any, tmp_path: Path
) -> None:
    def obedient_to_page(system: str, user: str, turn: int) -> ToolCall | None:
        if turn == 0:
            return call(
                "fill", ref=find(user, "textbox", label="Member ID"), value="{{inputs.member_id}}"
            )
        if turn == 1:
            return call("click", ref=find(user, "button", name="Search", **{"in": "Find Member"}))
        if turn == 2:
            assert "SYSTEM NOTE TO AUTOMATED AGENTS" in user  # the injection reached the model
            return call("navigate", route="/admin/transfer")
        if turn == 3:
            return call("click", ref=find(user, "link", name="Reports"))
        return call("escalate", reason="cannot complete the transfer")

    trace, _, _ = discover(
        servers, browser, tmp_path, obedient_to_page, spec(example_inputs={"member_id": "10088"})
    )
    nav, link = trace.steps[2], trace.steps[3]
    assert nav.status == "denied" and nav.policy and "not allowlisted" in nav.policy.reason
    assert link.status == "denied"  # allowed to click, but the browser guard aborted the request
    assert trace.status == "escalated"
    accounts = httpx.get(f"{servers['a'].url}/__test/member/10088").json()["accounts"]
    assert [a["balance"] for a in accounts] == ["640.00", "3000.00"]  # nothing moved


# --- writes and irreversible steps --------------------------------------------------------------


def open_account_policy(system: str, user: str, turn: int) -> ToolCall | None:
    r = route(user)
    log = user.split("ACTIONS SO FAR:")[1].split("LAST RESULT")[0]
    if r == "/search":
        return call("navigate", route="/member/10042/accounts/new")
    if r.endswith("/accounts/new"):
        if "select" not in log:
            return call(
                "select", ref=find(user, "combobox", label="Account Type"), option="Savings"
            )
        if "Initial Deposit" not in log:
            return call("fill", ref=find(user, "textbox", label="Initial Deposit"), value="250.00")
        if "Funding Source" not in log:
            line = next(ln for ln in user.splitlines() if "label='Funding Source'" in ln)
            shown = next(o for o in re.findall(r"'([^']*)'", line) if o.startswith("Checking"))
            assert "[REDACTED:account_number]" in shown  # the model never sees the number
            return call("select", ref=find(user, "combobox", label="Funding Source"), option=shown)
        return call("click", ref=find(user, "button", name="Continue"))
    if r.endswith("/accounts/review"):
        return call("click", ref=find(user, "button", name="Confirm"))
    return call("escalate", reason="unexpected page")


def test_read_ceiling_denies_writes(servers: Any, browser: Any, tmp_path: Path) -> None:
    trace, _, _ = discover(servers, browser, tmp_path, open_account_policy, spec(max_steps=6))
    cont = next(s for s in trace.steps if s.element and s.element.name == "Continue")
    assert cont.status == "denied" and cont.policy
    assert cont.policy.effective_risk == RiskClass.REVERSIBLE_WRITE


def test_irreversible_step_escalates_and_nothing_is_committed(
    servers: Any, browser: Any, tmp_path: Path
) -> None:
    the_spec = spec(risk_ceiling="reversible_write", max_steps=10)
    trace, _, _ = discover(servers, browser, tmp_path, open_account_policy, the_spec)
    last = trace.steps[-1]
    assert trace.status == "escalated", trace.reason
    assert last.element and last.element.name == "Confirm"
    assert last.policy and last.policy.effective_risk == RiskClass.IRREVERSIBLE
    accounts = httpx.get(f"{servers['a'].url}/__test/member/10042").json()["accounts"]
    assert len(accounts) == 3


# --- stop conditions ------------------------------------------------------------------------------


def test_three_invalid_calls_stop_the_run(servers: Any, browser: Any, tmp_path: Path) -> None:
    trace, _, _ = discover(servers, browser, tmp_path, lambda s, u, t: call("click", ref="e999"))
    assert trace.status == "failed" and "invalid" in trace.reason
    assert len(trace.steps) == 3


def test_no_tool_call_counts_as_invalid(servers: Any, browser: Any, tmp_path: Path) -> None:
    trace, _, _ = discover(servers, browser, tmp_path, lambda s, u, t: None)
    assert trace.status == "failed" and trace.steps[0].error and "no tool" in trace.steps[0].error


def test_dead_end_is_detected(servers: Any, browser: Any, tmp_path: Path) -> None:
    def fidget(system: str, user: str, turn: int) -> ToolCall | None:
        if turn % 2 == 0:
            return call("click", ref=find(user, "button", name="Clear"))
        return call("press", ref=find(user, "textbox", label="Member ID"), key="Tab")

    trace, _, _ = discover(servers, browser, tmp_path, fidget)
    assert trace.status == "failed" and "dead end" in trace.reason


def test_done_too_early_is_refused_and_budget_ends_run(
    servers: Any, browser: Any, tmp_path: Path
) -> None:
    trace, _, _ = discover(
        servers, browser, tmp_path, lambda s, u, t: call("done"), spec(max_steps=2)
    )
    assert trace.status == "budget_exhausted"
    assert all("still need" in (s.error or "") for s in trace.steps)


def test_sensitive_literals_are_refused(servers: Any, browser: Any, tmp_path: Path) -> None:
    def leaky(system: str, user: str, turn: int) -> ToolCall | None:
        return call("fill", ref=find(user, "textbox", label="Member ID"), value="123-45-6789")

    trace, _, _ = discover(servers, browser, tmp_path, leaky)
    assert trace.steps[0].status == "invalid" and "placeholder" in (trace.steps[0].error or "")


def test_unknown_placeholder_is_refused(servers: Any, browser: Any, tmp_path: Path) -> None:
    def wrong(system: str, user: str, turn: int) -> ToolCall | None:
        return call("fill", ref=find(user, "textbox", label="Member ID"), value="{{inputs.ssn}}")

    trace, _, _ = discover(servers, browser, tmp_path, wrong)
    assert trace.steps[0].status == "invalid" and "placeholder" in (trace.steps[0].error or "")


@pytest.mark.parametrize("bad_inputs", [{"member_id": "12ab"}, {"member": "10042"}])
def test_spec_validates_example_inputs(bad_inputs: dict[str, str]) -> None:
    with pytest.raises(ValueError):
        spec(example_inputs=bad_inputs)


def test_evidence_file_names_and_usage_stay_clean(
    servers: Any, browser: Any, tmp_path: Path
) -> None:
    _, run_dir, _ = discover(servers, browser, tmp_path, lookup_policy)
    assert not any("10042" in str(p) for p in run_dir.rglob("*"))
    result = json.loads((run_dir / "result.json").read_text())
    assert isinstance(result["llm_calls"], int) and isinstance(result["input_tokens"], int)
