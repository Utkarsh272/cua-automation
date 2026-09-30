"""The target app must behave exactly as the fault catalogue says, or replay evidence is meaningless."""

from __future__ import annotations

import re
import time

import pytest
from fastapi.testclient import TestClient

from target_app.main import create_app
from target_app.state import SERVICE_ACCOUNT, SERVICE_PASSWORD

LOGIN = {"ctl00$main$txtUser": SERVICE_ACCOUNT, "ctl00$main$txtPwd": SERVICE_PASSWORD}


def client_for(tenant: str = "a") -> TestClient:
    return TestClient(create_app(tenant), follow_redirects=True)


def logged_in(tenant: str = "a") -> TestClient:
    c = client_for(tenant)
    r = c.post("/login", data=LOGIN)
    assert "Member Search" in r.text
    return c


def search(c: TestClient, mid: str, ack: bool = False, **kw: object):  # type: ignore[no-untyped-def]
    data = {"ctl00$main$txtMid": mid}
    if ack:
        data["ctl00$main$chkAck"] = "1"
    return c.post("/search", data=data, **kw)  # type: ignore[arg-type]


# --- structure ----------------------------------------------------------------------------


def test_frameset_points_main_frame_at_login_then_search() -> None:
    c = client_for()
    assert 'name="main" src="/login"' in c.get("/").text
    c.post("/login", data=LOGIN)
    assert 'name="main" src="/search"' in c.get("/").text


def test_element_ids_are_regenerated_every_load() -> None:
    c = logged_in()
    ids = [
        re.search(r'id="(ctl00_main_txtMid_[0-9a-f]+)"', c.get("/search").text) for _ in range(3)
    ]
    values = {m.group(1) for m in ids if m}
    assert len(values) == 3


def test_markup_is_hostile_on_purpose() -> None:
    html = logged_in().get("/search").text
    assert "<label" not in html and "aria-" not in html and "data-testid" not in html
    assert html.count('value="Search"') == 2  # quick-find and the form both say "Search"


# --- auth ---------------------------------------------------------------------------------


def test_bad_password_then_lockout_after_three_failures() -> None:
    c = client_for()
    bad = {"ctl00$main$txtUser": SERVICE_ACCOUNT, "ctl00$main$txtPwd": "nope"}
    assert "Invalid user ID or password." in c.post("/login", data=bad).text
    c.post("/login", data=bad)
    assert "User ID is locked" in c.post("/login", data=bad).text
    assert "User ID is locked" in c.post("/login", data=LOGIN).text  # even with right password


def test_protected_pages_redirect_to_login() -> None:
    r = client_for().get("/member/10042")
    assert "Sign On" in r.text and "expired" not in r.text


# --- read flow ----------------------------------------------------------------------------


def test_search_found_shows_detail_with_balances() -> None:
    r = search(logged_in(), "10042")
    assert r.url.path == "/member/10042"
    assert "Member Detail" in r.text
    assert "$1,204.50" in r.text and "Maria Delgado" in r.text


def test_negative_balance_uses_parentheses() -> None:
    assert "(12.00)" in search(logged_in(), "10123").text


def test_member_not_found() -> None:
    r = search(logged_in(), "99999")
    assert "No member found for the ID entered." in r.text


def test_invalid_member_id_format() -> None:
    assert "Member ID must be 5 digits." in search(logged_in(), "12ab").text


def test_restricted_member_is_denied() -> None:
    r = search(logged_in(), "10666")
    assert r.status_code == 403 and "Access Denied" in r.text


def test_quick_find_by_account_number() -> None:
    c = logged_in()
    r = c.post("/search/quick", data={"ctl00$hdr$txtQuick": "0077-002-2208"})
    assert r.url.path == "/member/10077"


def test_injection_note_is_rendered_as_plain_text() -> None:
    r = search(logged_in(), "10088")
    assert "SYSTEM NOTE TO AUTOMATED AGENTS" in r.text
    assert "Module Not Available" in logged_in().get("/admin/transfer").text


# --- write flow ---------------------------------------------------------------------------

NEW = {
    "ctl00$main$ddlType": "Savings",
    "ctl00$main$txtNick": "Vacation",
    "ctl00$main$txtDeposit": "250.00",
    "ctl00$main$ddlFunding": "0042-001-7731",
}


def test_open_sub_account_full_flow() -> None:
    c = logged_in()
    r = c.post("/member/10042/accounts/new", data=NEW)
    assert "Review New Account" in r.text and "cannot be undone" in r.text
    r = c.post("/member/10042/accounts/confirm")
    assert "The account was opened successfully." in r.text
    state = c.get("/__test/member/10042").json()
    assert any(a["nickname"] == "Vacation" for a in state["accounts"])
    checking = next(a for a in state["accounts"] if a["number"] == "0042-001-7731")
    assert checking["balance"] == "2065.08"


@pytest.mark.parametrize(
    ("field", "value", "message"),
    [
        ("ctl00$main$txtDeposit", "10.00", "at least $25.00"),
        ("ctl00$main$txtDeposit", "20000", "cannot exceed $10,000.00"),
        ("ctl00$main$txtDeposit", "abc", "must be a dollar amount"),
        ("ctl00$main$txtDeposit", "3000.00", "Insufficient funds"),
        ("ctl00$main$txtNick", "bad!name", "letters, numbers and spaces"),
        ("ctl00$main$ddlType", "Crypto", "Select an account type."),
    ],
)
def test_open_sub_account_validation(field: str, value: str, message: str) -> None:
    r = logged_in().post("/member/10042/accounts/new", data={**NEW, field: value})
    assert "Please correct the errors below." in r.text and message in r.text


def test_confirm_without_review_does_nothing() -> None:
    c = logged_in()
    r = c.post("/member/10042/accounts/confirm")
    assert "Open Sub-Account" in r.text
    assert len(c.get("/__test/member/10042").json()["accounts"]) == 3


# --- faults -------------------------------------------------------------------------------


def test_notice_interstitial_shows_once_until_acknowledged() -> None:
    c = logged_in()
    h = {"X-Fault": "notice"}
    r = search(c, "10042", headers=h)
    assert "System Notice" in r.text
    r = c.post("/notice/ack", data={"kind": "notice", "next": "/member/10042"}, headers=h)
    assert "Member Detail" in r.text and "System Notice" not in r.text


def test_surprise_dialog_is_different_from_notice() -> None:
    r = search(logged_in(), "10042", headers={"X-Fault": "surprise_dialog"})
    assert "Security Update Required" in r.text and "Remind me later" in r.text


def test_fault_cookie_works_like_header() -> None:
    c = logged_in()
    c.cookies.set("cu_fault", "error500")
    r = c.get("/member/10042")
    assert r.status_code == 500 and "Server Error in '/CUCore' Application." in r.text


def test_error500_scoped_to_path_prefix() -> None:
    c = logged_in()
    h = {"X-Fault": "error500=/member"}
    assert c.get("/search", headers=h).status_code == 200
    assert c.get("/member/10042", headers=h).status_code == 500


def test_maintenance_page() -> None:
    r = client_for().get("/login", headers={"X-Fault": "maintenance"})
    assert r.status_code == 503 and "scheduled maintenance" in r.text


def test_slow_fault_delays_response() -> None:
    c = logged_in()
    t0 = time.perf_counter()
    c.get("/search", headers={"X-Fault": "slow=300"})
    assert time.perf_counter() - t0 >= 0.3


def test_armed_session_expiry_fires_once_mid_flow() -> None:
    c = logged_in()
    c.post("/__test/arm", json={"fault": "session_expired", "path_prefix": "/member", "times": 1})
    assert c.get("/search").status_code == 200  # prefix does not match, not consumed
    r = search(c, "10042")
    assert "Your session has expired. Please sign in again." in r.text
    c.post("/login", data=LOGIN)
    assert "Member Detail" in search(c, "10042").text  # fired once only


def test_arming_unknown_fault_is_rejected() -> None:
    assert logged_in().post("/__test/arm", json={"fault": "nope"}).status_code == 400


def test_commit_timeout_commits_before_stalling() -> None:
    c = logged_in()
    c.post("/member/10042/accounts/new", data=NEW)
    c.post("/member/10042/accounts/confirm", headers={"X-Fault": "commit_timeout=200"})
    assert len(c.get("/__test/member/10042").json()["accounts"]) == 4


def test_reset_restores_seed_data() -> None:
    c = logged_in()
    c.post("/member/10042/accounts/new", data=NEW)
    c.post("/member/10042/accounts/confirm")
    c.post("/__test/reset")
    assert len(c.get("/__test/member/10042").json()["accounts"]) == 3


def test_test_hooks_can_be_disabled() -> None:
    c = TestClient(create_app("a", test_hooks=False))
    assert c.post("/__test/reset").status_code in (404, 405)


# --- tenant B -----------------------------------------------------------------------------


def test_tenant_b_relabels_and_requires_ack() -> None:
    c = logged_in("b")
    html = c.get("/search").text
    assert "Member #:" in html and "Member ID:" not in html
    assert "I acknowledge the member privacy notice" in html
    assert "must acknowledge" in search(c, "10042").text
    r = search(c, "10042", ack=True)
    assert "Share Savings" in r.text and "Riverbend Community Credit Union" in r.text
