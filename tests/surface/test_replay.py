"""The replay engine against live CU Core: every result kind and every injected fault.

No model is involved anywhere in this file.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import httpx
import pytest

from cua.config import tenant_context
from cua.core.artifact import Capability, load_capability, parse_capability
from cua.core.authz import Caller, StaticOwnership
from cua.core.confirmation import ConfirmationTokens
from cua.core.policy import PolicyGuard, load_policy
from cua.core.results import BusinessOutcome, Failure, FailureCode, NeedsHuman, Success
from cua.core.templating import env_secrets
from cua.registry.store import Registry
from cua.replay.engine import ReplayEngine

from .conftest import ROOT, SECRETS

LOOKUP_101 = ROOT / "capabilities" / "lookup_savings_balance" / "1.0.1.yaml"
OPEN_ACCOUNT = ROOT / "tests" / "fixtures" / "reference" / "open_sub_account.hand_written.yaml"
TOKEN_SECRET = b"replay-test-secret-0123456789"
SECRET_FN = env_secrets(
    {
        "CU_CORE_USERNAME": SECRETS["cu_core.username"],
        "CU_CORE_PASSWORD": SECRETS["cu_core.password"],
    }
)
STAFF = Caller("agent:staff-tool", frozenset({"invoke:*", "staff"}), frozenset({"*"}))
WRITER = Caller("agent:staff-tool", frozenset({"invoke:*", "staff", "write"}), frozenset({"*"}))
OWNERS = StaticOwnership({"member:james.okafor": frozenset({"10077"})})
NEW_ACCOUNT = {
    "member_id": "10042",
    "account_type": "Savings",
    "nickname": "Vacation",
    "initial_deposit": "250.00",
    "funding_account": "0042-001-7731",
}


@pytest.fixture
def tokens() -> ConfirmationTokens:
    return ConfirmationTokens(TOKEN_SECRET)


@pytest.fixture
def make_engine(browser: Any, servers: Any, tmp_path: Path, tokens: ConfirmationTokens) -> Any:
    for s in servers.values():
        s.reset()

    def make(tenant: str = "a", faults: str | None = None, handoff: Any = None) -> ReplayEngine:
        server = servers[tenant]
        ctx = tenant_context(f"tenant-{tenant}")
        policy = load_policy(ROOT / "config" / "policies" / "cu-core.yaml")
        guard = PolicyGuard(policy.model_copy(update={"allowed_origins": (server.url,)}), tokens)
        return ReplayEngine(
            browser=browser,
            ctx=ctx,
            secrets=SECRET_FN,
            evidence_root=tmp_path / "runs",
            interventions_dir=tmp_path / "interventions",
            guard=guard,
            base_url=server.url,
            extra_headers={"X-Fault": faults} if faults else None,
            max_wait_ms=1500,
            settle_ms=1000,
            handoff=handoff,
        )

    return make


def lookup() -> Capability:
    """1.0.1 as a draft, whether or not the repo's copy has been approved since."""
    data = load_capability(LOOKUP_101).model_dump(mode="json", by_alias=True, exclude_none=True)
    for key in ("content_hash", "approved_by", "approved_at"):
        data.pop(key, None)
    data["status"] = "draft"
    return parse_capability(data)


def run(engine: ReplayEngine, cap: Capability, member: str, **kw: Any) -> Any:
    kw.setdefault("caller", STAFF)
    kw.setdefault("ownership", OWNERS)
    kw.setdefault("attended", cap.status == "draft")
    return engine.run(cap, {"member_id": member} if isinstance(member, str) else member, **kw)


def evidence_dir(result: Any, tmp_path: Path) -> Path:
    return tmp_path / "runs" / result.run_id


def accounts(servers: Any, member: str, tenant: str = "a") -> list[dict[str, str]]:
    return httpx.get(f"{servers[tenant].url}/__test/member/{member}").json()["accounts"]  # type: ignore[no-any-return]


# --- success and business outcomes ------------------------------------------------------------


def test_success_returns_typed_outputs_with_no_model(make_engine: Any, tmp_path: Path) -> None:
    r = run(make_engine(), lookup(), "10077")
    assert isinstance(r, Success), r
    assert r.outputs == {"member_name": "James Okafor", "savings_balance": 12940.33}
    assert (r.llm_calls, r.degraded, r.recoveries) == (0, False, ())
    d = evidence_dir(r, tmp_path)
    blob = "\n".join(p.read_text() for p in d.rglob("*") if p.suffix in (".json", ".jsonl", ".txt"))
    assert "James" not in blob and "12,940" not in blob and "10077" not in blob
    assert json.loads((d / "result.json").read_text())["kind"] == "success"


@pytest.mark.parametrize(
    ("member", "code", "step"),
    [
        ("99999", "MEMBER_NOT_FOUND", "click_search"),
        ("10666", "ACCESS_DENIED", "click_search"),
        ("10200", "NO_SAVINGS_ACCOUNT", "click_search"),
    ],
)
def test_business_outcomes(make_engine: Any, member: str, code: str, step: str) -> None:
    r = run(make_engine(), lookup(), member)
    assert isinstance(r, BusinessOutcome), r
    assert (r.code, r.at_step) == (code, step)


def test_invalid_input_never_opens_a_browser(make_engine: Any, tmp_path: Path) -> None:
    r = run(make_engine(), lookup(), "12ab")
    assert isinstance(r, BusinessOutcome) and r.code == "INVALID_INPUT" and r.at_step is None
    assert "does not match" in r.detail
    assert not (evidence_dir(r, tmp_path) / "screenshots").exists()


# --- recoverable conditions -------------------------------------------------------------------


def test_notice_interstitial_is_dismissed(make_engine: Any) -> None:
    r = run(make_engine(faults="notice"), lookup(), "10077")
    assert isinstance(r, Success), r
    assert [(e.detector, e.action) for e in r.recoveries] == [("dismiss_system_notice", "click")]


def test_session_expiry_relogs_and_restarts(make_engine: Any, servers: Any) -> None:
    engine = make_engine()
    servers["a"].arm("session_expired", "/member", times=1)
    r = run(engine, lookup(), "10077")
    assert isinstance(r, Success), r
    assert [(e.detector, e.action) for e in r.recoveries] == [("session_expired", "relogin")]


def test_transient_slowness_gets_one_extended_wait(make_engine: Any, servers: Any) -> None:
    engine = make_engine()  # settle 1.0 s + wait 1.5 s, then one extension of 1.5 s
    httpx.post(
        f"{servers['a'].url}/__test/arm",
        json={"fault": "slow", "value": "3500", "path_prefix": "/member"},
    )
    r = run(engine, lookup(), "10077")
    assert isinstance(r, Success), r
    assert ("slow_page", "extended_wait") in [(e.detector, e.action) for e in r.recoveries]


# --- failures ------------------------------------------------------------------------------------


def test_persistent_slowness_is_a_retryable_timeout(make_engine: Any, servers: Any) -> None:
    engine = make_engine()
    httpx.post(
        f"{servers['a'].url}/__test/arm",
        json={"fault": "slow", "value": "6000", "path_prefix": "/member"},
    )
    r = run(engine, lookup(), "10077")
    assert isinstance(r, Failure), r
    assert (r.code, r.at_step, r.retryable, r.side_effects) == (
        FailureCode.TIMEOUT,
        "click_search",
        True,
        "none",
    )


def test_app_error_page_fails_with_evidence(make_engine: Any, tmp_path: Path) -> None:
    r = run(make_engine(faults="error500=/member"), lookup(), "10077")
    assert isinstance(r, Failure), r
    assert (r.code, r.at_step, r.retryable) == (FailureCode.APP_ERROR, "click_search", False)
    assert r.evidence == ("screenshots/01-failure.png", "page_at_stop.txt")
    assert all((evidence_dir(r, tmp_path) / p).exists() for p in r.evidence)


def test_maintenance_is_unavailable_and_retryable(make_engine: Any) -> None:
    r = run(make_engine(faults="maintenance"), lookup(), "10077")
    assert isinstance(r, Failure), r
    assert (r.code, r.retryable) == (FailureCode.APP_UNAVAILABLE, True)
    assert r.at_step == "open_login"


def test_security_prompt_escalates_to_a_human(make_engine: Any, tmp_path: Path) -> None:
    r = run(make_engine(faults="surprise_dialog"), lookup(), "10077")
    assert isinstance(r, NeedsHuman), r
    assert "Security update" in r.reason
    record = json.loads((tmp_path / "interventions" / f"{r.intervention_id}.json").read_text())
    assert record["status"] == "open" and record["step"] == r.at_step


def test_tenant_b_reports_the_page_message_not_a_vague_timeout(make_engine: Any) -> None:
    r = run(make_engine("b"), lookup(), "10077")
    assert isinstance(r, Failure), r
    assert (r.code, r.at_step) == (FailureCode.UNEXPECTED_STATE, "click_search")
    assert "must acknowledge the member privacy notice" in r.observed


def test_degraded_locator_still_succeeds_and_is_flagged(make_engine: Any) -> None:
    data = lookup().model_dump(mode="json", by_alias=True, exclude_none=True)
    data["steps"][1]["target"]["strategies"][0]["text"] = "Member Number"  # simulate a relabel
    r = run(make_engine(), parse_capability(data), "10077")
    assert isinstance(r, Success) and r.degraded


# --- authorization ---------------------------------------------------------------------------------


def test_drafts_do_not_run_unattended(make_engine: Any) -> None:
    r = run(make_engine(), lookup(), "10077", attended=False)
    assert isinstance(r, Failure) and r.code == FailureCode.NOT_APPROVED


def test_subject_binding_blocks_another_members_data(make_engine: Any) -> None:
    james = Caller(
        "agent:chat",
        frozenset({"invoke:lookup_savings_balance"}),
        frozenset({"*"}),
        on_behalf_of="member:james.okafor",
    )
    assert isinstance(run(make_engine(), lookup(), "10077", caller=james), Success)
    r = run(make_engine(), lookup(), "10042", caller=james)
    assert isinstance(r, Failure) and r.code == FailureCode.NOT_AUTHORIZED


def test_approved_version_runs_unattended(make_engine: Any) -> None:
    cap = Registry(ROOT / "capabilities").resolve("lookup_savings_balance", "1.0.0")
    assert cap.status == "approved"
    r = run(make_engine(), cap, "10077", attended=False)
    assert isinstance(r, Success), r


# --- the write path: irreversible steps ------------------------------------------------------------


def open_account() -> Capability:
    return load_capability(OPEN_ACCOUNT)


def test_writes_need_the_write_scope(make_engine: Any) -> None:
    r = run(make_engine(), open_account(), NEW_ACCOUNT, caller=STAFF)
    assert isinstance(r, Failure) and r.code == FailureCode.NOT_AUTHORIZED


def test_irreversible_step_without_confirmation_stops_before_acting(
    make_engine: Any, servers: Any
) -> None:
    r = run(make_engine(), open_account(), NEW_ACCOUNT, caller=WRITER)
    assert isinstance(r, NeedsHuman), r
    assert r.at_step == "confirm" and "confirmation" in r.reason
    assert len(accounts(servers, "10042")) == 3  # nothing committed


def test_valid_confirmation_commits_once(
    make_engine: Any, servers: Any, tokens: ConfirmationTokens
) -> None:
    token = tokens.issue(
        run_id="run-ok", step_id="confirm", inputs=NEW_ACCOUNT, issued_to="op:alice"
    )
    r = run(
        make_engine(),
        open_account(),
        NEW_ACCOUNT,
        caller=WRITER,
        run_id="run-ok",
        confirmations={"confirm": token},
    )
    assert isinstance(r, Success), r
    assert r.outputs["new_account_number"].startswith("0042-")
    assert len(accounts(servers, "10042")) == 4


def test_confirmation_for_other_inputs_is_a_policy_violation(
    make_engine: Any, servers: Any, tokens: ConfirmationTokens
) -> None:
    other = {**NEW_ACCOUNT, "initial_deposit": "25.00"}
    token = tokens.issue(run_id="run-x", step_id="confirm", inputs=other, issued_to="op:alice")
    r = run(
        make_engine(),
        open_account(),
        NEW_ACCOUNT,
        caller=WRITER,
        run_id="run-x",
        confirmations={"confirm": token},
    )
    assert isinstance(r, Failure) and r.code == FailureCode.POLICY_VIOLATION
    assert "wrong_binding" in r.observed
    assert len(accounts(servers, "10042")) == 3


def test_application_validation_is_a_business_outcome(make_engine: Any) -> None:
    r = run(
        make_engine(), open_account(), {**NEW_ACCOUNT, "initial_deposit": "10.00"}, caller=WRITER
    )
    assert isinstance(r, BusinessOutcome), r
    assert (r.code, r.at_step) == ("VALIDATION_ERROR", "click_continue")
    assert "at least $25.00" in r.detail


def test_timeout_after_commit_reports_possible_side_effects(
    make_engine: Any, servers: Any, tokens: ConfirmationTokens
) -> None:
    httpx.post(
        f"{servers['a'].url}/__test/arm",
        json={
            "fault": "commit_timeout",
            "value": "5000",
            "path_prefix": "/member/10042/accounts/confirm",
        },
    )
    token = tokens.issue(
        run_id="run-t", step_id="confirm", inputs=NEW_ACCOUNT, issued_to="op:alice"
    )
    r = run(
        make_engine(),
        open_account(),
        NEW_ACCOUNT,
        caller=WRITER,
        run_id="run-t",
        confirmations={"confirm": token},
    )
    assert isinstance(r, Failure), r
    assert (r.code, r.at_step, r.side_effects, r.retryable) == (
        FailureCode.TIMEOUT,
        "confirm",
        "possible",
        False,
    )
    assert len(accounts(servers, "10042")) == 4  # it did commit: exactly why we never auto-retry


# --- multi-run stability -------------------------------------------------------------------------


def test_stability_report_for_a_clean_capability(make_engine: Any) -> None:
    from cua.replay.stability import measure

    cap = lookup()
    report = measure(lambda: run(make_engine(), cap, "10077"), cap, "tenant-a", 4)
    assert report.stable and report.results == {"success": 4}
    assert (report.success_rate, report.distinct_outputs, report.degraded_runs) == (1.0, 1, 0)
    assert report.llm_calls == 0 and len(set(report.run_ids)) == 4
    assert "James" not in report.to_json() and "12940" not in report.to_json()


def test_stability_report_flags_an_intermittent_failure(make_engine: Any, servers: Any) -> None:
    from cua.replay.stability import measure

    cap = lookup()
    engine = make_engine()
    servers["a"].arm("error500", "/member", times=1)  # one run of three hits a server error
    report = measure(lambda: run(engine, cap, "10077"), cap, "tenant-a", 3)
    assert not report.stable
    assert report.results == {"failure:APP_ERROR": 1, "success": 2}
    assert report.not_success[0]["at_step"] == "click_search"


def test_a_consistent_business_outcome_is_stable(make_engine: Any) -> None:
    from cua.replay.stability import measure

    cap = lookup()
    report = measure(lambda: run(make_engine(), cap, "99999"), cap, "tenant-a", 2)
    assert report.stable and report.results == {"business_outcome:MEMBER_NOT_FOUND": 2}


def test_stability_refuses_write_capabilities() -> None:
    from cua.replay.stability import measure

    with pytest.raises(ValueError, match="only read"):
        measure(lambda: None, open_account(), "tenant-a", 3)  # type: ignore[arg-type,return-value]
