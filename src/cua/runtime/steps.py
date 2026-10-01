"""Run one artifact step on a surface: render placeholders, resolve, act, wait, verify.

This is deliberately small and policy-free. The replay engine (Day 6) wraps it with policy
checks, detector precedence, recovery and result classification; discovery uses it to run the
approved ``login`` sub-capability before the agent starts.
"""

from __future__ import annotations

import json
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass, replace
from typing import Any

from pydantic import TypeAdapter

from cua.core.artifact import ActionKind, Capability, Detector, FailThen, Step
from cua.core.conditions import Condition, ConditionError, PageState, evaluate
from cua.core.results import FailureCode
from cua.core.templating import render
from cua.surface.base import ResolutionError, Surface

_COND: TypeAdapter[Condition] = TypeAdapter(Condition)
DEFAULT_POSTCONDITION_MS = 8000


def describe(cond: Condition) -> str:
    return json.dumps(_COND.dump_python(cond, by_alias=True, mode="json"), separators=(",", ":"))


def summarize(state: PageState) -> str:
    dialogs = f" dialogs={list(state.dialog_titles)}" if state.dialog_titles else ""
    return f"route={state.route} title={state.title!r}{dialogs}"


class StepError(Exception):
    """A step could not complete. Carries what the replay result contract needs."""

    def __init__(
        self, code: FailureCode, step_id: str, expected: str, observed: str, state: PageState | None
    ) -> None:
        super().__init__(f"{code.value} at {step_id}: expected {expected}; observed {observed}")
        self.code, self.step_id, self.expected, self.observed, self.state = (
            code,
            step_id,
            expected,
            observed,
            state,
        )


def fired_detectors(state: PageState, detectors: Sequence[Detector]) -> frozenset[str]:
    fired = set()
    for d in detectors:
        try:
            if evaluate(d.when, state):
                fired.add(d.id)
        except ConditionError:
            continue
    return frozenset(fired)


def snapshot(
    surface: Surface, detectors: Sequence[Detector], outputs: dict[str, Any] | None = None
) -> PageState:
    state = surface.page_state(outputs)
    return replace(state, fired_detectors=fired_detectors(state, detectors))


def wait_until(
    surface: Surface,
    cond: Condition,
    detectors: Sequence[Detector],
    timeout_ms: int,
    outputs: dict[str, Any] | None = None,
    poll_ms: int = 100,
) -> tuple[bool, PageState]:
    deadline = time.monotonic() + timeout_ms / 1000
    while True:
        state = snapshot(surface, detectors, outputs)
        if evaluate(cond, state):
            return True, state
        if time.monotonic() >= deadline:
            return False, state
        surface.pause(poll_ms)


@dataclass
class StepOutcome:
    step_id: str
    raw: str | None
    strategy_index: int | None
    state: PageState


@dataclass
class StepRunner:
    surface: Surface
    inputs: dict[str, Any]
    secrets: Callable[[str, str], str] | None
    detectors: Sequence[Detector] = ()

    def run(self, step: Step, outputs: dict[str, Any] | None = None) -> StepOutcome:
        raw: str | None = None
        index: int | None = None
        if step.action == ActionKind.NAVIGATE:
            assert step.route is not None
            self.surface.navigate(render(step.route, self.inputs, self.secrets), step.frame)
        else:
            assert step.target is not None
            try:
                res = self.surface.resolve(step.target)
            except ResolutionError as exc:
                state = snapshot(self.surface, self.detectors, outputs)
                raise StepError(
                    exc.code, step.id, step.target.description, str(exc), state
                ) from exc
            index = res.strategy_index
            if step.action == ActionKind.EXTRACT:
                raw = self.surface.read(res)
            elif step.action == ActionKind.FILL:
                self.surface.act(res, "fill", render(step.value or "", self.inputs, self.secrets))
            elif step.action == ActionKind.SELECT:
                self.surface.act(
                    res, "select", render(step.option or "", self.inputs, self.secrets)
                )
            elif step.action == ActionKind.PRESS:
                self.surface.act(res, "press", step.key)
            elif step.action == ActionKind.CHECK:
                self.surface.act(res, "check")
            else:
                self.surface.act(res, "click")

        state = snapshot(self.surface, self.detectors, outputs)
        if step.wait is not None:
            ok, state = wait_until(
                self.surface, step.wait.until, self.detectors, step.wait.timeout_ms, outputs
            )
            if not ok:
                raise StepError(
                    FailureCode.TIMEOUT, step.id, describe(step.wait.until), summarize(state), state
                )
        if step.postcondition is not None:
            timeout = step.wait.timeout_ms if step.wait else DEFAULT_POSTCONDITION_MS
            ok, state = wait_until(
                self.surface, step.postcondition, self.detectors, timeout, outputs
            )
            if not ok:
                raise StepError(
                    FailureCode.UNEXPECTED_STATE,
                    step.id,
                    describe(step.postcondition),
                    summarize(state),
                    state,
                )
        return StepOutcome(step.id, raw, index, state)


def run_login(
    surface: Surface,
    login: Capability,
    secrets: Callable[[str, str], str],
    extra_detectors: Sequence[Detector] = (),
) -> PageState:
    """Sign on with the approved login sub-capability. Raises StepError on any failure."""
    detectors = (*login.detectors, *extra_detectors)
    runner = StepRunner(surface, {}, secrets, detectors)
    state: PageState | None = None
    for step in login.steps:
        state = runner.run(step).state
        for det in login.detectors:
            if det.id in state.fired_detectors and isinstance(det.then, FailThen):
                raise StepError(det.then.fail, step.id, "signed on", summarize(state), state)
    assert state is not None
    if not evaluate(login.success, state):
        raise StepError(
            FailureCode.LOGIN_FAILED,
            login.steps[-1].id,
            describe(login.success),
            summarize(state),
            state,
        )
    return state
