"""Discovery trace(s) -> capability draft.

The compiler is ordinary deterministic code. It never re-reads model text and never calls a
model; it uses only what the surface verified during discovery. Given the same traces it
produces the same artifact (same content hash).

What it does:

1. **Keeps only what worked.** Steps that were invalid, denied, errored or escalated are dead
   ends and are dropped. Superseded actions are dropped too (a field filled twice keeps the last
   fill; an output extracted twice keeps the last read).
2. **Adds the entry point.** A first step navigates to the spec's start route, since discovery
   began there already signed on.
3. **Uses verified locators.** Each step's target is the list of strategies the surface proved
   unique *on the recorded page*, plus a fingerprint for drift detection (data scrubbed out).
4. **Parameterizes.** Values typed during discovery are already placeholders; a literal that
   equals an example input, or a redacted identifier inside a route, becomes ``{{inputs.x}}``.
5. **Derives waits from what was observed.** A step after which the page changed waits for the
   new page's title, or for any known business outcome.
6. **Turns interstitials into recoveries.** A click on a control inside a dialog becomes a
   ``recover: click`` detector, not a step: the notice only appears sometimes.
7. **Turns declared outcomes into detectors.** A business-outcome trace contributes a detector
   built from the exact page text the model quoted.
8. **Writes review notes.** Anything a person should confirm is listed, nothing is hidden.

The result is always a **draft**. Only ``cua approve`` by a person makes it runnable unattended.
"""

from __future__ import annotations

import re
from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import Any, Literal

from pydantic import TypeAdapter

from cua.core.artifact import (
    Capability,
    OutcomeDecl,
    RiskClass,
    compute_hash,
    max_risk,
)
from cua.core.conditions import Condition
from cua.core.redact import stable_tag
from cua.core.results import OUTCOME_CODE
from cua.core.semver import parse_version
from cua.core.targets import FrameRef
from cua.discovery.trace import DiscoveryTrace, TraceStep

from .yaml_out import dump

ACTIONS = {"click", "fill", "select", "check", "press", "navigate", "extract"}
_COND: TypeAdapter[Condition] = TypeAdapter(Condition)
_REDACTED = re.compile(r"\[(REDACTED:[a-z_]+|identifier:[0-9a-f]+)\]")
DEFAULT_WAIT_MS = 8000


@dataclass
class Note:
    level: Literal["info", "warn", "error"]
    message: str
    step: str | None = None

    def line(self) -> str:
        where = f" [{self.step}]" if self.step else ""
        return f"{self.level.upper():5}{where} {self.message}"


class CompileError(Exception):
    def __init__(self, notes: list[Note]) -> None:
        self.notes = notes
        super().__init__("; ".join(n.message for n in notes if n.level == "error"))


@dataclass
class CompileResult:
    capability: Capability
    yaml: str
    notes: list[Note] = field(default_factory=list)

    @property
    def review_markdown(self) -> str:
        cap = self.capability
        lines = [
            f"# Review: {cap.ref}",
            "",
            f"Compiled from discovery run `{cap.provenance.recorded_from_run}` "
            f"({cap.provenance.model}). Status: **draft**.",
            "",
            "## Checklist",
            "",
            "- [ ] Each step's target describes the control a person would use",
            "- [ ] Every business outcome the caller needs has a detector",
            "- [ ] Detector texts are application messages, not member data",
            "- [ ] Risk and subject are right for this capability",
            f"- [ ] Approve: `uv run cua approve {cap.id} {cap.version} --by <your-name>`",
            "",
            "## Compiler notes",
            "",
        ]
        lines += [f"- {n.line()}" for n in self.notes] or ["- (none)"]
        return "\n".join(lines) + "\n"


# --- helpers ----------------------------------------------------------------------------------


def _slug(text: str, limit: int = 40) -> str:
    s = re.sub(r"[^a-z0-9]+", "_", text.lower()).strip("_")
    s = s[:limit].rstrip("_")
    return s if s and s[0].isalpha() else f"x_{s}" if s else "step"


def _cond(data: Any) -> Condition:
    return _COND.validate_python(data)


def _templatize(text: str, inputs: dict[str, Any]) -> tuple[str, bool]:
    """Replace raw example values and their identifier tags with ``{{inputs.x}}``."""
    changed = False
    for name, value in sorted(inputs.items(), key=lambda kv: -len(str(kv[1]))):
        raw = str(value)
        for needle in (f"[identifier:{stable_tag(raw)}]", raw):
            if needle and needle in text:
                text = text.replace(needle, f"{{{{inputs.{name}}}}}")
                changed = True
    return text, changed


def _describe(step: TraceStep) -> str:
    el = step.element
    target = step.target
    if el is None or target is None:
        return step.tool
    first = target.strategies[0].model_dump()
    if first["by"] == "table_cell":
        col, row, table = first["column"], first["row_match"], first["table_near"]
        return f"{col} of the '{row}' row in the '{table}' table"
    where = f" in '{el.context}'" if el.context else ""
    if el.role == "textbox":
        return f"{el.label or el.name} text box{where}"
    if el.role == "combobox":
        return f"{el.label or el.name} dropdown{where}"
    if el.role == "checkbox":
        return f"'{el.label or el.name}' checkbox{where}"
    if el.role == "button":
        return f"{el.name or el.label} button{where}"
    if el.role == "link":
        return f"'{el.name}' link{where}"
    if el.role == "cell" and el.label:
        return f"value next to '{el.label}'"
    return f"{el.role} '{el.label or el.name}'"


def _step_id(step: TraceStep, taken: set[str]) -> str:
    el = step.element
    label = (el.label or el.name) if el else ""
    base = {
        "fill": f"enter_{_slug(label)}",
        "click": f"click_{_slug(label)}",
        "select": f"choose_{_slug(label)}",
        "check": f"tick_{_slug(label)}",
        "press": f"press_{_slug(str(step.args.get('key', 'key')))}",
        "extract": f"read_{step.output or _slug(label)}",
        "navigate": f"open_{_slug(_last_static_segment(str(step.args.get('route', ''))))}",
    }[step.tool]
    sid, n = base, 2
    while sid in taken:
        sid, n = f"{base}_{n}", n + 1
    taken.add(sid)
    return sid


def _last_static_segment(route: str) -> str:
    parts = [p for p in route.split("/") if p and not p.startswith("{{") and not p.startswith("[")]
    return parts[-1] if parts else "home"


def _scrub_fingerprint(fp: dict[str, Any] | None) -> dict[str, Any] | None:
    if not fp:
        return None
    clean = dict(fp)
    clean["row_context"] = [t for t in fp.get("row_context", ()) if t and not _REDACTED.search(t)]
    for key in ("name", "near_text"):
        if clean.get(key) and _REDACTED.search(str(clean[key])):
            clean[key] = None
    return {k: v for k, v in clean.items() if v not in (None, [], {}, ())}


def _target_key(step: TraceStep) -> str:
    return repr(step.target.strategies[0]) if step.target else ""


def _set_again(step: TraceStep, later: list[TraceStep]) -> bool:
    """The same field is set again before anything is clicked: only the last value matters."""
    for other in later:
        if other.tool in ("click", "navigate", "press"):
            return False
        if other.tool == step.tool and _target_key(other) == _target_key(step):
            return True
    return False


def _kept_steps(trace: DiscoveryTrace, notes: list[Note]) -> list[TraceStep]:
    ok = [s for s in trace.steps if s.status == "ok" and s.tool in ACTIONS]
    dropped = [s for s in trace.steps if s.tool in ACTIONS and s.status != "ok"]
    for s in dropped:
        notes.append(Note("info", f"dropped turn {s.index} ({s.tool}, {s.status}): dead end", None))
    for s in trace.steps:
        if s.tool == "escalate" and s.intervention:
            did = ", ".join(
                f"{a.get('event')} {a.get('label') or a.get('name')!r}" for a in s.human_actions
            )
            notes.append(
                Note(
                    "warn",
                    f"turn {s.index}: an operator stepped in ({did or 'no actions recorded'}); "
                    "those actions are not compiled into steps. Add a step or detector if "
                    "replay needs them",
                )
            )
    kept: list[TraceStep] = []
    for i, s in enumerate(ok):
        if s.tool in ("fill", "select") and _set_again(s, ok[i + 1 :]):
            notes.append(Note("info", f"dropped turn {s.index}: same field set again later"))
            continue
        if s.tool == "extract" and any(
            o.tool == "extract" and o.output == s.output for o in ok[i + 1 :]
        ):
            notes.append(Note("info", f"dropped turn {s.index}: {s.output} read again later"))
            continue
        kept.append(s)
    return kept


# --- the compiler -----------------------------------------------------------------------------


def compile_trace(
    trace: DiscoveryTrace,
    *,
    content_frame: Sequence[FrameRef],
    login: str,
    outcome_traces: Sequence[DiscoveryTrace] = (),
    example_inputs: dict[str, Any] | None = None,
    declared_outcomes: Sequence[OutcomeDecl] = (),
    subject: str | None = None,
    version: str = "1.0.0",
    recorded_by: str | None = None,
) -> CompileResult:
    notes: list[Note] = []
    inputs = example_inputs or {}

    # -- preconditions ---------------------------------------------------------------------
    if trace.status != "succeeded":
        raise CompileError(
            [Note("error", f"trace status is {trace.status!r}; need a succeeded run")]
        )
    unverified = [o for o, ok in trace.verified_outputs.items() if not ok]
    if unverified:
        raise CompileError([Note("error", f"outputs not verified during discovery: {unverified}")])
    for ot in outcome_traces:
        if ot.capability_id != trace.capability_id or ot.app_product != trace.app_product:
            raise CompileError(
                [Note("error", f"outcome run {ot.run_id} is for a different capability or app")]
            )
        if ot.status != "business_outcome" or ot.outcome is None:
            raise CompileError(
                [Note("error", f"run {ot.run_id} did not end in a business outcome")]
            )
    if trace.provider == "scripted":
        notes.append(Note("warn", "compiled from a scripted run, not a real model run"))

    # -- outcomes -> detectors -------------------------------------------------------------
    declared = {o.code: o for o in (*trace.declared_outcomes, *declared_outcomes)}
    detectors: list[dict[str, Any]] = []
    outcome_ids: list[str] = []
    for ot in outcome_traces:
        assert ot.outcome is not None
        code, quote = ot.outcome.code, ot.outcome.evidence_text.strip()
        if not re.match(OUTCOME_CODE, code):
            raise CompileError([Note("error", f"bad outcome code {code!r} in {ot.run_id}")])
        if _REDACTED.search(quote):
            notes.append(
                Note(
                    "error",
                    f"{code}: quoted text contains data ({quote!r}); write this detector by hand",
                )
            )
            continue
        det_id = _slug(code)
        if det_id in outcome_ids:
            notes.append(Note("info", f"{code}: seen in more than one run; kept the first quote"))
            continue
        detectors.append({"id": det_id, "when": {"text_visible": quote}, "then": {"outcome": code}})
        outcome_ids.append(det_id)
        if code not in declared:
            declared[code] = OutcomeDecl(code=code, description="Observed during discovery.")
            notes.append(
                Note("warn", f"{code} was not declared in the spec; confirm its description")
            )
        notes.append(
            Note(
                "info",
                f"{code}: detector from run {ot.run_id} quoting {quote!r}; "
                "confirm it is an application message, not member data",
            )
        )
    for code in declared:
        if _slug(code) not in outcome_ids:
            notes.append(
                Note(
                    "warn",
                    f"{code} is declared but has no detector yet: run discovery for a "
                    "case that produces it, or add a detector by hand",
                )
            )

    # -- steps ---------------------------------------------------------------------------
    kept = _kept_steps(trace, notes)
    if not kept:
        raise CompileError([Note("error", "no successful actions in the trace")])
    taken: set[str] = set()
    steps: list[dict[str, Any]] = []
    first_title = trace.steps[0].before.title if trace.steps else ""
    open_id = f"open_{_slug(_last_static_segment(trace.start_route))}"
    taken.add(open_id)
    steps.append(
        {
            "id": open_id,
            "action": "navigate",
            "description": f"Start at {trace.start_route} (already signed on)",
            "frame": [f.model_dump(exclude_none=True) for f in content_frame],
            "route": trace.start_route,
            **({"postcondition": {"title_contains": first_title}} if first_title else {}),
        }
    )
    risks: list[RiskClass] = [RiskClass.READ]
    last_title = first_title

    for s in kept:
        # Interstitial: clicking a control inside a dialog becomes a recovery, not a step.
        if s.tool == "click" and s.element and s.element.dialog:
            if s.target is None:
                raise CompileError(
                    [Note("error", "dialog button without a verified locator", f"turn {s.index}")]
                )
            rec_id = f"dismiss_{_slug(s.element.dialog)}"
            if all(d["id"] != rec_id for d in detectors):
                detectors.append(
                    {
                        "id": rec_id,
                        "when": {"dialog_title": s.element.dialog},
                        "then": {
                            "recover": "click",
                            "max": 2,
                            "target": _target_dict(
                                s, f"{s.element.name} button on '{s.element.dialog}'"
                            ),
                        },
                    }
                )
                notes.append(
                    Note(
                        "info",
                        f"'{s.element.dialog}' dialog becomes a recovery (click {s.element.name})",
                    )
                )
            if s.after:
                last_title = s.after.title
            continue

        sid = _step_id(s, taken)
        step: dict[str, Any] = {"id": sid, "action": s.tool}
        risk = s.policy.effective_risk if s.policy else RiskClass.READ
        if s.tool == "extract":
            risk = RiskClass.READ
        risks.append(risk)

        if s.tool == "navigate":
            route, changed = _templatize(str(s.args.get("route", "")), inputs)
            if _REDACTED.search(route):
                raise CompileError(
                    [Note("error", f"route {route!r} contains data with no matching input", sid)]
                )
            step.update(
                description=f"Open {route}",
                frame=[f.model_dump(exclude_none=True) for f in content_frame],
                route=route,
            )
            if changed:
                notes.append(Note("info", "route parameterized from an example input", sid))
        else:
            if s.target is None:
                raise CompileError(
                    [
                        Note(
                            "error",
                            f"no unique locator was found during discovery ({s.target_note})",
                            sid,
                        )
                    ]
                )
            step["target"] = _target_dict(s, _describe(s))

        if s.tool == "fill":
            value, changed = _templatize(str(s.args.get("value", "")), inputs)
            if changed:
                notes.append(Note("info", "literal replaced by an input placeholder", sid))
            elif "{{" not in value:
                notes.append(
                    Note("warn", f"literal value {value!r}: confirm it is a constant", sid)
                )
            step["value"] = value
        elif s.tool == "select":
            option = str(s.args.get("option", ""))
            if _REDACTED.search(option):
                raise CompileError(
                    [
                        Note(
                            "error",
                            f"option {option!r} depends on member data; it needs an input",
                            sid,
                        )
                    ]
                )
            option, changed = _templatize(option, inputs)
            if changed:
                notes.append(Note("info", "option replaced by an input placeholder", sid))
            step["option"] = option
        elif s.tool == "press":
            step["key"] = str(s.args.get("key", "Enter"))
        elif s.tool == "extract":
            step["output"] = s.output
            step["parse"] = s.parser or "text"

        if risk != RiskClass.READ:
            step["risk"] = risk.value
        if risk == RiskClass.IRREVERSIBLE:
            step["requires_confirmation"] = True
        if s.approved_by:
            notes.append(
                Note(
                    "info",
                    f"irreversible action approved by {s.approved_by} during discovery "
                    f"({s.intervention}); every replay needs its own confirmation",
                    sid,
                )
            )
        if s.actor != "agent":
            did = ", ".join(
                f"{a.get('event')} {a.get('label') or a.get('name')!r}" for a in s.human_actions
            )
            notes.append(
                Note(
                    "warn",
                    f"performed by {s.actor} during discovery ({did or 'no actions recorded'}); "
                    "the locator is the one the agent proposed. Confirm it is the control used",
                    sid,
                )
            )

        page_changed = s.after is not None and (
            s.after.title != s.before.title or s.after.route != s.before.route
        )
        if page_changed and s.tool != "extract":
            assert s.after is not None
            if s.tool == "navigate":
                step["postcondition"] = {"title_contains": s.after.title}
            else:
                until = [{"title_contains": s.after.title}, *({"detector": d} for d in outcome_ids)]
                step["wait"] = {
                    "until": {"any_of": until} if len(until) > 1 else until[0],
                    "timeout_ms": DEFAULT_WAIT_MS,
                }
            last_title = s.after.title
        steps.append(step)

    # -- contract and identity ---------------------------------------------------------------
    subj = trace.subject or subject
    if subj is None:
        ids = [n for n, p in trace.inputs.properties.items() if p.sensitivity.value == "identifier"]
        if len(ids) == 1:
            subj = f"inputs.{ids[0]}"
            notes.append(
                Note("warn", f"subject guessed as {subj} (only identifier input); confirm")
            )
    app_v = parse_version(trace.app_version)
    success_parts: list[dict[str, Any]] = []
    if last_title:
        success_parts.append({"title_contains": last_title})
    success_parts += [{"output_present": o} for o in trace.outputs.required]
    data: dict[str, Any] = {
        "schema_version": 1,
        "id": trace.capability_id,
        "version": version,
        "status": "draft",
        "recorded_by": recorded_by or f"discovery:{trace.provider}/{trace.model}",
        "provenance": {
            "origin": "discovery",
            "recorded_from_run": trace.run_id,
            "model": f"{trace.provider}/{trace.model}",
            "recorded_on_tenant": trace.tenant,
            "recorded_at": trace.started_at.isoformat(),
            "notes": (
                f"Compiled from {1 + len(outcome_traces)} discovery run(s): "
                + ", ".join([trace.run_id, *(o.run_id for o in outcome_traces)])
            ),
        },
        "app": {
            "product": trace.app_product,
            "versions": f">={app_v.major}.{app_v.minor} <{app_v.major + 1}",
        },
        "description": trace.description,
        "risk": max_risk(*risks).value,
        **({"subject": subj} if subj else {}),
        "inputs": trace.inputs.json_schema(),
        "outputs": trace.outputs.json_schema(),
        "outcomes": [o.model_dump() for o in declared.values()],
        "requires": [login],
        "steps": steps,
        "detectors": detectors,
        "success": {"all_of": success_parts} if len(success_parts) > 1 else success_parts[0],
    }
    try:
        cap = Capability.model_validate(data)
    except ValueError as exc:
        raise CompileError([*notes, Note("error", f"compiled artifact is invalid: {exc}")]) from exc

    errors = [n for n in notes if n.level == "error"]
    if errors:
        raise CompileError(notes)

    header = [
        f"{cap.id} {cap.version}, compiled by `cua compile` from discovery run {trace.run_id}",
        f"(model {trace.provider}/{trace.model}). Behavior hash: {compute_hash(cap)}",
        "Drafts are reviewed (see the matching .review.md), may be edited, then `cua approve`d.",
    ]
    text = dump(
        readable_dict(cap),
        header=header,
        gaps_before=("description", "inputs", "outcomes", "steps", "detectors", "success"),
    )
    return CompileResult(cap, text, notes)


def _target_dict(s: TraceStep, description: str) -> dict[str, Any]:
    assert s.target is not None
    target: dict[str, Any] = {
        "description": description,
        "frame": [f.model_dump(exclude_none=True) for f in s.target.frame],
        "strategies": [st.model_dump(mode="json") for st in s.target.strategies],
    }
    fp = _scrub_fingerprint(s.fingerprint.model_dump(mode="json") if s.fingerprint else None)
    if fp:
        target["fingerprint"] = fp
    return target


# Values that only add noise for a reviewer. Pruned by (key, value), never by "is a default":
# a discriminator such as ``by: label`` is a default too and must stay.
_NOISE_ANYWHERE = {("exact", True), ("row_match_mode", "exact"), ("requires_confirmation", False)}
_NOISE_IN_STEP = {("risk", "read")}
_EMPTY_TOP = ("overrides", "detectors", "requires", "outcomes")


def _prune(value: Any, in_step: bool = False) -> Any:
    if isinstance(value, dict):
        out = {}
        for k, v in value.items():
            scalar = isinstance(v, str | int | float | bool)
            if scalar and ((k, v) in _NOISE_ANYWHERE or (in_step and (k, v) in _NOISE_IN_STEP)):
                continue
            if k in ("frame", "row_context", "attributes") and v in ([], {}):
                continue
            out[k] = _prune(v, in_step)
        return out
    if isinstance(value, list):
        return [_prune(v, in_step) for v in value]
    return value


def readable_dict(cap: Capability) -> dict[str, Any]:
    """The capability as a reviewer should read it: no default noise, empty sections omitted."""
    data = cap.model_dump(mode="json", by_alias=True, exclude_none=True)
    data["steps"] = [_prune(s, in_step=True) for s in data["steps"]]
    data["detectors"] = [_prune(d) for d in data["detectors"]]
    for key in _EMPTY_TOP:
        if not data.get(key):
            data.pop(key, None)
    return data
