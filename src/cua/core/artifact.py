"""The capability artifact: what a successful discovery run is compiled into.

A capability is a **contract first and a step list second**.

* The contract (id, version, description, inputs, outputs, declared outcomes, risk, subject) is
  what a calling agent reads. Inputs and outputs are JSON Schema, so the same contract is a
  valid LLM tool definition (see :func:`to_tool_definition`).
* The steps (targets, waits, postconditions), detectors and success checkpoint are what the
  replay engine reads.

Both halves live in one immutable, content-hashed file so they cannot drift apart. Invariants
that would make a replay unsafe or ambiguous are rejected at load time, not discovered at run
time (see :meth:`Capability._check_consistency`).
"""

from __future__ import annotations

import hashlib
import io
import json
import re
from datetime import datetime
from enum import StrEnum
from pathlib import Path
from typing import Annotated, Any, Literal

import jsonschema
from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator, model_validator
from ruamel.yaml import YAML

from .conditions import Condition, referenced_detectors, referenced_outputs
from .redact import find_sensitive
from .results import BUILTIN_OUTCOMES, OUTCOME_CODE, FailureCode
from .semver import is_strict_version, parse_requirement, satisfies
from .targets import FrameRef, TargetSpec

SNAKE = r"^[a-z][a-z0-9_]*$"
TEMPLATE = re.compile(r"\{\{\s*([^}]+?)\s*\}\}")
INPUT_REF = re.compile(r"^inputs\.([a-z][a-z0-9_]*)$")
SECRET_REF = re.compile(r"^secrets\.([a-z][a-z0-9_]*)\.([a-z][a-z0-9_]*)$")

# Fields that describe approval, not behavior. They are excluded from the content hash so that
# approving an artifact does not change its identity, while any behavioral edit does.
HASH_EXCLUDED = {"content_hash", "status", "approved_by", "approved_at"}


class ArtifactError(ValueError):
    """The artifact is invalid or has been tampered with."""


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, populate_by_name=True)


# --- enums ------------------------------------------------------------------------------------


class RiskClass(StrEnum):
    READ = "read"
    REVERSIBLE_WRITE = "reversible_write"
    IRREVERSIBLE = "irreversible"

    @property
    def rank(self) -> int:
        return _RISK_RANK[self]


_RISK_RANK = {RiskClass.READ: 0, RiskClass.REVERSIBLE_WRITE: 1, RiskClass.IRREVERSIBLE: 2}


def max_risk(*risks: RiskClass) -> RiskClass:
    return max(risks, key=lambda r: r.rank, default=RiskClass.READ)


class Sensitivity(StrEnum):
    PUBLIC = "public"
    IDENTIFIER = "identifier"
    PII = "pii"
    FINANCIAL = "financial"
    SECRET = "secret"


class ActionKind(StrEnum):
    NAVIGATE = "navigate"
    CLICK = "click"
    FILL = "fill"
    SELECT = "select"
    CHECK = "check"
    PRESS = "press"
    EXTRACT = "extract"


Parser = Literal["text", "currency_usd", "integer", "date_mdy"]


# --- contract ---------------------------------------------------------------------------------


class PropertySchema(BaseModel):
    """One JSON Schema property. Any JSON Schema keyword is allowed; two are required."""

    model_config = ConfigDict(extra="allow", frozen=True, populate_by_name=True)

    type: Literal["string", "number", "integer", "boolean"]
    description: str | None = None
    sensitivity: Sensitivity = Field(alias="x-sensitivity")


class ObjectSchema(_Strict):
    type: Literal["object"] = "object"
    required: tuple[str, ...] = ()
    properties: dict[str, PropertySchema] = Field(default_factory=dict)
    additionalProperties: bool = False

    @model_validator(mode="after")
    def _required_declared(self) -> ObjectSchema:
        missing = set(self.required) - set(self.properties)
        if missing:
            raise ValueError(f"required fields not declared in properties: {sorted(missing)}")
        for name in self.properties:
            if not re.match(SNAKE, name):
                raise ValueError(f"field names must be snake_case: {name!r}")
        return self

    def json_schema(self, *, include_extensions: bool = True) -> dict[str, Any]:
        data = self.model_dump(mode="json", by_alias=True, exclude_none=True)
        if not include_extensions:
            for prop in data["properties"].values():
                for key in [k for k in prop if k.startswith("x-")]:
                    del prop[key]
        return data


class OutcomeDecl(_Strict):
    code: str = Field(pattern=OUTCOME_CODE)
    description: str


class AppRef(_Strict):
    """The vendor product and version range the artifact is valid for. Never a tenant."""

    product: str = Field(pattern=r"^[a-z][a-z0-9-]*$")
    versions: str = "*"

    @field_validator("versions")
    @classmethod
    def _valid_range(cls, v: str) -> str:
        satisfies("0.0.0", v)  # raises on malformed constraints
        return v


class Provenance(_Strict):
    origin: Literal["discovery", "hand_written", "human"]
    recorded_from_run: str | None = None
    model: str | None = None
    recorded_on_tenant: str | None = None
    recorded_at: datetime | None = None
    notes: str | None = None


# --- steps and detectors ----------------------------------------------------------------------


class Wait(_Strict):
    until: Condition
    timeout_ms: int = Field(default=8000, ge=100, le=120_000)


class Step(_Strict):
    id: str = Field(pattern=SNAKE)
    action: ActionKind
    description: str | None = None
    target: TargetSpec | None = None
    frame: tuple[FrameRef, ...] = Field(
        default=(), description="Frame to navigate (navigate only)."
    )
    route: str | None = None
    value: str | None = None
    option: str | None = None
    key: str | None = None
    output: str | None = None
    parse: Parser | None = None
    wait: Wait | None = None
    postcondition: Condition | None = None
    risk: RiskClass = RiskClass.READ
    requires_confirmation: bool = False
    safe_to_repeat: bool | None = Field(
        default=None,
        description="May replay resume from here after a re-login? Defaults to risk == read.",
    )
    idempotency_check: Condition | None = Field(
        default=None,
        description="A read that proves the effect already happened, so a timed-out write "
        "can be resolved without repeating it.",
    )
    origin: Literal["agent", "human", "hand_written"] | None = None

    @property
    def repeatable(self) -> bool:
        return self.risk == RiskClass.READ if self.safe_to_repeat is None else self.safe_to_repeat

    @model_validator(mode="after")
    def _shape(self) -> Step:
        a = self.action
        needs_target = {
            ActionKind.CLICK,
            ActionKind.FILL,
            ActionKind.SELECT,
            ActionKind.CHECK,
            ActionKind.EXTRACT,
        }
        if a == ActionKind.NAVIGATE:
            if not self.route or self.target is not None:
                raise ValueError(f"step {self.id}: navigate needs a route and no target")
        elif a in needs_target and self.target is None:
            raise ValueError(f"step {self.id}: {a.value} needs a target")
        if self.frame and a != ActionKind.NAVIGATE:
            raise ValueError(f"step {self.id}: step-level frame is only for navigate")
        if a == ActionKind.FILL and self.value is None:
            raise ValueError(f"step {self.id}: fill needs a value")
        if a == ActionKind.SELECT and self.option is None:
            raise ValueError(f"step {self.id}: select needs an option")
        if a == ActionKind.PRESS and not self.key:
            raise ValueError(f"step {self.id}: press needs a key")
        if (a == ActionKind.EXTRACT) != (self.output is not None):
            raise ValueError(f"step {self.id}: 'output' is required for, and only for, extract")
        if self.parse is not None and a != ActionKind.EXTRACT:
            raise ValueError(f"step {self.id}: 'parse' is only for extract")
        if a == ActionKind.EXTRACT and self.risk != RiskClass.READ:
            raise ValueError(f"step {self.id}: extract steps are always read")
        if self.risk == RiskClass.IRREVERSIBLE and not self.requires_confirmation:
            raise ValueError(
                f"step {self.id}: irreversible steps must set requires_confirmation: true"
            )
        if self.requires_confirmation and self.risk != RiskClass.IRREVERSIBLE:
            raise ValueError(f"step {self.id}: only irreversible steps require confirmation")
        if self.safe_to_repeat and self.risk == RiskClass.IRREVERSIBLE:
            raise ValueError(f"step {self.id}: an irreversible step is never safe to repeat")
        return self

    def templated_fields(self) -> list[str]:
        return [v for v in (self.value, self.option, self.route, self.key) if v]


class OutcomeThen(_Strict):
    outcome: str = Field(pattern=OUTCOME_CODE)


class RecoverThen(_Strict):
    recover: Literal["click", "relogin"]
    target: TargetSpec | None = None
    max: int = Field(default=2, ge=1, le=5)

    @model_validator(mode="after")
    def _target_for_click(self) -> RecoverThen:
        if (self.recover == "click") != (self.target is not None):
            raise ValueError("recover: click needs a target; relogin takes none")
        return self


class EscalateThen(_Strict):
    escalate: str = Field(min_length=3, description="Reason shown to the operator.")


class FailThen(_Strict):
    fail: FailureCode


DetectorThen = Annotated[
    OutcomeThen | RecoverThen | EscalateThen | FailThen, Field(union_mode="smart")
]


class Detector(_Strict):
    """A page signature and what it means. Checked before and after every step.

    Precedence when several fire: fail/escalate, then declared outcomes, then recoveries,
    then the step's own postcondition. A "No member found" banner must beat a timeout.
    """

    id: str = Field(pattern=SNAKE)
    when: Condition
    then: DetectorThen


# --- tenant overrides -------------------------------------------------------------------------


class SetOp(_Strict):
    """Replace one value, addressed by step or detector id: ``steps.enter_member_id.target...``."""

    op: Literal["set"] = "set"
    path: str = Field(pattern=r"^(steps|detectors)\.[a-z][a-z0-9_]*(\.[A-Za-z0-9_]+|\[\d+\])*$")
    value: Any


class InsertStepOp(_Strict):
    op: Literal["insert_step"] = "insert_step"
    after: str = Field(pattern=SNAKE)
    step: Step


OverrideOp = Annotated[SetOp | InsertStepOp, Field(discriminator="op")]


# --- the capability ---------------------------------------------------------------------------


class Capability(_Strict):
    schema_version: Literal[1] = 1
    id: str = Field(pattern=SNAKE)
    version: str
    content_hash: str | None = None
    status: Literal["draft", "approved", "deprecated"] = "draft"
    recorded_by: str | None = None
    approved_by: str | None = None
    approved_at: datetime | None = None
    provenance: Provenance
    app: AppRef

    description: str = Field(min_length=10)
    risk: RiskClass
    subject: str | None = Field(
        default=None,
        description="Input that identifies whose data this touches, e.g. inputs.member_id. "
        "Enables subject binding: the caller must be acting for that subject.",
    )
    inputs: ObjectSchema = Field(default_factory=ObjectSchema)
    outputs: ObjectSchema = Field(default_factory=ObjectSchema)
    outcomes: tuple[OutcomeDecl, ...] = ()
    requires: tuple[str, ...] = ()

    steps: tuple[Step, ...] = Field(min_length=1)
    detectors: tuple[Detector, ...] = ()
    success: Condition
    overrides: dict[str, tuple[OverrideOp, ...]] = Field(default_factory=dict)

    @field_validator("version")
    @classmethod
    def _semver(cls, v: str) -> str:
        if not is_strict_version(v):
            raise ValueError("version must be MAJOR.MINOR.PATCH")
        return v

    @field_validator("requires")
    @classmethod
    def _requires(cls, v: tuple[str, ...]) -> tuple[str, ...]:
        for req in v:
            parse_requirement(req)
        return v

    @model_validator(mode="after")
    def _check_consistency(self) -> Capability:
        errors: list[str] = []
        step_ids = [s.id for s in self.steps]
        det_ids = [d.id for d in self.detectors]
        outcome_codes = {o.code for o in self.outcomes} | set(BUILTIN_OUTCOMES)

        for label, ids in (("step", step_ids), ("detector", det_ids)):
            dupes = {i for i in ids if ids.count(i) > 1}
            if dupes:
                errors.append(f"duplicate {label} ids: {sorted(dupes)}")
        codes = [o.code for o in self.outcomes]
        if len(codes) != len(set(codes)):
            errors.append("duplicate outcome codes")

        # risk: the capability declares at least the riskiest step
        worst = max_risk(*(s.risk for s in self.steps))
        if worst.rank > self.risk.rank:
            errors.append(
                f"capability risk {self.risk.value} is below its riskiest step ({worst.value})"
            )

        # templates may only reference declared inputs or secrets
        for step in self.steps:
            for text in step.templated_fields():
                for ref in TEMPLATE.findall(text):
                    m = INPUT_REF.match(ref)
                    if m and m.group(1) not in self.inputs.properties:
                        errors.append(f"step {step.id}: unknown input {{{{{ref}}}}}")
                    elif not m and not SECRET_REF.match(ref):
                        errors.append(f"step {step.id}: bad template {{{{{ref}}}}}")
                # no concrete sensitive literals: they belong in inputs or secrets
                literal = TEMPLATE.sub("", text)
                found = find_sensitive(literal)
                if found:
                    errors.append(
                        f"step {step.id}: sensitive literal in artifact ({', '.join(found)})"
                    )

        # outputs: every extract is declared; every required output is produced
        extracted = {s.output for s in self.steps if s.output}
        undeclared = extracted - set(self.outputs.properties)
        if undeclared:
            errors.append(f"extract steps produce undeclared outputs: {sorted(undeclared)}")
        never = set(self.outputs.required) - extracted
        if never:
            errors.append(f"required outputs never extracted: {sorted(never)}")

        # every referenced detector/output exists; every detector outcome is declared
        conds: list[Condition] = [self.success]
        for s in self.steps:
            conds += [c for c in (s.postcondition, s.idempotency_check) if c is not None]
            if s.wait:
                conds.append(s.wait.until)
        conds += [d.when for d in self.detectors]
        for c in conds:
            for d in referenced_detectors(c) - set(det_ids):
                errors.append(f"condition references unknown detector {d!r}")
            for o in referenced_outputs(c) - set(self.outputs.properties):
                errors.append(f"condition references unknown output {o!r}")
        for det in self.detectors:
            if isinstance(det.then, OutcomeThen) and det.then.outcome not in outcome_codes:
                errors.append(f"detector {det.id} returns undeclared outcome {det.then.outcome}")
            if referenced_detectors(det.when):
                errors.append(f"detector {det.id} cannot depend on other detectors")

        # subject binding refers to a declared input
        if self.subject is not None:
            m = INPUT_REF.match(self.subject)
            if not m or m.group(1) not in self.inputs.properties:
                errors.append(f"subject must be inputs.<declared input>, got {self.subject!r}")

        # overrides address real steps/detectors
        for tenant, ops in self.overrides.items():
            if not re.match(r"^[a-z][a-z0-9-]*$", tenant):
                errors.append(f"override key must be a tenant id, got {tenant!r}")
            known = set(step_ids)
            for op in ops:
                if isinstance(op, InsertStepOp):
                    if op.after not in known:
                        errors.append(f"override {tenant}: insert after unknown step {op.after!r}")
                    known.add(op.step.id)
                else:
                    kind, ident = op.path.split(".")[:2]
                    pool = known if kind == "steps" else set(det_ids)
                    ident = ident.split("[")[0]
                    if ident not in pool:
                        errors.append(f"override {tenant}: unknown {kind[:-1]} {ident!r}")

        if self.status == "approved" and not self.approved_by:
            errors.append("approved artifacts must name approved_by")

        if errors:
            raise ValueError("; ".join(errors))
        return self

    # --- derived views ----------------------------------------------------------------------

    @property
    def ref(self) -> str:
        return f"{self.id}@{self.version}"

    def sensitivities(self) -> dict[str, str]:
        out = {k: v.sensitivity.value for k, v in self.inputs.properties.items()}
        out.update({k: v.sensitivity.value for k, v in self.outputs.properties.items()})
        return out

    def step(self, step_id: str) -> Step:
        for s in self.steps:
            if s.id == step_id:
                return s
        raise KeyError(step_id)


# --- hashing, IO, input validation ------------------------------------------------------------


def canonical_json(cap: Capability) -> str:
    data = cap.model_dump(mode="json", by_alias=True, exclude_none=True, exclude=HASH_EXCLUDED)
    return json.dumps(data, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def compute_hash(cap: Capability) -> str:
    return "sha256:" + hashlib.sha256(canonical_json(cap).encode()).hexdigest()


def with_hash(cap: Capability) -> Capability:
    return cap.model_copy(update={"content_hash": compute_hash(cap)})


def _yaml() -> YAML:
    y = YAML(typ="safe")
    y.default_flow_style = False
    return y


def parse_capability(data: Any, *, verify_hash: bool = True) -> Capability:
    try:
        cap = Capability.model_validate(data)
    except ValidationError as exc:
        raise ArtifactError(str(exc)) from exc
    if cap.status == "approved" and not cap.content_hash:
        raise ArtifactError(f"{cap.ref}: approved artifacts must carry a content_hash")
    if verify_hash and cap.content_hash and cap.content_hash != compute_hash(cap):
        raise ArtifactError(
            f"{cap.ref}: content_hash mismatch; the artifact was changed outside the approval flow"
        )
    return cap


def load_capability(path: str | Path, *, verify_hash: bool = True) -> Capability:
    with open(path, encoding="utf-8") as fh:
        data = _yaml().load(fh)
    return parse_capability(data, verify_hash=verify_hash)


def dump_capability(cap: Capability) -> str:
    buf = io.StringIO()
    _yaml().dump(with_hash(cap).model_dump(mode="json", by_alias=True, exclude_none=True), buf)
    return buf.getvalue()


def validate_against(schema: ObjectSchema, data: dict[str, Any]) -> list[str]:
    """JSON Schema errors for ``data``, as readable strings. Empty means valid."""
    validator = jsonschema.Draft202012Validator(schema.json_schema(include_extensions=False))
    return [
        f"{'.'.join(str(p) for p in e.absolute_path) or '(root)'}: {e.message}"
        for e in sorted(validator.iter_errors(data), key=lambda e: list(e.absolute_path))
    ]


def validate_inputs(cap: Capability, inputs: dict[str, Any]) -> list[str]:
    return validate_against(cap.inputs, inputs)


def validate_outputs(cap: Capability, outputs: dict[str, Any]) -> list[str]:
    return validate_against(cap.outputs, outputs)


def to_tool_definition(cap: Capability) -> dict[str, Any]:
    """The capability as an agent tool (Anthropic tool-use format)."""
    outcomes = "; ".join(f"{o.code} ({o.description.rstrip('.')})" for o in cap.outcomes)
    outputs = ", ".join(cap.outputs.properties)
    description = cap.description.strip()
    if outputs:
        description += f" Returns: {outputs}."
    if outcomes:
        description += f" Possible business outcomes: {outcomes}."
    if cap.risk != RiskClass.READ:
        description += f" Risk: {cap.risk.value}; requires write scope."
    return {
        "name": cap.id,
        "description": description,
        "input_schema": cap.inputs.json_schema(include_extensions=False),
    }


def capability_json_schema() -> dict[str, Any]:
    return Capability.model_json_schema(by_alias=True)
