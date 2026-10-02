"""The discovery trace: what the agent did, captured as data the compiler can use.

The trace is deliberately *not* the model transcript. For every action it records what the
surface verified at that moment: the element as a person would describe it, locator strategies
proven to hit that same element, a fingerprint, the policy decision, and the page before/after.
The compiler (Day 5) turns this into a capability without ever re-reading model text.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from cua.core.artifact import ObjectSchema, OutcomeDecl, RiskClass
from cua.core.targets import Fingerprint, FrameRef, TargetSpec

StepStatus = Literal["ok", "invalid", "denied", "escalated", "error"]
RunStatus = Literal["succeeded", "business_outcome", "escalated", "failed", "budget_exhausted"]


class _M(BaseModel):
    model_config = ConfigDict(extra="forbid")


class PageRef(_M):
    route: str
    title: str


class ElementInfo(_M):
    role: str
    label: str = ""
    name: str = ""
    tag: str = ""
    row: tuple[str, ...] = ()
    column: str = ""
    context: str = ""
    dialog: str = ""
    frame: tuple[FrameRef, ...] = ()


class PolicyRecord(_M):
    verdict: Literal["allow", "deny", "escalate"]
    effective_risk: RiskClass
    reason: str


class LLMCallRecord(_M):
    provider: str
    model: str
    request_id: str | None = None
    input_tokens: int | None = None
    output_tokens: int | None = None
    latency_ms: int = 0
    retries: int = 0


class TraceStep(_M):
    index: int
    tool: str
    args: dict[str, Any]
    rationale: str = ""
    ref: str | None = None
    element: ElementInfo | None = None
    target: TargetSpec | None = None
    target_note: str | None = None
    fingerprint: Fingerprint | None = None
    before: PageRef
    after: PageRef | None = None
    policy: PolicyRecord | None = None
    status: StepStatus
    error: str | None = None
    output: str | None = None
    parser: str | None = None
    value: Any = None
    llm: LLMCallRecord | None = None
    t_ms: int = 0
    # Human-in-the-loop: who performed the step ("agent" or "human:<operator>"), who approved an
    # irreversible action, and what a person did in the window while they held the session.
    actor: str = "agent"
    approved_by: str | None = None
    intervention: str | None = None
    human_actions: list[dict[str, Any]] = Field(default_factory=list)


class OutcomeRecord(_M):
    code: str
    detail: str
    evidence_text: str
    page: PageRef


class DiscoveryTrace(_M):
    schema_version: Literal[1] = 1
    run_id: str
    capability_id: str
    description: str
    goal: str
    tenant: str
    app_product: str
    app_version: str
    login: str
    provider: str
    model: str
    started_at: datetime
    finished_at: datetime | None = None
    status: RunStatus = "failed"
    reason: str = ""
    risk_ceiling: RiskClass
    inputs: ObjectSchema
    outputs: ObjectSchema
    declared_outcomes: list[OutcomeDecl] = Field(default_factory=list)
    subject: str | None = None
    start_route: str
    steps: list[TraceStep] = Field(default_factory=list)
    extracted: dict[str, Any] = Field(default_factory=dict)
    verified_outputs: dict[str, bool] = Field(default_factory=dict)
    outcome: OutcomeRecord | None = None
    llm_calls: int = 0
    input_tokens: int = 0
    output_tokens: int = 0
