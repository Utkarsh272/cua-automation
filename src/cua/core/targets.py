"""How a step names the element it acts on.

A target is a *ranked list of locator strategies* plus a fingerprint:

* Strategies are tried in order at replay. The first one that matches **exactly one** visible
  element wins. Zero or several matches means "try the next one", never "pick the closest".
* The fingerprint records what the element looked like when it was recorded. It is used to
  detect drift and to help repair a locator, never to decide what to click.

Strategies are ordered from most to least robust. Coordinates, when present, must be last:
they are the fallback for pixel-only surfaces (Citrix, some desktop apps).
"""

from __future__ import annotations

from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class FrameRef(_Strict):
    """One hop in a frame path, outermost first. Match by frame name or by URL fragment."""

    name: str | None = None
    url_contains: str | None = None

    @model_validator(mode="after")
    def _one_key(self) -> FrameRef:
        if (self.name is None) == (self.url_contains is None):
            raise ValueError("a frame reference needs exactly one of 'name' or 'url_contains'")
        return self


class RoleStrategy(_Strict):
    """Accessibility role and accessible name, as a person would describe the control.

    ``near_text`` narrows a non-unique match to the one sharing the closest container with
    that text (for example the "Search" button of the "Find Member" form, not quick-find).
    """

    by: Literal["role"] = "role"
    role: str
    name: str
    exact: bool = True
    near_text: str | None = None


ControlKind = Literal["textbox", "checkbox", "combobox", "button", "value_cell", "any"]


class LabelStrategy(_Strict):
    """A control identified by visible label text.

    Covers ``<label for>``, ``aria-labelledby`` and the legacy pattern where the label sits in
    the table cell to the left of the control. ``value_cell`` means the read-only value next to
    the label (``Name: | Maria Delgado``).
    """

    by: Literal["label"] = "label"
    text: str
    control: ControlKind = "any"


class TableCellStrategy(_Strict):
    """A cell addressed the way a person reads a table: by row label and column header text."""

    by: Literal["table_cell"] = "table_cell"
    table_near: str
    row_match: str
    column: str
    row_match_mode: Literal["exact", "contains"] = "exact"


class AttributeStrategy(_Strict):
    """A stable attribute, such as ``name``. Generated ids must never be used here."""

    by: Literal["attribute"] = "attribute"
    attr: str
    value: str
    tag: str | None = None


class TextStrategy(_Strict):
    by: Literal["text"] = "text"
    text: str
    exact: bool = True


class CssStrategy(_Strict):
    by: Literal["css"] = "css"
    selector: str


class CoordinatesStrategy(_Strict):
    """Pixel position. Last resort, only for surfaces with no usable UI tree."""

    by: Literal["coordinates"] = "coordinates"
    x: int
    y: int
    relative_to: Literal["frame", "viewport"] = "frame"


Strategy = Annotated[
    RoleStrategy
    | LabelStrategy
    | TableCellStrategy
    | AttributeStrategy
    | TextStrategy
    | CssStrategy
    | CoordinatesStrategy,
    Field(discriminator="by"),
]

# Lower is more robust. Used for drift reporting ("the primary strategy stopped winning").
ROBUSTNESS: dict[str, int] = {
    "role": 0,
    "label": 1,
    "table_cell": 2,
    "attribute": 3,
    "text": 4,
    "css": 5,
    "coordinates": 6,
}


class Fingerprint(_Strict):
    """What the element looked like at record time. For drift detection only."""

    tag: str | None = None
    role: str | None = None
    name: str | None = None
    near_text: str | None = None
    row_context: tuple[str, ...] = ()
    attributes: dict[str, str] = Field(default_factory=dict)
    bbox: tuple[int, int, int, int] | None = None


class TargetSpec(_Strict):
    description: str = Field(min_length=3, description="What a reviewer should understand this is.")
    frame: tuple[FrameRef, ...] = ()
    strategies: tuple[Strategy, ...] = Field(min_length=1)
    fingerprint: Fingerprint | None = None

    @model_validator(mode="after")
    def _coordinates_last(self) -> TargetSpec:
        kinds = [s.by for s in self.strategies]
        if "coordinates" in kinds[:-1]:
            raise ValueError("coordinates can only be the last strategy")
        return self
