"""The handoff: pause automation at a step boundary and let a person drive the same session.

``Handoff.request`` is called by an engine (replay or discovery) when it must not continue on
its own. It:

1. moves the lease to ``pending_human`` (automation can no longer act on the surface),
2. writes an intervention request with a redacted screenshot and context,
3. waits while pumping the browser, so the operator can use the very same window:
   claim -> lease ``human`` (their clicks and edits are recorded) -> decision,
4. returns the decision. The *engine* then re-verifies the page before the lease returns to
   automation (``verify_and_resume``); if the check fails the request is reopened.

Timeouts: an unclaimed request is aborted after ``claim_sla_s``; a claimed one whose operator
stops sending heartbeats goes back to open so someone else can take it.
"""

from __future__ import annotations

import contextlib
import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from cua.core.lease import ControlLease, LeaseError
from cua.evidence.store import RunEvidence

from .store import Intervention, InterventionStore, Kind


@dataclass
class Decision:
    action: str  # approve | step_done | resume | abort | timeout
    operator: str | None
    intervention: Intervention
    human_actions: list[dict[str, Any]]


class Handoff:
    def __init__(
        self,
        store: InterventionStore,
        *,
        claim_sla_s: float = 600.0,
        heartbeat_timeout_s: float = 20.0,
        max_reopens: int = 2,
        pump_hook: Callable[[Any, Intervention], None] | None = None,
        announce: Callable[[str], None] | None = None,
    ) -> None:
        self.store = store
        self.claim_sla_s = claim_sla_s
        self.heartbeat_timeout_s = heartbeat_timeout_s
        self.max_reopens = max_reopens
        self.pump_hook = pump_hook  # tests: act as the human inside the browser thread
        self.announce = announce or (lambda _msg: None)

    def request(
        self,
        *,
        surface: Any,
        lease: ControlLease,
        evidence: RunEvidence,
        kind: Kind,
        run_id: str,
        mode: str,
        capability: str,
        tenant: str,
        step: str | None,
        step_description: str,
        reason: str,
        expects_on_return: str,
        recent_events: list[str] | None = None,
        allowed: tuple[str, ...] | None = None,
        existing: Intervention | None = None,
    ) -> Decision:
        if lease.holder == "automation":
            lease.request_human(reason)
        if existing is None:
            shot = evidence.screenshot(surface, f"handoff-{kind}")
            item = self.store.create(
                kind=kind,
                run_id=run_id,
                mode=mode,
                capability=capability,
                tenant=tenant,
                step=step,
                step_description=step_description,
                reason=reason,
                expects_on_return=expects_on_return,
                screenshot=str(shot),
                recent_events=recent_events,
                allowed=allowed,
            )
            evidence.event(
                "intervention_opened", id=item.id, intervention_kind=kind, step=step, reason=reason
            )
            self.announce(f"waiting for an operator: {reason} (intervention {item.id})")
        else:
            item = existing
        surface.bring_to_front()

        captured: list[dict[str, Any]] = []

        def on_event(ev: dict[str, Any]) -> None:
            # Automation's own clicks also raise DOM events; they are not recorded here because
            # this callback is only installed while automation is paused. A person may act in
            # the window *before* claiming in the console: that is recorded too, as unclaimed.
            if lease.holder not in ("pending_human", "human"):
                return
            who = lease.operator if lease.holder == "human" else "unclaimed"
            record = {"actor": f"human:{who}", **ev}
            captured.append(record)
            self.store.add_human_action(item.id, evidence.redactor.redact(record))
            evidence.event("human_action", intervention=item.id, **record)

        surface.on_human_event = on_event
        try:
            while True:
                surface.pause(200)
                if self.pump_hook is not None:
                    self.pump_hook(surface, item)
                now = time.monotonic()
                if item.status == "open":
                    if lease.holder == "human":  # was claimed, then reopened elsewhere
                        lease.release()
                    if now - item.created_monotonic > self.claim_sla_s:
                        self.store.close(item.id, "timed_out", "no operator claimed it in time")
                        lease.abort("operator timeout")
                        evidence.event("intervention_timed_out", id=item.id)
                        return Decision("timeout", None, item, captured)
                    continue
                if item.status == "claimed":
                    if lease.holder == "pending_human":
                        lease.claim(item.claimed_by or "operator")
                        evidence.event("intervention_claimed", id=item.id, operator=item.claimed_by)
                    if item.decision is None:
                        if now - item.last_heartbeat > self.heartbeat_timeout_s:
                            lease.release()
                            self.store.reopen(item.id, "operator heartbeat lost; released")
                            evidence.event("intervention_released", id=item.id)
                        continue
                    action = item.decision["action"]
                    operator = item.decision["by"]
                    evidence.event(
                        "operator_decision", id=item.id, action=action, operator=operator
                    )
                    if action == "abort":
                        self.store.close(item.id, "aborted", f"aborted by {operator}")
                        lease.abort(f"aborted by {operator}")
                    else:
                        lease.begin_resume()
                    return Decision(action, operator, item, captured)
        finally:
            surface.on_human_event = None

    def verify_and_resume(
        self,
        decision: Decision,
        lease: ControlLease,
        evidence: RunEvidence,
        check: Callable[[], str | None],
    ) -> bool:
        """Re-verify the page before automation continues. ``check`` returns a problem or None.

        True: lease is back with automation. False: the request was reopened (call ``request``
        again with ``existing=decision.intervention``) unless the reopen budget is spent.
        """
        problem = check()
        item = decision.intervention
        if problem is None:
            lease.resumed()
            self.store.close(item.id, "resolved", f"resumed after {decision.action}")
            evidence.event("intervention_resolved", id=item.id, action=decision.action)
            return True
        reopens = sum("resume check failed" in n for n in item.notes)
        if reopens >= self.max_reopens:
            self.store.close(
                item.id, "aborted", f"resume check failed {reopens + 1} times: {problem}"
            )
            with contextlib.suppress(LeaseError):
                lease.abort("resume checks kept failing")
            return False
        lease.resume_failed(problem)
        self.store.reopen(item.id, f"resume check failed: {problem}")
        evidence.event("intervention_reopened", id=item.id, problem=problem)
        self.announce(f"hand-back refused: {problem}; intervention {item.id} is open again")
        return False
