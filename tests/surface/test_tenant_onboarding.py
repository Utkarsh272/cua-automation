"""Cross-tenant reuse: probe tenant B, get override proposals, replay the same capability there."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from cua.config import tenant_context
from cua.core.artifact import Capability, load_capability, parse_capability
from cua.core.overrides import apply_ops, capability_dict
from cua.core.results import BusinessOutcome, Failure, FailureCode, Success
from cua.onboarding.probe import Probe

from .conftest import ROOT, guard_for
from .test_replay import SECRET_FN, make_engine, run, tokens  # noqa: F401  (fixtures)

LOOKUP_101 = ROOT / "capabilities" / "lookup_savings_balance" / "1.0.1.yaml"
LOOKUP_110 = ROOT / "capabilities" / "lookup_savings_balance" / "1.1.0.yaml"


def probe(browser: Any, servers: Any, tenant: str) -> Probe:
    servers[tenant].reset()  # earlier tests may have opened accounts for this member
    url = servers[tenant].url
    guard = guard_for(url)
    return Probe(
        browser=browser,
        ctx=tenant_context(f"tenant-{tenant}"),
        secrets=SECRET_FN,
        base_url=url,
        url_guard=lambda u: guard.check_url(u).allowed,
        wait_ms=1200,
        settle_ms=1000,
    )


def with_ops(cap: Capability, tenant: str, ops: list[dict[str, Any]]) -> Capability:
    data = capability_dict(cap)
    data["overrides"] = {tenant: ops}
    data.pop("content_hash", None)
    data["status"] = "draft"
    for key in ("approved_by", "approved_at"):
        data.pop(key, None)
    return parse_capability(data, verify_hash=False)


def test_probe_finds_nothing_to_change_on_the_recorded_tenant(browser: Any, servers: Any) -> None:
    report = probe(browser, servers, "a").run(load_capability(LOOKUP_101), {"member_id": "10042"})
    assert report.works and report.ops == [] and report.rounds == 1
    assert report.outputs_read == ["member_name", "savings_balance"]
    assert {f.level for f in report.findings} == {"ok"}


def test_probe_proposes_tenant_b_overrides_and_they_replay(
    browser: Any,
    servers: Any,
    make_engine: Any,  # noqa: F811
) -> None:
    cap = load_capability(LOOKUP_101, verify_hash=False)
    report = probe(browser, servers, "b").run(cap, {"member_id": "10042"})
    assert report.works, [f.line() for f in report.findings]

    sets = {op["path"]: op["value"] for op in report.ops if op["op"] == "set"}
    assert sets == {
        "steps.enter_member_id.target.strategies[0].text": "Member #",
        "steps.read_savings_balance.target.strategies[0].row_match": "Share Savings",
        # the same relabel carried into the detector that looks for the Savings row
        "detectors.no_savings_account.when.all_of[1].not.element.strategies[0].row_match": (
            "Share Savings"
        ),
    }
    (insert,) = [op for op in report.ops if op["op"] == "insert_step"]
    assert insert["after"] == "enter_member_id" and insert["step"]["action"] == "check"
    assert "privacy notice" in insert["step"]["target"]["strategies"][0]["text"]
    assert any("must acknowledge" in f.message for f in report.findings)

    # The proposals are valid override operations...
    apply_ops(capability_dict(cap), json.loads(json.dumps(report.ops)))
    patched = with_ops(cap, "tenant-b", report.ops)
    # ...the same artifact still runs unchanged on tenant A...
    a = run(make_engine("a"), patched, "10077", attended=True)
    assert isinstance(a, Success) and not a.degraded
    # ...and now runs on tenant B with no fallback locators.
    b = run(make_engine("b"), patched, "10077", attended=True)
    assert isinstance(b, Success), b
    assert not b.degraded and b.outputs["savings_balance"] == 12940.33


def test_without_overrides_tenant_b_fails_and_says_why(make_engine: Any) -> None:  # noqa: F811
    r = run(
        make_engine("b"), load_capability(LOOKUP_101, verify_hash=False), "10077", attended=True
    )
    assert isinstance(r, Failure) and r.code == FailureCode.UNEXPECTED_STATE


def test_shipped_1_1_0_runs_on_both_tenants(make_engine: Any, tmp_path: Path) -> None:  # noqa: F811
    cap = load_capability(LOOKUP_110)
    assert set(cap.overrides) == {"tenant-b"}
    for tenant in ("a", "b"):
        r = run(make_engine(tenant), cap, "10042", attended=True)
        assert isinstance(r, Success) and not r.degraded, (tenant, r)
        assert r.outputs == {"member_name": "Maria Delgado", "savings_balance": 1204.5}
    events = (tmp_path / "runs" / r.run_id / "events.jsonl").read_text()
    assert '"overrides_applied"' in events

    # Business outcomes keep working on tenant B, including the relabelled "no savings" check.
    engine = make_engine("b")
    assert run(engine, cap, "99999", attended=True).code == "MEMBER_NOT_FOUND"
    none = run(make_engine("b"), cap, "10200", attended=True)
    assert isinstance(none, BusinessOutcome) and none.code == "NO_SAVINGS_ACCOUNT"


def test_approved_1_1_0_runs_unattended_on_tenant_b(
    make_engine: Any,  # noqa: F811
    tmp_path: Path,
) -> None:
    from cua.registry.store import Registry

    shipped = load_capability(LOOKUP_110)
    if shipped.status == "approved":  # the repo's copy, once a reviewer has approved it
        cap = shipped
    else:
        reg = Registry(tmp_path / "reg")
        reg.save_draft(shipped, LOOKUP_110.read_text())
        cap = reg.approve("lookup_savings_balance", "1.1.0", approver="reviewer.one")
    r = run(make_engine("b"), cap, "10077", attended=False)
    assert isinstance(r, Success) and not r.degraded, r


def test_probe_refuses_write_capabilities(browser: Any, servers: Any) -> None:
    cap = load_capability(ROOT / "tests/fixtures/reference/open_sub_account.hand_written.yaml")
    report = probe(browser, servers, "b").run(cap, {})
    assert not report.works and "only read flows" in report.findings[0].message
