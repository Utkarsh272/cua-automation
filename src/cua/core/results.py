"""The replay result contract.

Every replay returns exactly one of four kinds, discriminated by ``kind``:

* ``success``: the goal was reached; outputs are schema-validated.
* ``business_outcome``: a legitimate, *declared* answer that is not the happy path
  ("no such member"). It is not an error and the caller should not retry.
* ``failure``: something broke. It says where, what was expected, what was observed,
  whether the caller may retry, and whether the target system may already have changed.
* ``needs_human``: the run is paused on an intervention request.

Recoverable conditions (a notice dialog, a re-login) never surface as failures; they are
handled inside the engine and reported as ``recoveries`` on the result.
"""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum
from typing import Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, Field, TypeAdapter, model_validator

OUTCOME_CODE = r"^[A-Z][A-Z0-9_]*$"

# Declared by every capability implicitly: raised before a browser is opened.
BUILTIN_OUTCOMES: dict[str, str] = {
    "INVALID_INPUT": "Inputs failed the capability's input schema.",
}


class FailureCode(StrEnum):
    TARGET_NOT_FOUND = "TARGET_NOT_FOUND"
    TARGET_AMBIGUOUS = "TARGET_AMBIGUOUS"
    UNEXPECTED_STATE = "UNEXPECTED_STATE"
    TIMEOUT = "TIMEOUT"
    SESSION_LOST = "SESSION_LOST"
    LOGIN_FAILED = "LOGIN_FAILED"
    ACCOUNT_LOCKED = "ACCOUNT_LOCKED"
    APP_ERROR = "APP_ERROR"
    APP_UNAVAILABLE = "APP_UNAVAILABLE"
    POLICY_VIOLATION = "POLICY_VIOLATION"
    NOT_AUTHORIZED = "NOT_AUTHORIZED"
    NOT_APPROVED = "NOT_APPROVED"
    ARTIFACT_INVALID = "ARTIFACT_INVALID"
    OUTPUT_INVALID = "OUTPUT_INVALID"
    OPERATOR_TIMEOUT = "OPERATOR_TIMEOUT"
    OPERATOR_ABORTED = "OPERATOR_ABORTED"
    INTERNAL_ERROR = "INTERNAL_ERROR"


# Whether retrying the *whole capability later* can reasonably succeed, before side effects
# are considered. Anything that may have changed the target system is never retryable.
RETRYABLE_BY_DEFAULT: frozenset[FailureCode] = frozenset(
    {FailureCode.TIMEOUT, FailureCode.SESSION_LOST, FailureCode.APP_UNAVAILABLE}
)

SideEffects = Literal["none", "possible", "committed"]


class _Base(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    run_id: str
    capability: str
    version: str
    tenant: str
    started_at: datetime
    duration_ms: int = Field(ge=0)
    llm_calls: int = Field(
        default=0, ge=0, description="Always 0 for replay; recorded to prove it."
    )


class RecoveryEvent(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    detector: str
    action: str
    at_step: str
    attempt: int = Field(ge=1)


class Success(_Base):
    kind: Literal["success"] = "success"
    outputs: dict[str, Any]
    degraded: bool = Field(
        default=False, description="A fallback locator strategy won somewhere; early drift signal."
    )
    recoveries: tuple[RecoveryEvent, ...] = ()


class BusinessOutcome(_Base):
    kind: Literal["business_outcome"] = "business_outcome"
    code: str = Field(pattern=OUTCOME_CODE)
    detail: str
    at_step: str | None = None


class Failure(_Base):
    kind: Literal["failure"] = "failure"
    code: FailureCode
    at_step: str | None
    expected: str
    observed: str
    retryable: bool
    side_effects: SideEffects = "none"
    evidence: tuple[str, ...] = ()
    recoveries: tuple[RecoveryEvent, ...] = ()

    @model_validator(mode="after")
    def _no_retry_after_side_effects(self) -> Failure:
        if self.side_effects != "none" and self.retryable:
            raise ValueError(
                "a failure that may have changed the target system cannot be retryable"
            )
        return self


class NeedsHuman(_Base):
    kind: Literal["needs_human"] = "needs_human"
    intervention_id: str
    reason: str
    at_step: str | None = None


ReplayResult = Annotated[
    Success | BusinessOutcome | Failure | NeedsHuman, Field(discriminator="kind")
]

_adapter: TypeAdapter[ReplayResult] = TypeAdapter(ReplayResult)


def parse_result(data: Any) -> ReplayResult:
    return _adapter.validate_python(data)


def result_json(result: ReplayResult) -> str:
    return _adapter.dump_json(result, indent=2).decode()


def make_failure(
    *,
    code: FailureCode,
    at_step: str | None,
    expected: str,
    observed: str,
    side_effects: SideEffects = "none",
    **meta: Any,
) -> Failure:
    """Build a failure with the default retry rule applied (never retryable after side effects)."""
    retryable = code in RETRYABLE_BY_DEFAULT and side_effects == "none"
    return Failure(
        code=code,
        at_step=at_step,
        expected=expected,
        observed=observed,
        retryable=retryable,
        side_effects=side_effects,
        **meta,
    )
