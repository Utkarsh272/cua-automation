"""The replay engine: run an approved capability with typed inputs, no model in the loop.

Order of checks (cheapest and safest first, all before a browser opens):

1. **Inputs** against the JSON Schema -> ``BusinessOutcome INVALID_INPUT``
2. **Authorization** (tenant, scope, approval, subject) -> ``Failure NOT_AUTHORIZED``
3. **Compatibility** with the tenant product version -> ``Failure ARTIFACT_INVALID``

Then sign on with the shared ``login`` capability and run each step:

* **Before each step and while waiting** the current page is checked against every detector
  (app profile + capability), with fixed precedence:
  ``fail`` > ``escalate`` > ``outcome`` > ``recover`` > the step's own condition.
  A "No member found" banner therefore wins over a timeout, and a notice dialog is dismissed
  before the step that it would block. A dialog no detector explains -> ``NeedsHuman``.
* **Policy** is checked for every action with the control's real name and the page it is on.
  Irreversible actions need a confirmation token bound to this run, step and inputs; without one
  the run stops with ``NeedsHuman`` *before* acting.
* **Waits** are conditions, never sleeps. A slow page gets one extended wait (recorded as a
  recovery); after that it is ``TIMEOUT``, or ``UNEXPECTED_STATE`` if the page shows a message.
* **Session expiry** triggers a re-login and a restart from the first step, but only if every
  step run so far was safe to repeat. Otherwise the run fails rather than repeating a write.
* **Side effects** are reported on every failure: ``committed`` after an irreversible step,
  ``possible`` if a write was attempted, else ``none``. Anything but ``none`` is never retryable.

**With a handoff configured** (``handoff=``), the three places that would return ``NeedsHuman``
(an irreversible step without a token, an ``escalate`` detector, an unexplained dialog) instead
pause the *live session* and wait for an operator. The control lease guarantees automation
cannot act while they hold it. Before automation continues, the page is re-verified; a failed
check reopens the request. Abort and timeout end the run as ``OPERATOR_ABORTED`` /
``OPERATOR_TIMEOUT``. Without a handoff, behaviour is unchanged: ``NeedsHuman`` and a closed
session.

Evidence goes to ``<evidence_root>/<run_id>/`` through the redactor: events, result, and on
failure a masked screenshot and the page as text.
"""

from __future__ import annotations

import json
import secrets as pysecrets
import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime
from functools import partial
from pathlib import Path
from typing import Any
from urllib.parse import urljoin

from playwright.sync_api import Browser
from playwright.sync_api import Error as PlaywrightError

from cua.core.artifact import (
    ActionKind,
    Capability,
    Detector,
    EscalateThen,
    FailThen,
    OutcomeThen,
    RecoverThen,
    RiskClass,
    Step,
    validate_inputs,
    validate_outputs,
)
from cua.core.authz import Caller, SubjectOwnership, authorize_invocation
from cua.core.conditions import Condition, PageState, evaluate
from cua.core.confirmation import Confirmation
from cua.core.lease import ControlLease, LeaseToken
from cua.core.overrides import OverrideError, effective_capability
from cua.core.parse import ParseError, parse_value
from cua.core.policy import PolicyGuard, ProposedAction
from cua.core.redact import Redactor
from cua.core.results import (
    BusinessOutcome,
    FailureCode,
    NeedsHuman,
    RecoveryEvent,
    ReplayResult,
    SideEffects,
    Success,
    make_failure,
    result_json,
)
from cua.core.semver import parse_requirement, satisfies
from cua.core.templating import TemplateError, render
from cua.evidence.store import RunEvidence, new_run_id
from cua.handoff.session import Decision, Handoff
from cua.handoff.store import Kind
from cua.runtime.steps import describe, fired_detectors, summarize
from cua.surface.base import ResolutionError
from cua.surface.web import NavigationBlocked, WebSurface

POLL_MS = 150
_PRECEDENCE = (FailThen, EscalateThen, OutcomeThen, RecoverThen)


def choose_detector(fired: frozenset[str], detectors: Sequence[Detector]) -> Detector | None:
    """The detector that decides what happens next, by fixed precedence."""
    hits = [d for d in detectors if d.id in fired]
    for kind in _PRECEDENCE:
        for d in hits:
            if isinstance(d.then, kind):
                return d
    return None


class _Stop(Exception):
    """Ends the run with a result (built by the engine from these fields)."""

    def __init__(self, kind: str, **fields: Any) -> None:
        super().__init__(kind)
        self.kind = kind
        self.fields = fields


class _Relogin(Exception):
    pass


@dataclass
class _Run:
    run_id: str
    cap: Capability
    inputs: dict[str, Any]
    scopes: frozenset[str]
    confirmations: Mapping[str, str]
    outputs: dict[str, Any] = field(default_factory=dict)
    recoveries: list[RecoveryEvent] = field(default_factory=list)
    executed: list[Step] = field(default_factory=list)
    counts: dict[str, int] = field(default_factory=dict)
    degraded: bool = False
    step_id: str | None = None
    lease: ControlLease = field(default_factory=ControlLease)
    token: LeaseToken = field(default_factory=lambda: LeaseToken(1))
    ev: RunEvidence | None = None
    human_clicks: int = 0
    resumed: bool = False
    human_steps: list[str] = field(default_factory=list)

    def side_effects(self, current: Step | None = None) -> SideEffects:
        if any(s.risk == RiskClass.IRREVERSIBLE for s in self.executed):
            return "committed"
        writes = [s for s in self.executed if s.risk != RiskClass.READ]
        if writes or (current is not None and current.risk != RiskClass.READ):
            return "possible"
        if self.human_clicks:  # a person clicked in the session; we cannot rule out a change
            return "possible"
        return "none"


class ReplayEngine:
    def __init__(
        self,
        *,
        browser: Browser,
        ctx: Any,  # cua.config.TenantContext (or a test double with the same fields)
        secrets: Callable[[str, str], str],
        evidence_root: str | Path,
        guard: PolicyGuard,
        base_url: str | None = None,
        interventions_dir: str | Path = "interventions",
        extra_headers: dict[str, str] | None = None,
        max_wait_ms: int | None = None,
        settle_ms: int = 5000,
        handoff: Handoff | None = None,
    ) -> None:
        self.handoff = handoff
        self.browser = browser
        self.ctx = ctx
        self.secrets = secrets
        self.evidence_root = Path(evidence_root)
        self.guard = guard
        self.base_url = base_url or ctx.tenant.base_url
        self.interventions_dir = Path(interventions_dir)
        self.extra_headers = extra_headers
        self.max_wait_ms = max_wait_ms
        self.settle_ms = settle_ms

    # --- public -------------------------------------------------------------------------------

    def run(
        self,
        cap: Capability,
        inputs: dict[str, Any],
        *,
        caller: Caller,
        ownership: SubjectOwnership,
        attended: bool = False,
        confirmations: Mapping[str, str] | None = None,
        run_id: str | None = None,
    ) -> ReplayResult:
        started = datetime.now(UTC)
        t0 = time.monotonic()
        run = _Run(
            run_id=run_id or new_run_id("replay"),
            cap=cap,
            inputs=dict(inputs),
            scopes=caller.scopes,
            confirmations=dict(confirmations or {}),
        )

        def meta() -> dict[str, Any]:
            return {
                "run_id": run.run_id,
                "capability": cap.id,
                "version": cap.version,
                "tenant": self.ctx.tenant.id,
                "started_at": started,
                "duration_ms": int((time.monotonic() - t0) * 1000),
            }

        evidence = RunEvidence(self.evidence_root, run.run_id, self._redactor(cap, inputs))
        run.ev = evidence
        evidence.event(
            "replay_started",
            capability=cap.ref,
            status=cap.status,
            tenant=self.ctx.tenant.id,
            caller=caller.principal,
            on_behalf_of=caller.on_behalf_of,
            attended=attended,
            inputs=inputs,
        )

        pre = self._preflight(cap, inputs, caller, ownership, attended, meta)
        if pre is not None:
            return self._finish(evidence, pre)

        try:
            cap, applied = effective_capability(cap, self.ctx.tenant.id)
        except OverrideError as exc:
            return self._finish(
                evidence,
                make_failure(
                    code=FailureCode.ARTIFACT_INVALID,
                    at_step=None,
                    expected="valid tenant overrides",
                    observed=str(exc),
                    **meta(),
                ),
            )
        if applied:
            run.cap = cap
            evidence.event("overrides_applied", tenant=self.ctx.tenant.id, count=applied)

        surface = WebSurface.open(
            self.browser,
            self.base_url,
            extra_headers=self.extra_headers,
            content_frame=self.ctx.profile.content_frame,
            url_guard=lambda u: self.guard.check_url(u).allowed,
            settle_ms=self.settle_ms,
        )
        surface.gate = lambda: run.lease.check(run.token)
        result: ReplayResult
        try:
            try:
                surface.start("/")
                self._login(surface, run, evidence)
                self._steps_with_relogin(surface, run, evidence)
                result = self._success(surface, run, meta, evidence)
            except _Stop as stop:
                result = self._stopped(stop, surface, run, meta, evidence)
            except PlaywrightError as exc:
                result = make_failure(
                    code=FailureCode.INTERNAL_ERROR,
                    at_step=run.step_id,
                    expected="the browser to respond",
                    observed=str(exc).splitlines()[0][:300],
                    side_effects=run.side_effects(),
                    recoveries=tuple(run.recoveries),
                    **meta(),
                )
        finally:
            surface.close()
        return self._finish(evidence, result)

    # --- before the browser -------------------------------------------------------------------

    def _preflight(
        self,
        cap: Capability,
        inputs: dict[str, Any],
        caller: Caller,
        ownership: SubjectOwnership,
        attended: bool,
        meta: Callable[[], dict[str, Any]],
    ) -> ReplayResult | None:
        errors = validate_inputs(cap, inputs)
        if errors:
            return BusinessOutcome(code="INVALID_INPUT", detail="; ".join(errors), **meta())
        decision = authorize_invocation(
            cap,
            tenant=self.ctx.tenant.id,
            inputs=inputs,
            caller=caller,
            ownership=ownership,
            attended=attended,
        )
        if not decision.allowed:
            code = (
                FailureCode.NOT_APPROVED
                if "draft" in decision.reason
                else FailureCode.NOT_AUTHORIZED
            )
            return make_failure(
                code=code,
                at_step=None,
                expected="an authorized invocation",
                observed=decision.reason,
                **meta(),
            )
        tenant = self.ctx.tenant
        if cap.app.product != tenant.product or not satisfies(
            tenant.product_version, cap.app.versions
        ):
            return make_failure(
                code=FailureCode.ARTIFACT_INVALID,
                at_step=None,
                expected=f"{cap.app.product} {cap.app.versions}",
                observed=f"tenant {tenant.id} runs {tenant.product} {tenant.product_version}",
                **meta(),
            )
        for req in cap.requires:
            req_id, constraint = parse_requirement(req)
            login = self.ctx.login
            if req_id != login.id or not satisfies(login.version, constraint):
                return make_failure(
                    code=FailureCode.ARTIFACT_INVALID,
                    at_step=None,
                    expected=req,
                    observed=f"available: {login.ref}",
                    **meta(),
                )
        return None

    def _redactor(self, cap: Capability, inputs: dict[str, Any]) -> Redactor:
        prefix = self.ctx.tenant.secrets_prefix
        secret_values = []
        for key in ("username", "password"):
            try:
                secret_values.append(self.secrets(prefix, key))
            except (KeyError, TemplateError):
                continue
        return Redactor.for_run(
            cap.sensitivities(),
            inputs,
            secrets=secret_values,
            extra_patterns=self.ctx.profile.redaction_patterns,
        )

    # --- login and the step loop --------------------------------------------------------------

    def _login_detectors(self) -> tuple[Detector, ...]:
        profile = tuple(
            d
            for d in self.ctx.profile.detectors
            if not (isinstance(d.then, RecoverThen) and d.then.recover == "relogin")
        )
        return (*self.ctx.login.detectors, *profile)

    def _login(self, surface: WebSurface, run: _Run, ev: RunEvidence) -> None:
        login = self.ctx.login
        detectors = self._login_detectors()
        for step in login.steps:
            self._step(surface, run, step, detectors, ev, inputs={})
        state = self._snapshot(surface, detectors, run)
        if not evaluate(login.success, state):
            raise _Stop(
                "failure",
                code=FailureCode.LOGIN_FAILED,
                expected=describe(login.success),
                observed=summarize(state),
            )
        ev.event("login_ok", login=login.ref)

    def _steps_with_relogin(self, surface: WebSurface, run: _Run, ev: RunEvidence) -> None:
        detectors = (*self.ctx.profile.detectors, *run.cap.detectors)
        while True:
            try:
                for step in run.cap.steps:
                    self._step(surface, run, step, detectors, ev, inputs=run.inputs)
                    run.executed.append(step)
                return
            except _Relogin:
                unsafe = [s.id for s in run.executed if not s.repeatable]
                if unsafe:
                    raise _Stop(
                        "failure",
                        code=FailureCode.SESSION_LOST,
                        expected="session to stay valid",
                        observed=f"session expired after non-repeatable step(s) {unsafe}",
                    ) from None
                ev.event("relogin", after_step=run.step_id)
                self._login(surface, run, ev)
                run.outputs.clear()
                run.executed.clear()

    def _step(
        self,
        surface: WebSurface,
        run: _Run,
        step: Step,
        detectors: Sequence[Detector],
        ev: RunEvidence,
        *,
        inputs: dict[str, Any],
    ) -> None:
        run.step_id = step.id
        self._await(surface, run, step, detectors, None, 0)  # page must be clean before acting

        try:
            if step.action == ActionKind.NAVIGATE:
                assert step.route is not None
                route = render(step.route, inputs, self.secrets)
                url = urljoin(surface.base_url, route.lstrip("/"))
                self._authorize(
                    run,
                    step,
                    ProposedAction(
                        ActionKind.NAVIGATE, url, declared_risk=step.risk, step_id=step.id
                    ),
                )
                try:
                    surface.navigate(route, step.frame)
                except NavigationBlocked as exc:
                    raise _Stop(
                        "failure",
                        code=FailureCode.POLICY_VIOLATION,
                        expected="an allowlisted route",
                        observed=str(exc),
                    ) from exc
                raw = None
            else:
                assert step.target is not None
                try:
                    res = surface.resolve(step.target)
                except ResolutionError as exc:
                    # A business page may have replaced the expected one: let detectors speak first.
                    self._await(surface, run, step, detectors, None, 0)
                    raise _Stop(
                        "failure",
                        code=exc.code,
                        expected=step.target.description,
                        observed=str(exc),
                    ) from exc
                if res.degraded and step.id not in run.human_steps:
                    run.degraded = True
                    ev.event(
                        "drift",
                        step=step.id,
                        strategy=res.strategy.by,
                        attempts=[a.__dict__ for a in res.attempts],
                    )
                by_human = self._authorize(
                    run,
                    step,
                    ProposedAction(
                        step.action,
                        surface.content_url,
                        declared_risk=step.risk,
                        control_name=surface.control_name(res),
                        step_id=step.id,
                    ),
                    surface,
                    detectors,
                )
                raw = None
                if by_human:
                    run.human_steps.append(step.id)
                    ev.event("step_done_by_human", step=step.id)
                elif step.action == ActionKind.EXTRACT:
                    raw = surface.read(res)
                elif step.action == ActionKind.FILL:
                    surface.act(res, "fill", render(step.value or "", inputs, self.secrets))
                elif step.action == ActionKind.SELECT:
                    surface.act(res, "select", render(step.option or "", inputs, self.secrets))
                elif step.action == ActionKind.PRESS:
                    surface.act(res, "press", step.key)
                elif step.action == ActionKind.CHECK:
                    surface.act(res, "check")
                else:
                    surface.act(res, "click")
        except TemplateError as exc:
            raise _Stop(
                "failure",
                code=FailureCode.ARTIFACT_INVALID,
                expected="renderable values",
                observed=str(exc),
            ) from exc

        cond = step.wait.until if step.wait else step.postcondition
        timeout = step.wait.timeout_ms if step.wait else 8000
        self._await(surface, run, step, detectors, cond, timeout)
        if step.wait is not None and step.postcondition is not None:
            self._await(surface, run, step, detectors, step.postcondition, timeout)

        if raw is not None and step.output:
            try:
                run.outputs[step.output] = parse_value(step.parse, raw)
            except ParseError as exc:
                raise _Stop(
                    "failure",
                    code=FailureCode.OUTPUT_INVALID,
                    expected=f"{step.output} as {step.parse}",
                    observed=str(exc),
                ) from exc
        ev.event("step_ok", step=step.id, action=step.action.value)

    def _authorize(
        self,
        run: _Run,
        step: Step,
        action: ProposedAction,
        surface: WebSurface | None = None,
        detectors: Sequence[Detector] = (),
    ) -> bool:
        """Policy check for one action. True means a person already performed the step."""
        conf = None
        if step.id in run.confirmations:
            conf = Confirmation(run.confirmations[step.id], run.run_id, step.id, run.inputs)
        decision = self.guard.evaluate(action, scopes=run.scopes, confirmation=conf)
        if decision.verdict == "deny":
            raise _Stop(
                "failure",
                code=FailureCode.POLICY_VIOLATION,
                expected=f"{step.id} allowed by policy",
                observed=decision.reason,
            )
        if decision.verdict == "allow":
            return False
        reason = f"{step.id}: {decision.reason}"
        if self.handoff is None or surface is None:
            raise _Stop("needs_human", reason=reason)

        cond = step.wait.until if step.wait else step.postcondition
        assert step.target is not None
        target = step.target

        def check(d: Decision) -> str | None:
            if d.action == "step_done":
                if cond is None:
                    return None
                state = self._snapshot(surface, detectors, run)
                return (
                    None
                    if evaluate(cond, state)
                    else f"expected {describe(cond)}, but the page shows {summarize(state)}"
                )
            try:  # approve: automation will act, so the control must still be there
                surface.resolve(target)
            except ResolutionError:
                return f"{target.description} is no longer on the page"
            return None

        d = self._handoff(
            surface,
            run,
            step,
            kind="confirmation",
            reason=reason,
            expects=(
                f"Approve: the page is unchanged and automation performs '{step.id}'. "
                f"Did it yourself: {describe(cond) if cond else 'the step is complete'}."
            ),
            check=check,
        )
        if d.action == "step_done":
            return True
        tokens = self.guard.tokens
        if tokens is None:
            raise _Stop(
                "failure",
                code=FailureCode.POLICY_VIOLATION,
                expected="a confirmation service",
                observed="no confirmation secret is configured",
            )
        token = tokens.issue(
            run_id=run.run_id, step_id=step.id, inputs=run.inputs, issued_to=d.operator or "?"
        )
        again = self.guard.evaluate(
            action,
            scopes=run.scopes,
            confirmation=Confirmation(token, run.run_id, step.id, run.inputs),
        )
        if again.verdict != "allow":
            raise _Stop(
                "failure",
                code=FailureCode.POLICY_VIOLATION,
                expected=f"{step.id} allowed after approval",
                observed=again.reason,
            )
        assert run.ev is not None
        run.ev.event("confirmed", step=step.id, confirmed_by=again.confirmed_by)
        return False

    def _handoff(
        self,
        surface: WebSurface,
        run: _Run,
        step: Step,
        *,
        kind: Kind,
        reason: str,
        expects: str,
        check: Callable[[Decision], str | None],
    ) -> Decision:
        """Pause for an operator until the page passes ``check``; raise ``_Stop`` otherwise."""
        assert self.handoff is not None and run.ev is not None
        existing = None
        while True:
            d = self.handoff.request(
                surface=surface,
                lease=run.lease,
                evidence=run.ev,
                kind=kind,
                run_id=run.run_id,
                mode="replay",
                capability=run.cap.ref,
                tenant=self.ctx.tenant.id,
                step=step.id,
                step_description=step.target.description if step.target else step.action.value,
                reason=reason,
                expects_on_return=expects,
                recent_events=[s.id for s in run.executed[-5:]],
                existing=existing,
            )
            run.human_clicks += sum(a.get("event") == "click" for a in d.human_actions)
            if d.action in ("abort", "timeout"):
                code = (
                    FailureCode.OPERATOR_ABORTED
                    if d.action == "abort"
                    else FailureCode.OPERATOR_TIMEOUT
                )
                raise _Stop(
                    "failure",
                    code=code,
                    expected="an operator to resolve the pause",
                    observed=f"{reason} ({d.intervention.id}: {d.action})",
                    side_effects=run.side_effects(step),
                )
            if self.handoff.verify_and_resume(d, run.lease, run.ev, partial(check, d)):
                run.token = run.lease.token()
                run.resumed = True
                return d
            if run.lease.holder == "aborted":
                raise _Stop(
                    "failure",
                    code=FailureCode.OPERATOR_ABORTED,
                    expected="the page to be ready for automation",
                    observed=f"hand-back checks kept failing ({d.intervention.id})",
                    side_effects=run.side_effects(step),
                )
            existing = d.intervention

    # --- waiting and detectors ----------------------------------------------------------------

    def _snapshot(self, surface: WebSurface, detectors: Sequence[Detector], run: _Run) -> PageState:
        state = surface.page_state(run.outputs)
        return replace(state, fired_detectors=fired_detectors(state, detectors))

    def _await(
        self,
        surface: WebSurface,
        run: _Run,
        step: Step,
        detectors: Sequence[Detector],
        cond: Condition | None,
        timeout_ms: int,
    ) -> PageState:
        if self.max_wait_ms is not None:
            timeout_ms = min(timeout_ms, self.max_wait_ms)
        deadline = time.monotonic() + timeout_ms / 1000
        extended = False
        while True:
            state = self._snapshot(surface, detectors, run)
            if self._react(surface, run, step, detectors, state):
                if run.resumed:  # an operator took time; the step gets a fresh window
                    run.resumed = False
                    deadline = time.monotonic() + timeout_ms / 1000
                continue
            if cond is None or evaluate(cond, state):
                return state
            if time.monotonic() >= deadline:
                if not extended:
                    extended = True
                    deadline = time.monotonic() + timeout_ms / 1000
                    run.recoveries.append(
                        RecoveryEvent(
                            detector="slow_page", action="extended_wait", at_step=step.id, attempt=1
                        )
                    )
                    continue
                alerts = surface.alerts()
                observed = summarize(state) + (
                    f"; page says: {' | '.join(alerts)}" if alerts else ""
                )
                raise _Stop(
                    "failure",
                    code=FailureCode.UNEXPECTED_STATE if alerts else FailureCode.TIMEOUT,
                    expected=describe(cond),
                    observed=observed,
                    side_effects=run.side_effects(step),
                )
            surface.pause(POLL_MS)

    def _react(
        self,
        surface: WebSurface,
        run: _Run,
        step: Step,
        detectors: Sequence[Detector],
        state: PageState,
    ) -> bool:
        """Act on what the page shows. True if a recovery was performed (poll again)."""
        d = choose_detector(state.fired_detectors, detectors)
        if d is None:
            if state.dialog_titles:
                reason = f"unexpected dialog {state.dialog_titles[0]!r}"
                if self.handoff is None:
                    raise _Stop("needs_human", reason=reason)
                self._handoff(
                    surface,
                    run,
                    step,
                    kind="dialog",
                    reason=reason,
                    expects="No dialog is open.",
                    check=lambda _d: self._still_blocked(surface, run, detectors, None),
                )
                return True
            return False
        then = d.then
        if isinstance(then, FailThen):
            alerts = surface.alerts()
            raise _Stop(
                "failure",
                code=then.fail,
                expected=f"no '{d.id}'",
                observed=f"detector {d.id} matched; {summarize(state)}"
                + (f"; page says: {' | '.join(alerts)}" if alerts else ""),
                side_effects=run.side_effects(step),
            )
        if isinstance(then, EscalateThen):
            reason = f"{d.id}: {then.escalate}"
            det_id = d.id
            if self.handoff is None:
                raise _Stop("needs_human", reason=reason)
            self._handoff(
                surface,
                run,
                step,
                kind="stuck",
                reason=reason,
                expects=f"The page no longer shows '{det_id}'.",
                check=lambda _d: self._still_blocked(surface, run, detectors, det_id),
            )
            return True
        if isinstance(then, OutcomeThen):
            declared = {o.code: o.description for o in run.cap.outcomes}
            alerts = surface.alerts()
            detail = declared.get(then.outcome, d.id)
            if alerts:
                detail += f" (page says: {' | '.join(alerts)})"
            raise _Stop("outcome", code=then.outcome, detail=detail)
        assert isinstance(then, RecoverThen)
        n = run.counts.get(d.id, 0) + 1
        run.counts[d.id] = n
        if n > then.max:
            raise _Stop(
                "failure",
                code=FailureCode.UNEXPECTED_STATE,
                expected=f"'{d.id}' to clear",
                observed=f"still present after {then.max} recoveries",
                side_effects=run.side_effects(step),
            )
        run.recoveries.append(
            RecoveryEvent(detector=d.id, action=then.recover, at_step=step.id, attempt=n)
        )
        if then.recover == "relogin":
            raise _Relogin()
        assert then.target is not None
        try:
            surface.act(surface.resolve(then.target), "click")
        except ResolutionError as exc:
            raise _Stop(
                "failure",
                code=exc.code,
                expected=then.target.description,
                observed=str(exc),
                side_effects=run.side_effects(step),
            ) from exc
        return True

    def _still_blocked(
        self, surface: WebSurface, run: _Run, detectors: Sequence[Detector], detector: str | None
    ) -> str | None:
        state = self._snapshot(surface, detectors, run)
        if detector is not None and detector in state.fired_detectors:
            return f"the page still shows '{detector}'"
        if detector is None and state.dialog_titles:
            return f"dialog {state.dialog_titles[0]!r} is still open"
        return None

    # --- results ------------------------------------------------------------------------------

    def _success(
        self, surface: WebSurface, run: _Run, meta: Callable[[], dict[str, Any]], ev: RunEvidence
    ) -> ReplayResult:
        cap = run.cap
        errors = validate_outputs(cap, run.outputs)
        if errors:
            raise _Stop(
                "failure",
                code=FailureCode.OUTPUT_INVALID,
                expected="outputs matching the schema",
                observed="; ".join(errors),
            )
        state = self._snapshot(surface, (*self.ctx.profile.detectors, *cap.detectors), run)
        if not evaluate(cap.success, state):
            raise _Stop(
                "failure",
                code=FailureCode.UNEXPECTED_STATE,
                expected=describe(cap.success),
                observed=summarize(state),
                side_effects=run.side_effects(),
            )
        for name, value in run.outputs.items():
            ev.redactor.add(value, cap.sensitivities().get(name, "pii"))
        return Success(
            outputs=dict(run.outputs),
            degraded=run.degraded,
            recoveries=tuple(run.recoveries),
            **meta(),
        )

    def _stopped(
        self,
        stop: _Stop,
        surface: WebSurface,
        run: _Run,
        meta: Callable[[], dict[str, Any]],
        ev: RunEvidence,
    ) -> ReplayResult:
        f = stop.fields
        if stop.kind == "outcome":
            return BusinessOutcome(
                code=f["code"], detail=f["detail"], at_step=run.step_id, **meta()
            )
        evidence = self._capture(surface, ev, stop.kind)
        if stop.kind == "needs_human":
            iid = self._intervention(run, f["reason"], evidence)
            return NeedsHuman(
                intervention_id=iid, reason=f["reason"], at_step=run.step_id, **meta()
            )
        return make_failure(
            code=f["code"],
            at_step=run.step_id,
            expected=f["expected"],
            observed=f["observed"],
            side_effects=f.get("side_effects", run.side_effects()),
            evidence=tuple(evidence),
            recoveries=tuple(run.recoveries),
            **meta(),
        )

    def _capture(self, surface: WebSurface, ev: RunEvidence, why: str) -> list[str]:
        """Screenshot and page text at the stop, as paths relative to the run folder."""
        paths: list[Path] = []
        try:
            paths.append(ev.screenshot(surface, why))
            paths.append(ev.write_text("page_at_stop", surface.observe().render(max_text=4000)))
        except PlaywrightError:
            pass
        return [str(p.relative_to(ev.dir)) for p in paths]

    def _intervention(self, run: _Run, reason: str, evidence: list[str]) -> str:
        iid = f"int_{datetime.now(UTC).strftime('%Y%m%dT%H%M%S')}_{pysecrets.token_hex(3)}"
        self.interventions_dir.mkdir(parents=True, exist_ok=True)
        record = {
            "id": iid,
            "status": "open",
            "created_at": datetime.now(UTC).isoformat(timespec="seconds"),
            "run_id": run.run_id,
            "capability": run.cap.ref,
            "tenant": self.ctx.tenant.id,
            "step": run.step_id,
            "reason": reason,
            "evidence": evidence,
            "expected_on_return": "precondition re-verified before automation continues",
        }
        (self.interventions_dir / f"{iid}.json").write_text(
            json.dumps(record, indent=2), encoding="utf-8"
        )
        return iid

    def _finish(self, ev: RunEvidence, result: ReplayResult) -> ReplayResult:
        ev.event(
            "replay_finished",
            result=result.kind,
            code=getattr(result, "code", None),
            at_step=getattr(result, "at_step", None),
        )
        ev.write_json("result", json.loads(result_json(result)))
        ev.finalize()
        return result
