"""Human takeover of a live replay session.

The "operator" here is a script called from inside the wait loop (``pump_hook``): it claims the
request through the store exactly as the console does, and uses the page *directly* (not through
the surface's gated ``act``), which is what a person clicking in the window amounts to.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest

from cua.core.lease import ControlLease, LeaseError
from cua.core.results import Failure, FailureCode, Success
from cua.core.targets import RoleStrategy, TargetSpec
from cua.handoff.session import Handoff
from cua.handoff.store import Intervention, InterventionStore

from .test_replay import (
    NEW_ACCOUNT,
    WRITER,
    accounts,
    evidence_dir,
    lookup,
    make_engine,  # noqa: F401  (fixture)
    open_account,
    run,
    tokens,  # noqa: F401  (fixture)
)

Script = Callable[[Any, Intervention, int], str | None]


class FakeOperator:
    """Claims the open request, then calls ``script(surface, item, visit)`` once per claim.
    The script returns the decision to submit (or None to stay silent)."""

    def __init__(self, store: InterventionStore, script: Script, name: str = "ops.alex") -> None:
        self.store, self.script, self.name = store, script, name
        self.visits = 0
        self.acted_for_claim = False

    def __call__(self, surface: Any, item: Intervention) -> None:
        if item.status == "open":
            self.store.claim(item.id, self.name)
            self.acted_for_claim = False
            return
        if item.status != "claimed" or item.decision is not None or self.acted_for_claim:
            return
        if surface.on_human_event is None:  # pragma: no cover - set before the loop starts
            return
        self.store.heartbeat(item.id, self.name)
        self.acted_for_claim = True
        self.visits += 1
        action = self.script(surface, item, self.visits)
        surface.pause(150)  # let the page report what the person did
        if action is not None:
            self.store.decide(item.id, self.name, action)


@pytest.fixture
def store(tmp_path: Path) -> InterventionStore:
    return InterventionStore(tmp_path / "interventions")


def events(result: Any, tmp_path: Path) -> list[dict[str, Any]]:
    path = evidence_dir(result, tmp_path) / "events.jsonl"
    return [json.loads(line) for line in path.read_text().splitlines()]


def kinds(result: Any, tmp_path: Path) -> list[str]:
    return [e["kind"] for e in events(result, tmp_path)]


def click_in_main(surface: Any, name: str) -> None:
    surface.frame(surface.content_frame).get_by_role("button", name=name).click()


# --- irreversible step: approve, or do it yourself -------------------------------------------------


def test_operator_approval_lets_automation_commit_once(
    make_engine: Any,  # noqa: F811
    servers: Any,
    store: InterventionStore,
    tmp_path: Path,
) -> None:
    op = FakeOperator(store, lambda _s, _i, _n: "approve")
    r = run(
        make_engine(handoff=Handoff(store, pump_hook=op)),
        open_account(),
        NEW_ACCOUNT,
        caller=WRITER,
    )
    assert isinstance(r, Success), r
    assert len(accounts(servers, "10042")) == 4
    seen = kinds(r, tmp_path)
    for kind in (
        "intervention_opened",
        "intervention_claimed",
        "operator_decision",
        "intervention_resolved",
        "confirmed",
    ):
        assert kind in seen, kind
    confirmed = next(e for e in events(r, tmp_path) if e["kind"] == "confirmed")
    assert confirmed["confirmed_by"] == "ops.alex"
    item = store.list()[0]
    assert (item.status, item.kind, item.step) == ("resolved", "confirmation", "confirm")
    assert Path(item.screenshot or "").is_file()


def test_operator_does_the_step_and_automation_continues_without_repeating_it(
    make_engine: Any,  # noqa: F811
    servers: Any,
    store: InterventionStore,
    tmp_path: Path,
) -> None:
    def script(surface: Any, _item: Intervention, _n: int) -> str:
        click_in_main(surface, "Confirm")
        return "step_done"

    op = FakeOperator(store, script)
    r = run(
        make_engine(handoff=Handoff(store, pump_hook=op)),
        open_account(),
        NEW_ACCOUNT,
        caller=WRITER,
    )
    assert isinstance(r, Success), r
    assert r.outputs["new_account_number"].startswith("0042-")
    assert len(accounts(servers, "10042")) == 4  # once: automation did not click Confirm again
    assert "step_done_by_human" in kinds(r, tmp_path)
    human = [e for e in events(r, tmp_path) if e["kind"] == "human_action"]
    assert [(h["actor"], h["event"], h["name"]) for h in human] == [
        ("human:ops.alex", "click", "Confirm")
    ]
    assert store.list()[0].human_actions[0]["name"] == "Confirm"


def test_hand_back_is_refused_until_the_page_is_right_then_abort(
    make_engine: Any,  # noqa: F811
    servers: Any,
    store: InterventionStore,
) -> None:
    # The operator says "I did it" without doing it; the second time they abort.
    op = FakeOperator(store, lambda _s, _i, n: "step_done" if n == 1 else "abort")
    r = run(
        make_engine(handoff=Handoff(store, pump_hook=op)),
        open_account(),
        NEW_ACCOUNT,
        caller=WRITER,
    )
    assert isinstance(r, Failure), r
    assert (r.code, r.at_step, r.retryable) == (FailureCode.OPERATOR_ABORTED, "confirm", False)
    assert len(accounts(servers, "10042")) == 3
    item = store.list()[0]
    assert item.status == "aborted"
    assert any("resume check failed" in n and "Account Opened" in n for n in item.notes)


def test_unclaimed_request_times_out(
    make_engine: Any,  # noqa: F811
    servers: Any,
    store: InterventionStore,
) -> None:
    r = run(
        make_engine(handoff=Handoff(store, claim_sla_s=0.5)),
        open_account(),
        NEW_ACCOUNT,
        caller=WRITER,
    )
    assert isinstance(r, Failure) and r.code == FailureCode.OPERATOR_TIMEOUT
    assert store.list()[0].status == "timed_out"
    assert len(accounts(servers, "10042")) == 3


def test_lost_operator_is_released_and_another_can_claim(
    make_engine: Any,  # noqa: F811
    store: InterventionStore,
) -> None:
    state = {"claims": 0}

    def hook(_surface: Any, item: Intervention) -> None:
        if item.status == "open":
            state["claims"] += 1
            store.claim(item.id, "ops.gone" if state["claims"] == 1 else "ops.alex")
        elif item.claimed_by == "ops.alex" and item.decision is None:
            store.decide(item.id, "ops.alex", "approve")
        # ops.gone never sends a heartbeat or a decision

    handoff = Handoff(store, pump_hook=hook, heartbeat_timeout_s=0.5)
    r = run(make_engine(handoff=handoff), open_account(), NEW_ACCOUNT, caller=WRITER)
    assert isinstance(r, Success), r
    notes = " ".join(store.list()[0].notes)
    assert "claimed by ops.gone" in notes and "heartbeat lost" in notes
    assert "claimed by ops.alex" in notes


# --- an unexpected prompt: fix it and hand back ----------------------------------------------------


def test_operator_clears_a_security_prompt_and_the_run_finishes(
    make_engine: Any,  # noqa: F811
    store: InterventionStore,
    tmp_path: Path,
) -> None:
    def script(surface: Any, item: Intervention, n: int) -> str:
        assert "resume" in item.allowed and "approve" not in item.allowed
        if n == 2:  # first hand-back is refused: the dialog is still there
            click_in_main(surface, "Remind me later")
        return "resume"

    op = FakeOperator(store, script)
    r = run(
        make_engine(faults="surprise_dialog", handoff=Handoff(store, pump_hook=op)),
        lookup(),
        "10077",
    )
    assert isinstance(r, Success), r
    assert r.outputs["member_name"] == "James Okafor"
    seen = kinds(r, tmp_path)
    assert seen.count("intervention_reopened") == 1 and "intervention_resolved" in seen
    assert store.list()[0].human_actions[0]["name"] == "Remind me later"


def test_actions_before_the_claim_are_recorded_as_unclaimed(
    make_engine: Any,  # noqa: F811
    store: InterventionStore,
    tmp_path: Path,
) -> None:
    # Found by a real run: the browser comes to the front, so a person naturally clicks there
    # first and claims in the console afterwards. That click must not go unrecorded.
    state = {"n": 0}

    def hook(surface: Any, item: Intervention) -> None:
        state["n"] += 1
        if state["n"] == 1:
            click_in_main(surface, "Remind me later")
            surface.pause(150)
        elif item.status == "open":
            store.claim(item.id, "ops.alex")
        elif item.decision is None:
            store.decide(item.id, "ops.alex", "resume")

    r = run(
        make_engine(faults="surprise_dialog", handoff=Handoff(store, pump_hook=hook)),
        lookup(),
        "10077",
    )
    assert isinstance(r, Success), r
    (action,) = store.list()[0].human_actions
    assert (action["actor"], action["name"]) == ("human:unclaimed", "Remind me later")
    assert "human_action" in kinds(r, tmp_path)


# --- the lease on the surface ----------------------------------------------------------------------


def test_automation_cannot_act_while_a_person_holds_the_session(open_surface: Any) -> None:
    surface = open_surface()
    lease = ControlLease()
    token = lease.token()
    surface.gate = lambda: lease.check(token)
    button = TargetSpec(
        description="Sign In",
        frame=surface.content_frame,
        strategies=(RoleStrategy(role="button", name="Sign In"),),
    )
    lease.request_human("test")
    lease.claim("ops.alex")
    with pytest.raises(LeaseError, match="human"):
        surface.act(surface.resolve(button), "click")
    with pytest.raises(LeaseError):
        surface.navigate("/search", surface.content_frame)
    lease.begin_resume()
    lease.resumed()
    with pytest.raises(LeaseError, match="stale"):  # the pre-handoff token is dead
        surface.act(surface.resolve(button), "click")


def test_sensitive_values_typed_by_a_person_are_not_recorded(open_surface: Any) -> None:
    surface = open_surface()
    seen: list[dict[str, Any]] = []
    surface.on_human_event = seen.append
    frame = surface.frame(surface.content_frame)
    frame.locator("input[type=password]").fill("hunter2-not-a-real-one")
    frame.locator("input[type=password]").blur()
    frame.locator("input[type=text]").first.fill("someone")
    frame.locator("input[type=text]").first.blur()
    surface.pause(200)
    values = {e.get("value") for e in seen if e["event"] == "change"}
    assert "[not recorded]" in values and "hunter2-not-a-real-one" not in values
