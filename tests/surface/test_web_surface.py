"""Day 3 acceptance: locate and act on CU Core, inside frames, without test ids."""

from __future__ import annotations

import json
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest
from PIL import Image

from cua.core.artifact import Capability, RecoverThen
from cua.core.conditions import evaluate
from cua.core.parse import parse_value
from cua.core.profile import AppProfile
from cua.core.redact import Redactor
from cua.core.results import FailureCode
from cua.core.targets import FrameRef, TargetSpec
from cua.evidence.store import RunEvidence
from cua.surface.base import ResolutionError
from cua.surface.web import NavigationBlocked, WebSurface

from .conftest import SurfaceFactory, run_steps

MAIN = (FrameRef(name="main"),)


def target(*strategies: dict[str, Any], description: str = "test target") -> TargetSpec:
    return TargetSpec.model_validate(
        {"description": description, "frame": [{"name": "main"}], "strategies": list(strategies)}
    )


def signed_in(open_surface: SurfaceFactory, login_cap: Capability, **kw: Any) -> WebSurface:
    s = open_surface(**kw)
    run_steps(s, login_cap)
    return s


def search(s: WebSurface, member: str, tenant: str = "a") -> None:
    label = "Member ID" if tenant == "a" else "Member #"
    s.act(s.resolve(target({"by": "label", "text": label, "control": "textbox"})), "fill", member)
    if tenant == "b":
        s.act(
            s.resolve(
                target(
                    {
                        "by": "label",
                        "text": "I acknowledge the member privacy notice",
                        "control": "checkbox",
                    }
                )
            ),
            "check",
        )
    button = target({"by": "role", "role": "button", "name": "Search", "near_text": "Find Member"})
    s.act(s.resolve(button), "click")


def detector(cap: Capability | AppProfile, det_id: str) -> Any:
    return next(d for d in cap.detectors if d.id == det_id)


# --- sign-on via the approved login artifact ------------------------------------------------


def test_login_artifact_resolves_inside_frames_and_signs_on(
    open_surface: SurfaceFactory, login_cap: Capability
) -> None:
    s = signed_in(open_surface, login_cap)
    state = s.page_state()
    assert state.route == "/search"
    assert "Member Search" in state.title
    assert evaluate(login_cap.success, state)


# --- the three Day 3 locator paths ----------------------------------------------------------


def test_member_id_role_fails_label_wins_and_is_flagged_degraded(
    open_surface: SurfaceFactory, login_cap: Capability
) -> None:
    s = signed_in(open_surface, login_cap)
    t = target(
        {"by": "role", "role": "textbox", "name": "Member ID"},
        {"by": "label", "text": "Member ID", "control": "textbox"},
        {"by": "attribute", "attr": "name", "value": "ctl00$main$txtMid", "tag": "input"},
    )
    res = s.resolve(t)
    assert res.strategy.by == "label" and res.degraded
    assert res.attempts[0].matches == 0  # no accessible name exists on this legacy input

    by_attr = s.resolve(target(t.strategies[2].model_dump()))
    same = s.frame(MAIN).evaluate(
        "(a) => a.x === a.y", {"x": res.handle[1], "y": by_attr.handle[1]}
    )
    assert same


def test_table_cell_reads_savings_balance(
    open_surface: SurfaceFactory, login_cap: Capability
) -> None:
    s = signed_in(open_surface, login_cap)
    search(s, "10042")
    cell = target(
        {"by": "table_cell", "table_near": "Accounts", "row_match": "Savings", "column": "Balance"}
    )
    raw = s.read(s.resolve(cell))
    assert raw == "$1,204.50"
    assert parse_value("currency_usd", raw) == float(Decimal("1204.50"))


def test_duplicate_button_names_are_ambiguous_until_narrowed(
    open_surface: SurfaceFactory, login_cap: Capability
) -> None:
    s = signed_in(open_surface, login_cap)
    with pytest.raises(ResolutionError) as err:
        s.resolve(target({"by": "role", "role": "button", "name": "Search"}))
    assert err.value.code == FailureCode.TARGET_AMBIGUOUS
    assert err.value.attempts[0].matches == 2

    res = s.resolve(
        target({"by": "role", "role": "button", "name": "Search", "near_text": "Find Member"})
    )
    assert res.handle[1].get_attribute("name") == "ctl00$main$btnSearch"


def test_regenerated_ids_do_not_affect_resolution(
    open_surface: SurfaceFactory, login_cap: Capability
) -> None:
    s = signed_in(open_surface, login_cap)
    t = target({"by": "label", "text": "Member ID", "control": "textbox"})
    first = s.resolve(t).handle[1].get_attribute("id")
    s.navigate("/search", MAIN)
    second = s.resolve(t).handle[1].get_attribute("id")
    assert first and second and first != second


# --- the reference artifact end to end ------------------------------------------------------


@pytest.mark.parametrize(
    ("member", "name", "balance"),
    [("10042", "Maria Delgado", 1204.50), ("10077", "James Okafor", 12940.33)],
)
def test_reference_lookup_artifact_runs_on_real_app(
    open_surface: SurfaceFactory,
    login_cap: Capability,
    lookup_cap: Capability,
    member: str,
    name: str,
    balance: float,
) -> None:
    s = signed_in(open_surface, login_cap)
    raw = run_steps(s, lookup_cap, {"member_id": member})
    outputs = {
        k: parse_value(lookup_cap.step(sid).parse, v)
        for k, v in raw.items()
        for sid in [next(st.id for st in lookup_cap.steps if st.output == k)]
    }
    assert outputs == {"member_name": name, "savings_balance": balance}
    assert evaluate(lookup_cap.success, s.page_state(outputs))


# --- detectors evaluated against real pages -------------------------------------------------


@pytest.mark.parametrize(
    ("member", "fires"),
    [
        ("99999", "member_not_found"),
        ("10666", "access_denied"),
        ("10200", "no_savings_account"),
        ("10042", None),
    ],
)
def test_lookup_detectors_on_real_pages(
    open_surface: SurfaceFactory,
    login_cap: Capability,
    lookup_cap: Capability,
    member: str,
    fires: str | None,
) -> None:
    s = signed_in(open_surface, login_cap)
    search(s, member)
    state = s.page_state()
    fired = {d.id for d in lookup_cap.detectors if evaluate(d.when, state)}
    assert fired == ({fires} if fires else set())


def test_notice_modal_is_detected_and_recoverable(
    open_surface: SurfaceFactory, login_cap: Capability, lookup_cap: Capability
) -> None:
    s = signed_in(open_surface, login_cap, faults="notice")
    search(s, "10042")
    notice = detector(lookup_cap, "system_notice")
    assert "System Notice" in s.page_state().dialog_titles
    assert evaluate(notice.when, s.page_state())

    assert isinstance(notice.then, RecoverThen) and notice.then.target is not None
    s.act(s.resolve(notice.then.target), "click")
    state = s.page_state()
    assert not evaluate(notice.when, state)
    assert "Member Detail" in state.title


def test_unknown_modal_is_seen_as_a_dialog(
    open_surface: SurfaceFactory, login_cap: Capability, profile: AppProfile
) -> None:
    s = signed_in(open_surface, login_cap, faults="surprise_dialog")
    search(s, "10042")
    (dialog,) = s.dialogs()
    assert dialog.title == "Security Update Required"
    assert "Remind me later" in dialog.buttons
    assert evaluate(detector(profile, "security_update_prompt").when, s.page_state())


def test_app_level_detectors(
    open_surface: SurfaceFactory, login_cap: Capability, profile: AppProfile, servers: Any
) -> None:
    s = signed_in(open_surface, login_cap)
    servers["a"].arm("session_expired", "/member")
    search(s, "10042")
    assert evaluate(detector(profile, "session_expired").when, s.page_state())

    s2 = signed_in(open_surface, login_cap, faults="error500=/member")
    search(s2, "10042")
    assert evaluate(detector(profile, "app_error").when, s2.page_state())


# --- tenant B -------------------------------------------------------------------------------


def test_tenant_b_relabel_is_absorbed_but_flagged_and_extra_step_needs_override(
    open_surface: SurfaceFactory, login_cap: Capability, lookup_cap: Capability
) -> None:
    s = signed_in(open_surface, login_cap, tenant="b")
    base = lookup_cap.step("enter_member_id").target
    assert base is not None
    # Same vendor product, same field names: the fallback absorbs "Member ID" -> "Member #",
    # and the resolution is flagged degraded so the relabel is noticed, not hidden.
    res = s.resolve(base)
    assert res.strategy.by == "attribute" and res.degraded
    assert res.attempts[0].matches == 0

    # What the fallback cannot absorb is the extra compliance step: without the override's
    # inserted checkbox step, tenant B refuses the search.
    s.act(res, "fill", "10042")
    submit = lookup_cap.step("submit_search").target
    assert submit is not None
    s.act(s.resolve(submit), "click")
    assert "must acknowledge the member privacy notice" in s.page_state().visible_text

    search(s, "10042", tenant="b")
    share = target(
        {
            "by": "table_cell",
            "table_near": "Accounts",
            "row_match": "Share Savings",
            "column": "Balance",
        }
    )
    assert s.read(s.resolve(share)) == "$1,204.50"


# --- navigation guard -----------------------------------------------------------------------


def test_off_policy_navigation_is_blocked_in_the_browser(
    open_surface: SurfaceFactory, login_cap: Capability
) -> None:
    s = signed_in(open_surface, login_cap)
    with pytest.raises(NavigationBlocked):
        s.navigate("/admin/transfer", MAIN)

    nav = TargetSpec.model_validate(
        {
            "description": "Reports link",
            "frame": [{"name": "nav"}],
            "strategies": [{"by": "role", "role": "link", "name": "Reports"}],
        }
    )
    s.act(s.resolve(nav), "click")
    assert any(u.endswith("/reports") for u in s.blocked)
    assert "Module Not Available" not in s.page_state().title


# --- observation and locator suggestion (used by discovery and the compiler) ----------------


def test_observation_exposes_legacy_labels(
    open_surface: SurfaceFactory, login_cap: Capability
) -> None:
    s = signed_in(open_surface, login_cap)
    obs = s.observe()
    mid = [e for e in obs.elements if e.role == "textbox" and e.label == "Member ID"]
    assert len(mid) == 1 and mid[0].name == "" and mid[0].frame == MAIN
    buttons = [e for e in obs.elements if e.role == "button" and e.name == "Search"]
    assert len(buttons) == 2
    assert all(b.label == "" for b in buttons)  # a button's name is its own text, not layout
    rendered = obs.render()
    assert "label='Member ID'" in rendered and "route=/search" in rendered


def test_suggested_targets_resolve_back_to_the_same_element(
    open_surface: SurfaceFactory, login_cap: Capability
) -> None:
    s = signed_in(open_surface, login_cap)
    obs = s.observe()
    mid = next(e for e in obs.elements if e.label == "Member ID" and e.role == "textbox")
    t = s.suggest_target(mid.ref, "Member ID text box")
    kinds = [st.by for st in t.strategies]
    assert kinds[0] == "label" and "attribute" in kinds and "role" not in kinds
    fp = s.fingerprint(mid.ref)
    assert fp["attributes"] == {"name": "ctl00$main$txtMid", "type": "text"}  # no generated id

    form_search = next(
        e
        for e in obs.elements
        if e.role == "button"
        and e.name == "Search"
        and "Find Member" not in e.row
        and s.fingerprint(e.ref)["attributes"].get("name") == "ctl00$main$btnSearch"
    )
    t2 = s.suggest_target(form_search.ref, "Find Member Search button")
    roles = [st for st in t2.strategies if st.by == "role"]
    assert roles and roles[0].near_text == "Find Member"  # type: ignore[union-attr]
    for tt in (t, t2):
        assert s.resolve(tt).strategy_index == 0

    search(s, "10042")
    obs = s.observe()
    bal = next(
        e
        for e in obs.elements
        if e.role == "cell" and e.column == "Balance" and e.row[0] == "Savings"
    )
    t3 = s.suggest_target(bal.ref, "Savings balance")
    assert t3.strategies[0].model_dump() == {
        "by": "table_cell",
        "table_near": "Accounts",
        "row_match": "Savings",
        "column": "Balance",
        "row_match_mode": "exact",
    }
    name = next(e for e in obs.elements if e.role == "cell" and e.label == "Name")
    t4 = s.suggest_target(name.ref, "Member name")
    assert t4.strategies[0].model_dump() == {"by": "label", "text": "Name", "control": "value_cell"}
    for tt in (t3, t4):
        assert s.resolve(tt).strategy_index == 0


# --- evidence: redaction before anything touches disk ---------------------------------------


def test_screenshot_blacks_out_sensitive_values(
    open_surface: SurfaceFactory, login_cap: Capability, tmp_path: Path
) -> None:
    s = signed_in(open_surface, login_cap)
    search(s, "10042")
    cell = target(
        {"by": "table_cell", "table_near": "Accounts", "row_match": "Savings", "column": "Balance"}
    )
    box = s.resolve(cell).handle[1].bounding_box()
    email = s.resolve(target({"by": "label", "text": "Email", "control": "value_cell"}))
    email_box = email.handle[1].bounding_box()
    checking_no = (
        s.resolve(
            target(
                {
                    "by": "table_cell",
                    "table_near": "Accounts",
                    "row_match": "Checking",
                    "column": "Number",
                }
            )
        )
        .handle[1]
        .bounding_box()
    )
    status = (
        s.resolve(
            target(
                {
                    "by": "table_cell",
                    "table_near": "Accounts",
                    "row_match": "Savings",
                    "column": "Status",
                }
            )
        )
        .handle[1]
        .bounding_box()
    )

    redactor = Redactor.for_run({}, {}, extra_patterns={"account_number": r"\b\d{4}-\d{3}-\d{4}\b"})
    path = s.screenshot(
        str(tmp_path / "shot.png"),
        redact_values=("$1,204.50",),
        redact_patterns=redactor.pattern_sources(),
    )
    img = Image.open(path).convert("RGB")

    def center(b: Any) -> tuple[int, int]:
        return int(b["x"] + b["width"] / 2), int(b["y"] + b["height"] / 2)

    assert img.getpixel(center(box)) == (0, 0, 0)  # known value
    assert img.getpixel(center(email_box)) == (0, 0, 0)  # email pattern
    assert img.getpixel(center(checking_no)) == (0, 0, 0)  # app-profile account pattern
    assert img.getpixel(center(status)) != (0, 0, 0)  # "Open" is not sensitive


def test_evidence_store_never_writes_sensitive_values(
    open_surface: SurfaceFactory, login_cap: Capability, tmp_path: Path, profile: AppProfile
) -> None:
    from .conftest import SECRETS

    s = signed_in(open_surface, login_cap)
    search(s, "10042")
    redactor = Redactor.for_run(
        {"member_id": "identifier", "member_name": "pii", "savings_balance": "financial"},
        {"member_id": "10042", "member_name": "Maria Delgado", "savings_balance": "1204.50"},
        secrets=SECRETS.values(),
        extra_patterns=profile.redaction_patterns,
    )
    ev = RunEvidence(tmp_path, "run_test", redactor)
    obs = s.observe()
    ev.event("observe", route=obs.route, text=obs.text, password=SECRETS["cu_core.password"])
    ev.write_text("observation", obs.render())
    ev.write_json("state", {"outputs": {"savings_balance": 1204.5, "member_name": "Maria Delgado"}})
    ev.screenshot(s, "detail")

    blob = "\n".join(
        p.read_text() for p in ev.dir.rglob("*") if p.suffix in (".jsonl", ".txt", ".json")
    )
    for leaked in (
        "Maria",
        "1,204.50",
        "1204.5",
        "10042",
        "0042-002-7731",
        "maria.delgado@",
        SECRETS["cu_core.password"],
    ):
        assert leaked not in blob, leaked
    assert "[REDACTED:financial]" in blob and "[identifier:" in blob
    assert (ev.dir / "screenshots" / "01-detail.png").exists()
    assert json.loads(ev.events_path.read_text().splitlines()[-1])["kind"] == "screenshot"


def test_native_dialogs_are_recorded_and_not_blindly_confirmed(
    open_surface: SurfaceFactory, login_cap: Capability
) -> None:
    s = signed_in(open_surface, login_cap)
    main = s.frame(MAIN)
    main.evaluate("() => { window.__answer = confirm('Delete everything?'); }")
    assert main.evaluate("() => window.__answer") is False
    assert "Delete everything?" in s.page_state().dialog_titles
