"""Locator probe: does this capability work on that tenant, and if not, what override would fix it?

Run when onboarding a tenant or after a vendor upgrade. No model is involved. The probe replays
the capability's steps on the tenant with example inputs and, where something does not fit,
tries two *bounded* repairs:

* **Relabel.** A label or row name that does not resolve (or resolves only through a fallback
  strategy) is retried with the app profile's known synonyms ("Member ID" -> "Member #"). A
  synonym is accepted only if it resolves to exactly one element (and, for a fallback, the same
  element). The same relabel is carried into detectors that use that name.
* **Extra required checkbox.** If a step's wait fails and the page shows exactly one unchecked
  checkbox that no step touches, a ``check`` step is proposed before the failing step.

After each proposal the probe starts again in a fresh session with the proposals applied, so the
final round proves the whole set works together. Nothing is written to the capability: the
output is a proposal for a person to review and approve as a new version.

Only read-only capabilities are probed; a write flow would change the tenant's data.
"""

from __future__ import annotations

import copy
import re
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any, Literal

from playwright.sync_api import Browser

from cua.core.artifact import Capability, Detector, RiskClass, Step, parse_capability
from cua.core.overrides import apply_ops, capability_dict, effective_capability
from cua.core.parse import ParseError, parse_value
from cua.core.results import FailureCode
from cua.core.targets import TargetSpec
from cua.runtime.steps import StepError, StepRunner, run_login
from cua.surface.base import ResolutionError
from cua.surface.web import WebSurface

Level = Literal["ok", "degraded", "proposed", "blocked", "info"]
_FIELDS = ("text", "row_match", "name", "column", "table_near")
MAX_ROUNDS = 6


@dataclass
class Finding:
    level: Level
    step: str | None
    message: str

    def line(self) -> str:
        where = f"{self.step}: " if self.step else ""
        return f"[{self.level}] {where}{self.message}"


@dataclass
class ProbeReport:
    capability: str
    tenant: str
    works: bool = False
    rounds: int = 0
    findings: list[Finding] = field(default_factory=list)
    ops: list[dict[str, Any]] = field(default_factory=list)
    outputs_read: list[str] = field(default_factory=list)

    def proposal(self) -> dict[str, Any]:
        return {"overrides": {self.tenant: self.ops}} if self.ops else {}


def _slug(text: str, limit: int = 40) -> str:
    return re.sub(r"[^a-z0-9]+", "_", text.lower()).strip("_")[:limit].strip("_")


def _strategy_paths(node: Any, path: str) -> list[tuple[str, dict[str, Any]]]:
    """Every locator strategy under ``node`` with its override path."""
    out: list[tuple[str, dict[str, Any]]] = []
    if isinstance(node, dict):
        if isinstance(node.get("by"), str):
            out.append((path, node))
        for k, v in node.items():
            out += _strategy_paths(v, f"{path}.{k}")
    elif isinstance(node, list):
        for i, v in enumerate(node):
            out += _strategy_paths(v, f"{path}[{i}]")
    return out


class Probe:
    def __init__(
        self,
        *,
        browser: Browser,
        ctx: Any,  # cua.config.TenantContext
        secrets: Callable[[str, str], str],
        base_url: str | None = None,
        url_guard: Callable[[str], bool] | None = None,
        wait_ms: int = 3000,
        settle_ms: int = 3000,
    ) -> None:
        self.browser, self.ctx, self.secrets = browser, ctx, secrets
        self.base_url = base_url or ctx.tenant.base_url
        self.url_guard = url_guard or ctx.url_guard()
        self.wait_ms, self.settle_ms = wait_ms, settle_ms
        self.synonyms: dict[str, tuple[str, ...]] = dict(ctx.profile.label_synonyms)

    # --- public -----------------------------------------------------------------------------

    def run(self, cap: Capability, inputs: dict[str, Any]) -> ProbeReport:
        tenant = self.ctx.tenant.id
        report = ProbeReport(capability=cap.ref, tenant=tenant)
        if cap.risk != RiskClass.READ:
            report.findings.append(
                Finding("blocked", None, f"risk is {cap.risk.value}; only read flows are probed")
            )
            return report
        base, existing = effective_capability(cap, tenant)
        if existing:
            report.findings.append(
                Finding("info", None, f"{existing} existing override(s) for {tenant} applied first")
            )
        base_dict = capability_dict(base)
        while report.rounds < MAX_ROUNDS:
            report.rounds += 1
            data = apply_ops(copy.deepcopy(base_dict), copy.deepcopy(report.ops))
            current = parse_capability(data, verify_hash=False)
            before = len(report.ops)
            round_findings: list[Finding] = []
            done = self._round(current, inputs, report, round_findings)
            if done or len(report.ops) == before:
                report.findings += round_findings
                report.works = done
                return report
            report.findings += [f for f in round_findings if f.level == "proposed"]
        report.findings.append(Finding("blocked", None, f"no stable result in {MAX_ROUNDS} rounds"))
        return report

    # --- one session ------------------------------------------------------------------------

    def _round(
        self, cap: Capability, inputs: dict[str, Any], report: ProbeReport, out: list[Finding]
    ) -> bool:
        """Run every step once. True if the capability completed with no new proposal."""
        surface = WebSurface.open(
            self.browser,
            self.base_url,
            content_frame=self.ctx.profile.content_frame,
            url_guard=self.url_guard,
            settle_ms=self.settle_ms,
        )
        try:
            surface.start("/")
            run_login(surface, self.ctx.login, self.secrets, self.ctx.profile.detectors)
            detectors: tuple[Detector, ...] = (*self.ctx.profile.detectors, *cap.detectors)
            runner = StepRunner(surface, inputs, self.secrets, detectors)
            outputs: dict[str, Any] = {}
            clean = True
            previous: str | None = None
            for step in cap.steps:
                step = self._fast(step)
                if step.target is not None and self._check_target(surface, cap, step, report, out):
                    clean = False
                try:
                    result = runner.run(step, outputs)
                except StepError as exc:
                    if exc.code in (FailureCode.TARGET_NOT_FOUND, FailureCode.TARGET_AMBIGUOUS):
                        if not any(f.step == step.id and f.level == "proposed" for f in out):
                            out.append(
                                Finding("blocked", step.id, f"cannot locate: {exc.observed}")
                            )
                        return False
                    if self._propose_checkbox(surface, cap, step, previous, report, out):
                        return False
                    alerts = surface.alerts()
                    said = f"; page says: {' | '.join(alerts)}" if alerts else ""
                    out.append(
                        Finding("blocked", step.id, f"{exc.code.value}: {exc.observed}{said}")
                    )
                    return False
                if result.raw is not None and step.output:
                    try:
                        outputs[step.output] = parse_value(step.parse, result.raw)
                        report.outputs_read = sorted({*report.outputs_read, step.output})
                    except ParseError as exc:
                        out.append(Finding("blocked", step.id, f"value does not parse: {exc}"))
                        return False
                if not any(f.step == step.id for f in out):
                    out.append(Finding("ok", step.id, "resolves with its first strategy"))
                previous = step.id
            return clean
        finally:
            surface.close()

    def _fast(self, step: Step) -> Step:
        if step.wait is None or step.wait.timeout_ms <= self.wait_ms:
            return step
        return step.model_copy(
            update={"wait": step.wait.model_copy(update={"timeout_ms": self.wait_ms})}
        )

    # --- repair 1: relabel ------------------------------------------------------------------

    def _check_target(
        self,
        surface: WebSurface,
        cap: Capability,
        step: Step,
        report: ProbeReport,
        out: list[Finding],
    ) -> bool:
        """True if a relabel was proposed for this step."""
        target = step.target
        assert target is not None
        try:
            found = surface.resolve(target)
        except ResolutionError:
            found = None
        if found is not None and found.strategy_index == 0:
            return False
        first = target.strategies[0].model_dump(mode="json", exclude_none=True)
        for name in _FIELDS:
            old = first.get(name)
            if not isinstance(old, str):
                continue
            for new in self.synonyms.get(old, ()):
                trial = TargetSpec.model_validate(
                    {
                        "description": target.description,
                        "frame": [f.model_dump(exclude_none=True) for f in target.frame],
                        "strategies": [{**first, name: new}],
                    }
                )
                try:
                    hit = surface.resolve(trial)
                except ResolutionError:
                    continue
                if found is not None and not surface.same_element(found, hit):
                    continue
                self._relabel(cap, old, new, name, step.id, report)
                how = (
                    f"found only by fallback '{found.strategy.by}'"
                    if found is not None
                    else "not found"
                )
                out.append(
                    Finding("proposed", step.id, f"{name} {old!r} {how}; this tenant shows {new!r}")
                )
                return True
        if found is not None:
            out.append(
                Finding(
                    "degraded",
                    step.id,
                    f"resolves only by fallback '{found.strategy.by}'; no known synonym fits",
                )
            )
        return False

    def _relabel(
        self, cap: Capability, old: str, new: str, name: str, step_id: str, report: ProbeReport
    ) -> None:
        """One relabel usually shows up in several places (a step and a detector): fix them all."""
        data = capability_dict(cap)
        paths = [(f"steps.{step_id}.target.strategies[0]", {name: old})]
        for det in data.get("detectors", []):
            for path, strategy in _strategy_paths(det, f"detectors.{det['id']}"):
                if strategy.get(name) == old:
                    paths.append((path, strategy))
        known = {op.get("path") for op in report.ops}
        for path, _ in paths:
            full = f"{path}.{name}"
            if full not in known:
                report.ops.append({"op": "set", "path": full, "value": new})

    # --- repair 2: a required checkbox ------------------------------------------------------

    def _propose_checkbox(
        self,
        surface: WebSurface,
        cap: Capability,
        step: Step,
        previous: str | None,
        report: ProbeReport,
        out: list[Finding],
    ) -> bool:
        if previous is None:
            return False
        obs = surface.observe()
        boxes = [e for e in obs.elements if e.role == "checkbox" and e.value == "unchecked"]
        if len(boxes) != 1:
            return False
        box = boxes[0]
        try:
            target = surface.suggest_target(box.ref, f"{box.label or box.name} checkbox")
        except ResolutionError:
            return False
        used = [s.target for s in cap.steps if s.target is not None and s.action.value == "check"]
        if any(t.strategies[0] == target.strategies[0] for t in used):
            return False  # already checked by a step; the failure is something else
        alerts = surface.alerts()
        new_id = f"check_{_slug(box.label or box.name or 'required_box')}"
        report.ops.append(
            {
                "op": "insert_step",
                "after": previous,
                "step": {
                    "id": new_id,
                    "action": "check",
                    "target": target.model_dump(mode="json", by_alias=True, exclude_none=True),
                },
            }
        )
        said = f" (page says: {' | '.join(alerts)})" if alerts else ""
        out.append(
            Finding(
                "proposed",
                step.id,
                f"did not complete{said}; this tenant has a required checkbox "
                f"{box.label or box.name!r}: insert step '{new_id}' after '{previous}'",
            )
        )
        return True
