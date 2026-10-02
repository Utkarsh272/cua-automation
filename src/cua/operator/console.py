"""Operator console: a small web app over the intervention store.

It runs in the same process as the engine (background thread) and shares the store, so a claim
or decision made here is seen by the paused run on its next poll. The operator drives the
*actual* browser window of the paused session; this page is where they claim it, see why it
paused, and hand it back.

Authorization: an operator signs in with a bearer token (``config/operators.yaml`` names the
environment variable holding it) and can only see and act on tenants they are assigned to.
Everything shown here has already been through the redactor.
"""

from __future__ import annotations

import hmac
import os
import threading
import time
from collections.abc import Mapping
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

import uvicorn
from fastapi import Depends, FastAPI, Header, HTTPException
from fastapi.responses import FileResponse, HTMLResponse
from pydantic import BaseModel
from ruamel.yaml import YAML

from cua.core.authz import Operator, authorize_operator
from cua.handoff.store import Intervention, InterventionError, InterventionStore

PAGE = (Path(__file__).parent / "console.html").read_text(encoding="utf-8")


@dataclass(frozen=True)
class OperatorAccount:
    operator: Operator
    token: str


def load_operators(path: str | Path, env: Mapping[str, str] | None = None) -> list[OperatorAccount]:
    """Operators whose token variable is set (at least 16 characters). Others cannot sign in."""
    env = os.environ if env is None else env
    data = YAML(typ="safe").load(Path(path).read_text(encoding="utf-8")) or {}
    out = []
    for row in data.get("operators", []):
        token = env.get(str(row["token_env"]), "")
        if len(token) >= 16:
            out.append(OperatorAccount(Operator(str(row["id"]), frozenset(row["tenants"])), token))
    return out


class DecisionBody(BaseModel):
    action: str
    note: str = ""


def create_console(store: InterventionStore, accounts: list[OperatorAccount]) -> FastAPI:
    app = FastAPI(title="CUA operator console", docs_url=None, redoc_url=None)

    def operator(authorization: str = Header(default="")) -> Operator:
        presented = authorization.removeprefix("Bearer ").strip()
        for acct in accounts:
            if presented and hmac.compare_digest(presented.encode(), acct.token.encode()):
                return acct.operator
        raise HTTPException(401, "unknown operator token")

    def item_for(iid: str, op: Operator, action: str) -> Intervention:
        try:
            item = store.get(iid)
        except InterventionError as exc:
            raise HTTPException(404, str(exc)) from exc
        decision = authorize_operator(
            op, tenant=item.tenant, action="abort" if action == "abort" else "claim"
        )
        if not decision.allowed:
            raise HTTPException(403, decision.reason)
        return item

    def public(item: Intervention) -> dict[str, Any]:
        data = asdict(item)
        for private in ("created_monotonic", "last_heartbeat", "screenshot"):
            data.pop(private)
        data["has_screenshot"] = bool(item.screenshot)
        return data

    @app.get("/", response_class=HTMLResponse)
    def index() -> str:
        return PAGE

    @app.get("/api/me")
    def me(op: Operator = Depends(operator)) -> dict[str, Any]:
        return {"id": op.principal, "tenants": sorted(op.tenants)}

    @app.get("/api/interventions")
    def interventions(op: Operator = Depends(operator)) -> list[dict[str, Any]]:
        return [
            public(i)
            for i in store.list()
            if authorize_operator(op, tenant=i.tenant, action="claim").allowed
        ]

    @app.get("/api/interventions/{iid}/screenshot")
    def screenshot(iid: str, op: Operator = Depends(operator)) -> FileResponse:
        item = item_for(iid, op, "claim")
        if not item.screenshot or not Path(item.screenshot).is_file():
            raise HTTPException(404, "no screenshot")
        return FileResponse(item.screenshot, media_type="image/png")

    @app.post("/api/interventions/{iid}/claim")
    def claim(iid: str, op: Operator = Depends(operator)) -> dict[str, Any]:
        item_for(iid, op, "claim")
        try:
            return public(store.claim(iid, op.principal))
        except InterventionError as exc:
            raise HTTPException(409, str(exc)) from exc

    @app.post("/api/interventions/{iid}/heartbeat")
    def heartbeat(iid: str, op: Operator = Depends(operator)) -> dict[str, bool]:
        item_for(iid, op, "claim")
        store.heartbeat(iid, op.principal)
        return {"ok": True}

    @app.post("/api/interventions/{iid}/decision")
    def decide(iid: str, body: DecisionBody, op: Operator = Depends(operator)) -> dict[str, Any]:
        item_for(iid, op, body.action)
        try:
            return public(store.decide(iid, op.principal, body.action, body.note[:500]))
        except InterventionError as exc:
            raise HTTPException(409, str(exc)) from exc

    return app


class ConsoleServer:
    """The console on a background thread, for the lifetime of a ``with`` block."""

    def __init__(
        self,
        store: InterventionStore,
        accounts: list[OperatorAccount],
        port: int = 8090,
        host: str = "127.0.0.1",
    ) -> None:
        self.url = f"http://{host}:{port}"
        config = uvicorn.Config(
            create_console(store, accounts), host=host, port=port, log_level="warning"
        )
        self.server = uvicorn.Server(config)
        self.thread = threading.Thread(target=self.server.run, daemon=True)

    def __enter__(self) -> ConsoleServer:
        self.thread.start()
        deadline = time.monotonic() + 10
        while not self.server.started:
            if time.monotonic() > deadline or not self.thread.is_alive():
                raise RuntimeError(f"operator console did not start on {self.url}")
            time.sleep(0.05)
        return self

    def __exit__(self, *exc: object) -> None:
        self.server.should_exit = True
        self.thread.join(timeout=5)
