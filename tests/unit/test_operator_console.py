"""Intervention store and operator console: who may claim, decide and see what."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from cua.core.authz import Operator
from cua.handoff.store import InterventionError, InterventionStore
from cua.operator.console import OperatorAccount, create_console, load_operators

ALEX = {"Authorization": "Bearer alex-token-0123456789"}
SAM = {"Authorization": "Bearer sam-token-0123456789x"}


def new(store: InterventionStore, tenant: str = "tenant-a", kind: Any = "confirmation") -> str:
    return store.create(
        kind=kind,
        run_id="replay_x",
        mode="replay",
        capability="open_sub_account@1.0.0",
        tenant=tenant,
        step="confirm",
        step_description="Confirm button",
        reason="confirm: irreversible action needs confirmation",
        expects_on_return="title contains Account Opened",
    ).id


@pytest.fixture
def store(tmp_path: Path) -> InterventionStore:
    return InterventionStore(tmp_path / "interventions")


@pytest.fixture
def client(store: InterventionStore) -> TestClient:
    accounts = [
        OperatorAccount(Operator("ops.alex", frozenset({"tenant-a"})), "alex-token-0123456789"),
        OperatorAccount(Operator("ops.sam", frozenset({"tenant-b"})), "sam-token-0123456789x"),
    ]
    return TestClient(create_console(store, accounts))


def test_store_lifecycle_is_persisted(store: InterventionStore, tmp_path: Path) -> None:
    iid = new(store)
    with pytest.raises(InterventionError, match="not claimed"):
        store.decide(iid, "ops.alex", "approve")
    store.claim(iid, "ops.alex")
    with pytest.raises(InterventionError, match="claimed, not open"):
        store.claim(iid, "ops.sam")
    with pytest.raises(InterventionError, match=r"not claimed by ops\.sam"):
        store.decide(iid, "ops.sam", "approve")
    with pytest.raises(InterventionError, match="not allowed"):
        store.decide(iid, "ops.alex", "resume")  # resume is for dialogs, not confirmations
    store.decide(iid, "ops.alex", "approve")
    store.close(iid, "resolved", "resumed after approve")
    saved = json.loads((tmp_path / "interventions" / f"{iid}.json").read_text())
    assert saved["status"] == "resolved" and saved["decision"]["by"] == "ops.alex"


def test_policy_pauses_cannot_be_approved(store: InterventionStore) -> None:
    iid = new(store, kind="dialog")
    assert store.get(iid).allowed == ("resume", "abort")


def test_console_requires_a_known_token(client: TestClient) -> None:
    assert client.get("/api/interventions").status_code == 401
    bad = {"Authorization": "Bearer nope"}
    assert client.get("/api/interventions", headers=bad).status_code == 401
    assert client.get("/api/me", headers=ALEX).json()["id"] == "ops.alex"
    assert "Operator console" in client.get("/").text


def test_operators_only_see_and_touch_their_tenants(
    client: TestClient, store: InterventionStore
) -> None:
    iid = new(store, "tenant-a")
    assert [i["id"] for i in client.get("/api/interventions", headers=ALEX).json()] == [iid]
    assert client.get("/api/interventions", headers=SAM).json() == []
    assert client.post(f"/api/interventions/{iid}/claim", headers=SAM).status_code == 403
    assert store.get(iid).status == "open"


def test_claim_then_decide_through_the_console(
    client: TestClient, store: InterventionStore
) -> None:
    iid = new(store)
    body = {"action": "approve"}
    r = client.post(f"/api/interventions/{iid}/decision", json=body, headers=ALEX)
    assert r.status_code == 409  # must claim first
    assert (
        client.post(f"/api/interventions/{iid}/claim", headers=ALEX).json()["status"] == "claimed"
    )
    assert client.post(f"/api/interventions/{iid}/claim", headers=ALEX).status_code == 409
    assert client.post(f"/api/interventions/{iid}/heartbeat", headers=ALEX).json() == {"ok": True}
    r = client.post(f"/api/interventions/{iid}/decision", json={"action": "resume"}, headers=ALEX)
    assert r.status_code == 409
    r = client.post(f"/api/interventions/{iid}/decision", json=body, headers=ALEX)
    assert r.json()["decision"]["action"] == "approve"
    assert "screenshot" not in r.json() and r.json()["has_screenshot"] is False
    assert store.get(iid).decision is not None


def test_operators_without_a_token_cannot_sign_in(tmp_path: Path) -> None:
    cfg = tmp_path / "operators.yaml"
    cfg.write_text(
        "operators:\n"
        "  - {id: ops.alex, tenants: [tenant-a], token_env: T_ALEX}\n"
        "  - {id: ops.sam, tenants: [tenant-b], token_env: T_SAM}\n"
    )
    accounts = load_operators(cfg, {"T_ALEX": "a-long-enough-token-1", "T_SAM": "short"})
    assert [a.operator.principal for a in accounts] == ["ops.alex"]
