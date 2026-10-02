"""The discovery loop: observe -> decide (LLM) -> check -> act -> record, until a stop condition.

Stop conditions:

* ``succeeded``: the model called ``done``, every required output was extracted, and each one
  was re-read through its *proposed locator* and matched. A run whose locators do not replay
  is not a success.
* ``business_outcome``: the model declared a business result, quoting text actually on the page.
* ``escalated``: the model asked for a human, or policy said an action needs one, and no
  operator console is attached. With a handoff attached the run *pauses* instead: an operator
  approves the action (automation performs it with a single-use token issued to them), does it
  by hand, or aborts. Either way the step is recorded with who did or approved it.
* ``failed``: dead end (three actions with no page change), repeating the same action, three
  invalid tool calls in a row, or an unrecoverable error.
* ``budget_exhausted``: step or wall-clock budget spent.

What the model never controls: which actions are allowed (policy guard + browser navigation
guard), the real input values (it only sees placeholders), and what counts as success.
"""

from __future__ import annotations

import hashlib
import re
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from functools import partial
from typing import Any
from urllib.parse import urljoin

from playwright.sync_api import Error as PlaywrightError

from cua.core.artifact import ActionKind, PropertySchema, RiskClass
from cua.core.confirmation import Confirmation
from cua.core.lease import ControlLease
from cua.core.parse import ParseError, parse_value
from cua.core.policy import WRITE_SCOPE, PolicyDecision, PolicyGuard, ProposedAction
from cua.core.redact import Redactor
from cua.core.results import OUTCOME_CODE
from cua.core.targets import Fingerprint, TargetSpec
from cua.core.templating import TEMPLATE, TemplateError, render
from cua.core.text import contains
from cua.evidence.store import RunEvidence
from cua.handoff.session import Decision, Handoff
from cua.handoff.store import Kind
from cua.llm.base import LLMClient, LLMError, LLMResponse, ToolCall
from cua.surface.base import Observation, ObservedElement, Resolution, ResolutionError
from cua.surface.web import NavigationBlocked, WebSurface

from . import prompts
from .spec import DiscoverySpec
from .trace import (
    DiscoveryTrace,
    ElementInfo,
    LLMCallRecord,
    OutcomeRecord,
    PageRef,
    PolicyRecord,
    TraceStep,
)

REF_TOOLS = {"click", "fill", "select", "check", "press", "extract"}
MAX_INVALID_STREAK = 3
MAX_NO_CHANGE_STREAK = 3
MAX_REPEATS = 3


def scopes_for(ceiling: RiskClass) -> frozenset[str]:
    """Discovery gets write scope only if the spec allows writes. Irreversible always escalates
    in discovery because no confirmation token is ever issued to the agent."""
    return frozenset({WRITE_SCOPE}) if ceiling != RiskClass.READ else frozenset()


def parser_for(prop: PropertySchema, raw: str) -> str:
    explicit = (prop.model_extra or {}).get("x-parse")
    if explicit:
        return str(explicit)
    if prop.type == "number":
        return "currency_usd" if re.search(r"[$()]", raw) or "," in raw else "number"
    if prop.type == "integer":
        return "integer"
    return "text"


def parse_output(parser: str, raw: str) -> Any:
    return parse_value(parser, raw)


def _describe(el: ObservedElement) -> str:
    bits = [el.role]
    if el.label:
        bits.append(repr(el.label))
    elif el.name:
        bits.append(repr(el.name))
    if el.role == "cell" and el.column:
        bits.append(f"column={el.column!r} row={el.row[0] if el.row else ''!r}")
    return " ".join(bits)


def _signature(obs: Observation) -> str:
    elements = [(e.role, e.label, e.name, e.value) for e in obs.elements]
    body = f"{obs.route}|{obs.title}|{obs.text}|{elements}"
    return hashlib.sha256(body.encode()).hexdigest()[:16]


@dataclass
class _State:
    extracted: dict[str, Any] = field(default_factory=dict)
    raw: dict[str, str] = field(default_factory=dict)
    parsers: dict[str, str] = field(default_factory=dict)
    targets: dict[str, TargetSpec | None] = field(default_factory=dict)
    log: list[str] = field(default_factory=list)
    last_result: str = "Session is signed on; this is the starting page."
    invalid_streak: int = 0
    no_change_streak: int = 0
    last_key: tuple[str, ...] | None = None
    repeats: int = 0


class DiscoveryAgent:
    def __init__(
        self,
        *,
        surface: WebSurface,
        llm: LLMClient,
        spec: DiscoverySpec,
        guard: PolicyGuard,
        evidence: RunEvidence,
        trace: DiscoveryTrace,
        llm_redactor: Redactor,
        include_screenshot: bool = False,
        clock: Callable[[], float] = time.monotonic,
        handoff: Handoff | None = None,
    ) -> None:
        self.handoff = handoff
        self.lease = ControlLease()
        self._token = self.lease.token()
        surface.gate = lambda: self.lease.check(self._token)
        self.surface = surface
        self.llm = llm
        self.spec = spec
        self.guard = guard
        self.ev = evidence
        self.trace = trace
        self.llm_redactor = llm_redactor
        self.include_screenshot = include_screenshot and llm.supports_images
        self.clock = clock
        self.scopes = scopes_for(spec.risk_ceiling)
        self.contract = prompts.contract_lines(spec.inputs, spec.outputs, spec.outcomes)
        self._t0 = clock()

    # --- helpers ----------------------------------------------------------------------------

    def _page(self, obs: Observation | None = None) -> PageRef:
        if obs is not None:
            return PageRef(route=obs.route, title=obs.title)
        st = self.surface.page_state()
        return PageRef(route=st.route, title=st.title)

    def _missing(self, s: _State) -> list[str]:
        return [o for o in self.spec.outputs.required if o not in s.extracted]

    def _finish(self, status: str, reason: str) -> DiscoveryTrace:
        self.trace.status = status  # type: ignore[assignment]
        self.trace.reason = reason
        self.trace.finished_at = datetime.now(UTC)
        return self.trace

    def _policy(self, action: ProposedAction) -> PolicyDecision:
        return self.guard.evaluate(action, scopes=self.scopes)

    # --- the loop ---------------------------------------------------------------------------

    def run(self) -> DiscoveryTrace:
        trace = self._loop()
        # Logged here, after the last step's event, so the timeline reads in order.
        self.ev.event("run_finished", status=trace.status, reason=trace.reason)
        return trace

    def _loop(self) -> DiscoveryTrace:
        s = _State()
        for turn in range(1, self.spec.max_steps + 1):
            if self.clock() - self._t0 > self.spec.max_minutes * 60:
                return self._finish("budget_exhausted", "wall-clock budget spent")

            obs = self.surface.observe()
            regions = [(e.bbox, e.value or "") for e in obs.elements if e.bbox and e.value]
            # File names are not redacted, so they carry no page data (a route can hold an ID).
            self.ev.capture(self.surface, f"turn{turn:02d}", regions)
            # One choke point: the whole message is redacted right before it leaves the process,
            # so no field (observation, action log, last result) can carry a raw value past it.
            user = self.llm_redactor.redact_text(
                prompts.user_message(
                    goal=self.spec.goal,
                    contract=self.contract,
                    hints=self.spec.hints,
                    turn=turn,
                    max_steps=self.spec.max_steps,
                    action_log=s.log,
                    last_result=s.last_result,
                    extracted=list(s.extracted),
                    missing=self._missing(s),
                    observation=obs.render(max_text=1500),
                )
            )
            self.ev.write_text(f"prompts/turn-{turn:02d}", user)
            image = None
            if self.include_screenshot:
                image = self.surface.screenshot_png(
                    redact_values=self.llm_redactor.sensitive_strings(),
                    redact_patterns=self.llm_redactor.pattern_sources(),
                )

            try:
                resp = self.llm.decide(prompts.SYSTEM, user, prompts.TOOLS, image)
            except LLMError as exc:
                return self._finish("failed", f"model provider error: {exc}")
            self._account(resp)
            call = resp.call
            self.ev.event(
                "llm_decision",
                turn=turn,
                provider=resp.provider,
                model=resp.model,
                request_id=resp.request_id,
                input_tokens=resp.input_tokens,
                output_tokens=resp.output_tokens,
                latency_ms=resp.latency_ms,
                retries=resp.retries,
                tool=call.name if call else None,
                args=call.args if call else None,
                text=resp.text[:500],
            )

            step = TraceStep(
                index=turn,
                tool=call.name if call else "(none)",
                args=dict(call.args) if call else {},
                rationale=str((call.args if call else {}).get("rationale", ""))[:400],
                before=self._page(obs),
                status="invalid",
                llm=LLMCallRecord(
                    provider=resp.provider,
                    model=resp.model,
                    request_id=resp.request_id,
                    input_tokens=resp.input_tokens,
                    output_tokens=resp.output_tokens,
                    latency_ms=resp.latency_ms,
                    retries=resp.retries,
                ),
                t_ms=int((self.clock() - self._t0) * 1000),
            )
            self.trace.steps.append(step)

            stop = self._handle(call, obs, step, s)
            self.ev.event(
                "step",
                index=turn,
                tool=step.tool,
                status=step.status,
                error=step.error,
                policy=step.policy.model_dump() if step.policy else None,
                after=step.after.model_dump() if step.after else None,
            )
            if stop is not None:
                return stop

            if step.status == "invalid":
                s.invalid_streak += 1
                if s.invalid_streak >= MAX_INVALID_STREAK:
                    return self._finish("failed", "three invalid tool calls in a row")
            else:
                s.invalid_streak = 0

            if step.status == "ok" and step.tool not in ("extract",):
                after = self.surface.observe()
                if _signature(after) == _signature(obs):
                    s.no_change_streak += 1
                    if s.no_change_streak >= MAX_NO_CHANGE_STREAK:
                        return self._finish("failed", "dead end: three actions without any change")
                else:
                    s.no_change_streak = 0

            key = (
                step.tool,
                step.element.label if step.element else "",
                str(step.args.get("value", "")),
            )
            s.repeats = s.repeats + 1 if key == s.last_key else 1
            s.last_key = key
            if s.repeats >= MAX_REPEATS and step.tool != "extract":
                return self._finish(
                    "failed", f"repeating the same action ({step.tool}) three times"
                )

        return self._finish("budget_exhausted", f"no result within {self.spec.max_steps} steps")

    def _account(self, resp: LLMResponse) -> None:
        self.trace.llm_calls += 1
        self.trace.input_tokens += resp.input_tokens or 0
        self.trace.output_tokens += resp.output_tokens or 0

    # --- one decision -----------------------------------------------------------------------

    def _invalid(self, step: TraceStep, s: _State, msg: str) -> None:
        step.status = "invalid"
        step.error = msg
        s.last_result = f"INVALID: {msg}"
        s.log.append(f"{step.index}. {step.tool} -> invalid ({msg})")

    def _handle(
        self, call: ToolCall | None, obs: Observation, step: TraceStep, s: _State
    ) -> DiscoveryTrace | None:
        if call is None:
            self._invalid(step, s, "no tool was called; call exactly one tool")
            return None
        name, args = call.name, call.args
        if name not in prompts.TOOL_NAMES:
            self._invalid(step, s, f"unknown tool {name!r}")
            return None

        if name == "escalate":
            reason = str(args.get("reason", "model asked for a human"))
            if self.handoff is None:
                step.status = "escalated"
                return self._finish("escalated", reason)
            d = self._pause(
                step,
                kind="stuck",
                what="the agent asked for help",
                reason=reason,
                expects="The page is ready for the agent to continue.",
                check=lambda _d: None,
            )
            if isinstance(d, DiscoveryTrace):
                return d
            step.status = "ok"
            did = "; ".join(self._say(a) for a in d.human_actions) or "nothing in the window"
            s.last_result = f"A human operator stepped in ({did}). Look at the page and continue."
            s.log.append(f"{step.index}. escalate -> operator {d.operator} stepped in ({did})")
            return None

        if name == "done":
            missing = self._missing(s)
            if missing:
                self._invalid(step, s, f"not done: still need {', '.join(missing)}")
                return None
            step.status = "ok"
            return self._verify_and_finish(s)

        if name == "declare_outcome":
            code = str(args.get("code", ""))
            evidence_text = str(args.get("evidence", "")).strip()
            if not re.match(OUTCOME_CODE, code):
                self._invalid(step, s, "code must be UPPER_SNAKE")
                return None
            if len(evidence_text) < 4 or not contains(obs.text, evidence_text):
                self._invalid(step, s, "evidence must be text quoted exactly from the current page")
                return None
            step.status = "ok"
            self.trace.outcome = OutcomeRecord(
                code=code,
                detail=str(args.get("rationale", "")),
                evidence_text=evidence_text,
                page=self._page(obs),
            )
            return self._finish("business_outcome", f"{code}: {evidence_text}")

        if name == "navigate":
            return self._navigate(str(args.get("route", "")), step, s)

        # --- actions on an element ------------------------------------------------------------
        ref = str(args.get("ref", ""))
        try:
            el = obs.element(ref)
        except KeyError:
            self._invalid(step, s, f"{ref!r} is not an element on the current page")
            return None
        step.ref = ref
        step.element = ElementInfo(
            role=el.role,
            label=el.label,
            name=el.name,
            tag=el.tag,
            row=el.row,
            column=el.column,
            context=el.context,
            dialog=el.dialog,
            frame=el.frame,
        )
        what = _describe(el)

        if name == "extract":
            output = str(args.get("output", ""))
            if output not in self.spec.outputs.properties:
                self._invalid(step, s, f"{output!r} is not a required output")
                return None
            step.output = output
        elif name == "fill":
            value = str(args.get("value", ""))
            try:
                render(value, self.spec.example_inputs)
            except TemplateError as exc:
                self._invalid(step, s, f"bad placeholder: {exc}")
                return None
            if TEMPLATE.search(value) is None and self.llm_redactor.redact_text(value) != value:
                self._invalid(step, s, "that looks like sensitive data; use an input placeholder")
                return None
        elif name == "select":
            option = str(args.get("option", ""))
            try:
                real_option = render(option, self.spec.example_inputs)
            except TemplateError as exc:
                self._invalid(step, s, f"bad placeholder: {exc}")
                return None
            if "[REDACTED" in option:
                # The model only sees redacted option text. If one of the inputs identifies the
                # option, it must say so with the placeholder (so the capability is reusable);
                # otherwise map its choice back, but only if exactly one option matches.
                shown = [o for o in el.options if self.llm_redactor.redact_text(o) == option]
                real_option = shown[0] if len(shown) == 1 else option
                names = [
                    k
                    for k, v in self.spec.example_inputs.items()
                    if str(v) and str(v) in real_option
                ]
                if names:
                    self._invalid(
                        step,
                        s,
                        "that option holds input data; pass the placeholder "
                        f"{{{{inputs.{names[0]}}}}} as the option instead",
                    )
                    return None

        # Propose a durable locator *before* acting: after a click the element may be gone.
        try:
            step.target = self.surface.suggest_target(ref, f"{what} ({self.spec.capability_id})")
        except ResolutionError as exc:
            step.target_note = f"no unique locator: {exc}"
        try:
            step.fingerprint = Fingerprint(**self.surface.fingerprint(ref))
        except (KeyError, ValueError):
            step.fingerprint = None

        decision = self._policy(
            ProposedAction(
                kind=ActionKind(name),
                url=self.surface.content_url,
                control_name=el.name or el.label,
                step_id=f"turn{step.index}",
            )
        )
        step.policy = PolicyRecord(
            verdict=decision.verdict, effective_risk=decision.effective_risk, reason=decision.reason
        )
        obs_res: Resolution | None = None
        if decision.verdict == "escalate":
            if self.handoff is None:
                step.status = "escalated"
                return self._finish("escalated", f"{what}: {decision.reason}")
            before = _signature(obs)
            target = step.target

            def check(d: Decision) -> str | None:
                if d.action == "step_done":
                    same = _signature(self.surface.observe()) == before
                    return "the page has not changed; the step does not look done" if same else None
                if target is not None and self.surface.count(target) != 1:
                    return f"{what} is no longer on the page"
                return None

            d = self._pause(
                step,
                kind="confirmation",
                what=what,
                reason=f"{name} {what}: {decision.reason}",
                expects=(
                    "Approve: the page is unchanged and the agent performs this action. "
                    "Did it yourself: the page has moved on."
                ),
                check=check,
            )
            if isinstance(d, DiscoveryTrace):
                return d
            if d.action == "step_done":
                step.actor = f"human:{d.operator}"
                step.after = self._page()
                step.status = "ok"
                s.log.append(
                    f"{step.index}. {name} ({what}) -> done by a human operator; "
                    f"page now {step.after.route} {step.after.title!r}"
                )
                s.last_result = (
                    f"OK: a human operator performed this step; page now {step.after.title!r}"
                )
                return None
            tokens = self.guard.tokens
            if tokens is None:
                step.status = "escalated"
                return self._finish("escalated", "approved, but no confirmation secret is set")
            sid = f"turn{step.index}"
            run_id, inputs = self.trace.run_id, self.spec.example_inputs
            token = tokens.issue(
                run_id=run_id, step_id=sid, inputs=inputs, issued_to=d.operator or "?"
            )
            decision = self.guard.evaluate(
                ProposedAction(
                    kind=ActionKind(name),
                    url=self.surface.content_url,
                    control_name=el.name or el.label,
                    step_id=sid,
                ),
                scopes=self.scopes,
                confirmation=Confirmation(token, run_id, sid, inputs),
            )
            step.policy = PolicyRecord(
                verdict=decision.verdict,
                effective_risk=decision.effective_risk,
                reason=decision.reason,
            )
            if decision.verdict != "allow":
                step.status = "denied"
                return self._finish("failed", f"approval was not accepted: {decision.reason}")
            step.approved_by = decision.confirmed_by
            # The pause invalidated the observation's element handles; resolve the control again.
            if target is None:
                step.status = "error"
                return self._finish("failed", f"{what} has no unique locator to act on")
            obs_res = self.surface.resolve(target)
        if decision.verdict == "deny":
            step.status = "denied"
            s.last_result = f"DENIED by policy: {decision.reason}. Choose a different action."
            s.log.append(f"{step.index}. {name} {ref} ({what}) -> DENIED ({decision.reason})")
            return None

        blocked_before = len(self.surface.blocked)
        res = obs_res or self.surface.resolution_for_ref(ref)
        try:
            if name == "extract":
                assert step.output is not None
                raw = self.surface.read(res)
                prop = self.spec.outputs.properties[step.output]
                step.parser = parser_for(prop, raw)
                try:
                    step.value = parse_output(step.parser, raw)
                except ParseError as exc:
                    step.status = "error"
                    step.error = str(exc)
                    s.last_result = f"ERROR: could not read {step.output} as {prop.type}: {exc}"
                    s.log.append(f"{step.index}. extract {ref} ({what}) -> error")
                    return None
                s.extracted[step.output] = step.value
                s.raw[step.output] = raw
                s.parsers[step.output] = step.parser
                s.targets[step.output] = step.target
                self.ev.redactor.add(step.value, prop.sensitivity.value)
                self.ev.redactor.add(raw, prop.sensitivity.value)
            elif name == "fill":
                self.surface.act(res, "fill", render(str(args["value"]), self.spec.example_inputs))
            elif name == "select":
                self.surface.act(res, "select", real_option)
            elif name == "press":
                self.surface.act(res, "press", str(args.get("key", "Enter")))
            elif name == "check":
                self.surface.act(res, "check")
            else:
                self.surface.act(res, "click")
        except (PlaywrightError, ValueError) as exc:
            step.status = "error"
            step.error = str(exc).splitlines()[0][:300]
            s.last_result = f"ERROR: the action failed: {step.error}"
            s.log.append(f"{step.index}. {name} {ref} ({what}) -> error")
            return None

        step.after = self._page()
        if len(self.surface.blocked) > blocked_before:
            step.status = "denied"
            url = self.surface.blocked[-1]
            s.last_result = f"DENIED: navigation to {url} was blocked by policy."
            s.log.append(f"{step.index}. {name} {ref} ({what}) -> navigation blocked")
            return None
        step.status = "ok"
        detail = (
            f"read {step.output}"
            if name == "extract"
            else f"page now {step.after.route} {step.after.title!r}"
        )
        arg = f" value={args['value']!r}" if name == "fill" else ""
        s.log.append(f"{step.index}. {name} {ref} ({what}){arg} -> ok; {detail}")
        s.last_result = f"OK: {detail}"
        return None

    # --- human in the loop ------------------------------------------------------------------

    @staticmethod
    def _say(action: dict[str, Any]) -> str:
        name = action.get("label") or action.get("name")
        return f"{action.get('event')} {action.get('role')} {name!r}"

    def _pause(
        self,
        step: TraceStep,
        *,
        kind: Kind,
        what: str,
        reason: str,
        expects: str,
        check: Callable[[Decision], str | None],
    ) -> Decision | DiscoveryTrace:
        """Hand the live session to an operator. Returns their decision once the page passes
        ``check``, or the finished trace if they aborted or nobody came."""
        assert self.handoff is not None
        existing = None
        while True:
            d = self.handoff.request(
                surface=self.surface,
                lease=self.lease,
                evidence=self.ev,
                kind=kind,
                run_id=self.trace.run_id,
                mode="discovery",
                capability=self.spec.capability_id,
                tenant=self.spec.tenant,
                step=f"turn{step.index}",
                step_description=what,
                reason=reason,
                expects_on_return=expects,
                existing=existing,
            )
            step.intervention = d.intervention.id
            step.human_actions = [self.ev.redactor.redact(a) for a in d.human_actions]
            if d.action in ("abort", "timeout"):
                step.status = "escalated"
                why = "aborted by the operator" if d.action == "abort" else "no operator in time"
                return self._finish("escalated", f"{reason} ({why})")
            if self.handoff.verify_and_resume(d, self.lease, self.ev, partial(check, d)):
                self._token = self.lease.token()
                return d
            if self.lease.holder == "aborted":
                step.status = "escalated"
                return self._finish("escalated", f"{reason} (hand-back checks kept failing)")
            existing = d.intervention

    def _navigate(self, route: str, step: TraceStep, s: _State) -> DiscoveryTrace | None:
        if not route.startswith("/") or route.startswith("//"):
            self._invalid(step, s, "route must be an absolute path such as /search")
            return None
        url = urljoin(self.surface.base_url, route.lstrip("/"))
        decision = self._policy(
            ProposedAction(ActionKind.NAVIGATE, url, step_id=f"turn{step.index}")
        )
        step.policy = PolicyRecord(
            verdict=decision.verdict, effective_risk=decision.effective_risk, reason=decision.reason
        )
        if decision.verdict != "allow":
            step.status = "denied" if decision.verdict == "deny" else "escalated"
            if decision.verdict == "escalate":
                return self._finish("escalated", decision.reason)
            s.last_result = f"DENIED by policy: {decision.reason}. Choose a different action."
            s.log.append(f"{step.index}. navigate {route} -> DENIED ({decision.reason})")
            return None
        try:
            self.surface.navigate(route, self.surface.content_frame)
        except NavigationBlocked as exc:
            step.status = "denied"
            s.last_result = f"DENIED: {exc}"
            s.log.append(f"{step.index}. navigate {route} -> blocked")
            return None
        step.after = self._page()
        step.status = "ok"
        s.log.append(f"{step.index}. navigate {route} -> ok; page now {step.after.route}")
        s.last_result = f"OK: page now {step.after.route} {step.after.title!r}"
        return None

    def _verify_and_finish(self, s: _State) -> DiscoveryTrace:
        """Re-read each output through its proposed locator. Success means it will replay."""
        self.trace.extracted = dict(s.extracted)
        for output, value in s.extracted.items():
            target = s.targets.get(output)
            ok = False
            if target is not None:
                try:
                    raw = self.surface.read(self.surface.resolve(target))
                    ok = parse_output(s.parsers[output], raw) == value
                except (ResolutionError, ParseError, PlaywrightError):
                    ok = False
            self.trace.verified_outputs[output] = ok
        bad = [o for o, ok in self.trace.verified_outputs.items() if not ok]
        if bad:
            return self._finish("failed", f"outputs did not re-read through their locators: {bad}")
        return self._finish("succeeded", "all required outputs extracted and verified")
