"""CU Core web app. Run with ``uv run cu-core --tenant a --port 8080``."""

from __future__ import annotations

import argparse
import asyncio
import re
import secrets
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any

from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from jinja2 import Environment, FileSystemLoader, select_autoescape

from .data import Member, format_money
from .faults import KNOWN_FAULTS, parse_fault_spec
from .state import AppState, PendingAccount, Session
from .tenants import TENANTS, Tenant, display_kind

HERE = Path(__file__).parent
SESSION_COOKIE = "cu_sid"
FAULT_COOKIE = "cu_fault"
FAULT_HEADER = "x-fault"
CONTENT_PREFIXES = ("/search", "/member")
ACCOUNT_KINDS = ["Savings", "Money Market", "Certificate"]
MIN_DEPOSIT = Decimal("25.00")
MAX_DEPOSIT = Decimal("10000.00")
NICKNAME_RE = re.compile(r"^[A-Za-z0-9 ]{0,20}$")
MEMBER_ID_RE = re.compile(r"^\d{5}$")

# Legacy WebForms-style field names. Element *ids* are regenerated on every render (see gid).
F_USER = "ctl00$main$txtUser"
F_PWD = "ctl00$main$txtPwd"
F_MID = "ctl00$main$txtMid"
F_ACK = "ctl00$main$chkAck"
F_QUICK = "ctl00$hdr$txtQuick"
F_TYPE = "ctl00$main$ddlType"
F_NICK = "ctl00$main$txtNick"
F_DEPOSIT = "ctl00$main$txtDeposit"
F_FUNDING = "ctl00$main$ddlFunding"


class LoginRequired(Exception):
    def __init__(self, expired: bool) -> None:
        self.expired = expired


def gid(prefix: str) -> str:
    """ASP.NET-style generated id that changes on every page load, so it can't be a locator."""
    return f"ctl00_main_{prefix}_{secrets.token_hex(3)}"


def parse_money(raw: str) -> Decimal | None:
    cleaned = raw.strip().replace("$", "").replace(",", "")
    if not cleaned:
        return None
    try:
        value = Decimal(cleaned)
    except InvalidOperation:
        return None
    if not value.is_finite() or value != value.quantize(Decimal("0.01")):
        return None
    return value


def create_app(tenant_key: str = "a", *, test_hooks: bool = True) -> FastAPI:
    tenant: Tenant = TENANTS[tenant_key]
    state = AppState()
    env = Environment(
        loader=FileSystemLoader(HERE / "templates"),
        autoescape=select_autoescape(["html"]),
    )
    env.globals.update(gid=gid, tenant=tenant, money=format_money)
    env.globals["kind_label"] = lambda kind: display_kind(tenant, kind)

    app = FastAPI(title=f"CU Core ({tenant.institution})", docs_url=None, redoc_url=None)
    app.state.cu = state
    app.state.tenant = tenant
    app.mount("/static", StaticFiles(directory=HERE / "static"), name="static")

    def render(name: str, status: int = 200, **ctx: Any) -> HTMLResponse:
        return HTMLResponse(env.get_template(name).render(**ctx), status_code=status)

    def faults(request: Request) -> dict[str, str | None]:
        return getattr(request.state, "faults", {})

    def require_session(request: Request) -> Session:
        sid = request.cookies.get(SESSION_COOKIE)
        if "session_expired" in faults(request):
            state.drop_session(sid)
            raise LoginRequired(expired=True)
        sess = state.get_session(sid)
        if sess is None:
            raise LoginRequired(expired=bool(sid))
        return sess

    def page(
        name: str, request: Request, sess: Session | None, status: int = 200, **ctx: Any
    ) -> HTMLResponse:
        return render(name, status, user=sess.user if sess else None, **ctx)

    def modal_for(request: Request, sess: Session) -> str | None:
        active = faults(request)
        if "surprise_dialog" in active and not sess.surprise_acked:
            return "surprise"
        if "notice" in active and not sess.notice_acked:
            return "notice"
        return None

    def member_or_404(member_id: str) -> Member | None:
        return state.members.get(member_id)

    # --- fault middleware -------------------------------------------------------------------

    @app.middleware("http")
    async def fault_middleware(request: Request, call_next: Any) -> Response:
        path = request.url.path
        if path.startswith(("/static", "/__test")):
            return await call_next(request)
        active = parse_fault_spec(request.headers.get(FAULT_HEADER))
        active.update(parse_fault_spec(request.cookies.get(FAULT_COOKIE)))
        active.update(state.faults.take(path))
        request.state.faults = active

        if "slow" in active:
            await asyncio.sleep(int(active["slow"] or "3000") / 1000)
        if "maintenance" in active and path.startswith(("/login", *CONTENT_PREFIXES)):
            return render("maintenance.html", 503)
        if "error500" in active:
            prefixes = (active["error500"],) if active["error500"] else CONTENT_PREFIXES
            if path.startswith(prefixes):
                return render("error500.html", 500, path=path)
        response: Response = await call_next(request)
        return response

    @app.exception_handler(LoginRequired)
    async def login_required_handler(request: Request, exc: LoginRequired) -> Response:
        target = "/login?expired=1" if exc.expired else "/login"
        resp = RedirectResponse(target, status_code=303)
        resp.delete_cookie(SESSION_COOKIE)
        return resp

    # --- frames -----------------------------------------------------------------------------

    @app.get("/", response_class=HTMLResponse)
    async def frameset(request: Request) -> HTMLResponse:
        sess = state.get_session(request.cookies.get(SESSION_COOKIE))
        return render("frameset.html", main_src="/search" if sess else "/login")

    @app.get("/frame/top", response_class=HTMLResponse)
    async def frame_top() -> HTMLResponse:
        return render("top.html")

    @app.get("/frame/nav", response_class=HTMLResponse)
    async def frame_nav() -> HTMLResponse:
        return render("nav.html")

    # --- auth -------------------------------------------------------------------------------

    @app.get("/login", response_class=HTMLResponse)
    async def login_form(request: Request, expired: int = 0) -> HTMLResponse:
        msg = "Your session has expired. Please sign in again." if expired else None
        return page("login.html", request, None, message=msg, message_kind="warn")

    @app.post("/login")
    async def login_submit(request: Request) -> Response:
        form = await request.form()
        user = str(form.get(F_USER, "")).strip()
        password = str(form.get(F_PWD, ""))
        sess, error = state.login(user, password)
        if sess is None:
            return page("login.html", request, None, message=error, message_kind="error")
        resp = RedirectResponse("/search", status_code=303)
        resp.set_cookie(SESSION_COOKIE, sess.sid, httponly=True, samesite="lax")
        return resp

    @app.get("/logout")
    async def logout(request: Request) -> Response:
        state.drop_session(request.cookies.get(SESSION_COOKIE))
        resp = RedirectResponse("/", status_code=303)
        resp.delete_cookie(SESSION_COOKIE)
        return resp

    # --- search -----------------------------------------------------------------------------

    @app.get("/search", response_class=HTMLResponse)
    async def search_form(request: Request) -> HTMLResponse:
        sess = require_session(request)
        return page("search.html", request, sess, mid="")

    @app.post("/search")
    async def search_submit(request: Request) -> Response:
        sess = require_session(request)
        form = await request.form()
        mid = str(form.get(F_MID, "")).strip()

        def again(msg: str, kind: str = "error", status: int = 200) -> HTMLResponse:
            return page(
                "search.html", request, sess, status, mid=mid, message=msg, message_kind=kind
            )

        if tenant.require_privacy_ack and form.get(F_ACK) != "1":
            return again("You must acknowledge the member privacy notice before searching.")
        if not MEMBER_ID_RE.match(mid):
            return again(f"{tenant.member_id_label} must be 5 digits.")
        member = member_or_404(mid)
        if member is None:
            return again("No member found for the ID entered.", "warn")
        return RedirectResponse(f"/member/{mid}", status_code=303)

    @app.post("/search/quick")
    async def quick_search(request: Request) -> Response:
        sess = require_session(request)
        form = await request.form()
        number = str(form.get(F_QUICK, "")).strip()
        for member in state.members.values():
            if any(a.number == number for a in member.accounts):
                return RedirectResponse(f"/member/{member.member_id}", status_code=303)
        return page(
            "search.html",
            request,
            sess,
            mid="",
            message="No account found for the number entered.",
            message_kind="warn",
        )

    # --- member -----------------------------------------------------------------------------

    def denied(request: Request, sess: Session) -> HTMLResponse:
        return page("denied.html", request, sess, 403)

    @app.get("/member/{member_id}", response_class=HTMLResponse)
    async def member_detail(request: Request, member_id: str) -> Response:
        sess = require_session(request)
        member = member_or_404(member_id)
        if member is None:
            return page(
                "search.html",
                request,
                sess,
                mid=member_id,
                message="No member found for the ID entered.",
                message_kind="warn",
            )
        if member.restricted:
            return denied(request, sess)
        return page(
            "member.html",
            request,
            sess,
            member=member,
            modal=modal_for(request, sess),
            next_url=request.url.path,
        )

    @app.post("/notice/ack")
    async def notice_ack(request: Request) -> Response:
        sess = require_session(request)
        form = await request.form()
        kind = str(form.get("kind", "notice"))
        nxt = str(form.get("next", "/search"))
        if not nxt.startswith("/") or nxt.startswith("//"):
            nxt = "/search"
        if kind == "surprise":
            sess.surprise_acked = True
        else:
            sess.notice_acked = True
        return RedirectResponse(nxt, status_code=303)

    # --- open sub-account (irreversible on Confirm) -----------------------------------------

    def funding_options(member: Member) -> list[Any]:
        return [a for a in member.accounts if a.kind in ("Checking", "Savings")]

    @app.get("/member/{member_id}/accounts/new", response_class=HTMLResponse)
    async def new_account_form(request: Request, member_id: str) -> Response:
        sess = require_session(request)
        member = member_or_404(member_id)
        if member is None:
            return RedirectResponse("/search", status_code=303)
        if member.restricted:
            return denied(request, sess)
        return page(
            "account_new.html",
            request,
            sess,
            member=member,
            kinds=ACCOUNT_KINDS,
            funding=funding_options(member),
            values={},
            errors={},
        )

    @app.post("/member/{member_id}/accounts/new")
    async def new_account_submit(request: Request, member_id: str) -> Response:
        sess = require_session(request)
        member = member_or_404(member_id)
        if member is None:
            return RedirectResponse("/search", status_code=303)
        if member.restricted:
            return denied(request, sess)
        form = await request.form()
        values = {
            "kind": str(form.get(F_TYPE, "")),
            "nickname": str(form.get(F_NICK, "")).strip(),
            "deposit": str(form.get(F_DEPOSIT, "")).strip(),
            "funding": str(form.get(F_FUNDING, "")),
        }
        errors: dict[str, str] = {}
        if values["kind"] not in ACCOUNT_KINDS:
            errors["kind"] = "Select an account type."
        if not NICKNAME_RE.match(values["nickname"]):
            errors["nickname"] = "Nickname may contain letters, numbers and spaces (max 20)."
        funding = next((a for a in funding_options(member) if a.number == values["funding"]), None)
        if funding is None:
            errors["funding"] = "Select a funding account."
        deposit = parse_money(values["deposit"])
        if deposit is None:
            errors["deposit"] = "Initial deposit must be a dollar amount."
        elif deposit < MIN_DEPOSIT:
            errors["deposit"] = "Initial deposit must be at least $25.00."
        elif deposit > MAX_DEPOSIT:
            errors["deposit"] = "Initial deposit cannot exceed $10,000.00."
        elif funding is not None and deposit > funding.balance:
            errors["deposit"] = "Insufficient funds in the funding account."

        if errors:
            return page(
                "account_new.html",
                request,
                sess,
                member=member,
                kinds=ACCOUNT_KINDS,
                funding=funding_options(member),
                values=values,
                errors=errors,
                message="Please correct the errors below.",
                message_kind="error",
            )
        assert deposit is not None and funding is not None
        sess.pending[member_id] = PendingAccount(
            values["kind"], values["nickname"], deposit, funding.number
        )
        return RedirectResponse(f"/member/{member_id}/accounts/review", status_code=303)

    @app.get("/member/{member_id}/accounts/review", response_class=HTMLResponse)
    async def review_account(request: Request, member_id: str) -> Response:
        sess = require_session(request)
        member = member_or_404(member_id)
        pending = sess.pending.get(member_id)
        if member is None or pending is None:
            return RedirectResponse(f"/member/{member_id}/accounts/new", status_code=303)
        return page("account_review.html", request, sess, member=member, pending=pending)

    @app.post("/member/{member_id}/accounts/confirm")
    async def confirm_account(request: Request, member_id: str) -> Response:
        sess = require_session(request)
        member = member_or_404(member_id)
        pending = sess.pending.pop(member_id, None)
        if member is None or pending is None:
            return RedirectResponse(f"/member/{member_id}/accounts/new", status_code=303)
        account = state.open_account(member_id, pending)  # the irreversible effect
        stall = faults(request)
        if "commit_timeout" in stall:
            await asyncio.sleep(int(stall["commit_timeout"] or "30000") / 1000)
        return RedirectResponse(
            f"/member/{member_id}/accounts/done?acct={account.number}", status_code=303
        )

    @app.get("/member/{member_id}/accounts/done", response_class=HTMLResponse)
    async def account_done(request: Request, member_id: str, acct: str = "") -> Response:
        sess = require_session(request)
        member = member_or_404(member_id)
        if member is None:
            return RedirectResponse("/search", status_code=303)
        account = next((a for a in member.accounts if a.number == acct), None)
        return page("account_done.html", request, sess, member=member, account=account)

    # --- modules this service account is not licensed for -----------------------------------

    @app.get("/loans", response_class=HTMLResponse)
    @app.get("/reports", response_class=HTMLResponse)
    async def unlicensed(request: Request) -> HTMLResponse:
        sess = require_session(request)
        return page("unlicensed.html", request, sess, 404)

    @app.get("/admin/{rest:path}", response_class=HTMLResponse)
    async def unlicensed_admin(request: Request, rest: str) -> HTMLResponse:
        sess = require_session(request)
        return page("unlicensed.html", request, sess, 404)

    # --- test hooks (local development and CI only) ------------------------------------------

    if test_hooks:

        @app.post("/__test/arm")
        async def arm_fault(request: Request) -> JSONResponse:
            body = await request.json()
            try:
                state.faults.arm(
                    str(body["fault"]),
                    body.get("value"),
                    str(body.get("path_prefix", "/")),
                    int(body.get("times", 1)),
                )
            except (KeyError, ValueError) as exc:
                return JSONResponse({"error": str(exc)}, status_code=400)
            return JSONResponse({"armed": state.faults.snapshot()})

        @app.get("/__test/faults")
        async def list_faults() -> JSONResponse:
            return JSONResponse({"known": KNOWN_FAULTS, "armed": state.faults.snapshot()})

        @app.post("/__test/reset")
        async def reset() -> JSONResponse:
            state.reset()
            return JSONResponse({"ok": True})

        @app.get("/__test/member/{member_id}")
        async def member_json(member_id: str) -> JSONResponse:
            member = state.members.get(member_id)
            if member is None:
                return JSONResponse({"error": "not found"}, status_code=404)
            return JSONResponse(
                {
                    "member_id": member.member_id,
                    "accounts": [
                        {
                            "number": a.number,
                            "kind": a.kind,
                            "nickname": a.nickname,
                            "balance": str(a.balance),
                        }
                        for a in member.accounts
                    ],
                }
            )

    return app


def run() -> None:
    import uvicorn

    parser = argparse.ArgumentParser(description="Run the CU Core target app.")
    parser.add_argument("--tenant", choices=sorted(TENANTS), default="a")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=None, help="default 8080 (a) / 8081 (b)")
    parser.add_argument("--no-test-hooks", action="store_true")
    args = parser.parse_args()
    port = args.port or (8080 if args.tenant == "a" else 8081)
    uvicorn.run(
        create_app(args.tenant, test_hooks=not args.no_test_hooks), host=args.host, port=port
    )


if __name__ == "__main__":
    run()
