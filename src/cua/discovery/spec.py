"""What a person asks discovery to learn.

The split of responsibilities is deliberate:

* A **person** declares the contract: the goal, the typed inputs and outputs (with their
  sensitivity), and the highest risk the agent may take while exploring.
* The **model** only discovers *how*: which screens, which controls, in which order.

So the capability's interface is never invented by the model; only its implementation is.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, model_validator
from ruamel.yaml import YAML

from cua.core.artifact import INPUT_REF, ObjectSchema, OutcomeDecl, RiskClass, validate_against
from cua.core.templating import TEMPLATE


class DiscoverySpec(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    capability_id: str = Field(pattern=r"^[a-z][a-z0-9_]*$")
    description: str = Field(min_length=10)
    goal: str = Field(min_length=10)
    tenant: str
    start_route: str = "/"
    inputs: ObjectSchema
    outputs: ObjectSchema
    example_inputs: dict[str, Any]
    outcomes: tuple[OutcomeDecl, ...] = Field(
        default=(),
        description="Business outcomes the person expects (not found, denied...). The model is "
        "told these codes so the same outcome gets the same name across runs.",
    )
    subject: str | None = Field(
        default=None, description="Input naming whose data this touches, e.g. inputs.member_id."
    )
    risk_ceiling: RiskClass = RiskClass.READ
    max_steps: int = Field(default=20, ge=1, le=60)
    max_minutes: float = Field(default=10.0, gt=0, le=60)
    hints: tuple[str, ...] = ()

    @model_validator(mode="after")
    def _consistent(self) -> DiscoverySpec:
        for ref in TEMPLATE.findall(self.goal):
            name = ref.removeprefix("inputs.")
            if not ref.startswith("inputs.") or name not in self.inputs.properties:
                raise ValueError(f"goal references unknown input {{{{{ref}}}}}")
        if self.subject is not None:
            m = INPUT_REF.match(self.subject)
            if not m or m.group(1) not in self.inputs.properties:
                raise ValueError(f"subject must be inputs.<declared input>, got {self.subject!r}")
        errors = validate_against(self.inputs, self.example_inputs)
        if errors:
            raise ValueError(f"example_inputs do not match the input schema: {errors}")
        return self


def load_spec(path: str | Path) -> DiscoverySpec:
    with open(path, encoding="utf-8") as fh:
        return DiscoverySpec.model_validate(YAML(typ="safe").load(fh))
