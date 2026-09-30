"""A tiny condition language for postconditions, waits, detectors and the success checkpoint.

Each condition is a single-key mapping, which keeps the YAML readable::

    {title_contains: "Member Detail"}
    {any_of: [{route: /member/:member_id}, {detector: member_not_found}]}
    {not: {element: {...target...}}}

Evaluation is a pure function of a :class:`PageState` snapshot. Only ``element`` needs the live
page; the surface supplies that through ``PageState.element_probe``.
"""

from __future__ import annotations

from collections.abc import Callable, Iterator, Mapping
from dataclasses import dataclass, field
from typing import Annotated, Any

from pydantic import BaseModel, ConfigDict, Field

from .targets import TargetSpec
from .text import contains, match_route, normalize


class _Cond(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, populate_by_name=True)


class TextVisible(_Cond):
    text_visible: str


class TitleContains(_Cond):
    title_contains: str


class Heading(_Cond):
    heading: str


class RouteIs(_Cond):
    route: str


class DialogTitle(_Cond):
    dialog_title: str


class DetectorFired(_Cond):
    detector: str


class OutputPresent(_Cond):
    output_present: str


class ElementPresent(_Cond):
    element: TargetSpec


class AllOf(_Cond):
    all_of: list[Condition] = Field(min_length=1)


class AnyOf(_Cond):
    any_of: list[Condition] = Field(min_length=1)


class Not(_Cond):
    not_: Condition = Field(alias="not")


Condition = Annotated[
    TextVisible
    | TitleContains
    | Heading
    | RouteIs
    | DialogTitle
    | DetectorFired
    | OutputPresent
    | ElementPresent
    | AllOf
    | AnyOf
    | Not,
    Field(union_mode="smart"),
]

for _model in (AllOf, AnyOf, Not):
    _model.model_rebuild()


class ConditionError(RuntimeError):
    """The condition cannot be evaluated against this state (for example no element probe)."""


@dataclass(frozen=True)
class PageState:
    """What the surface observed. Built by the surface adapter; consumed here without I/O."""

    route: str = "/"
    title: str = ""
    visible_text: str = ""
    headings: tuple[str, ...] = ()
    dialog_titles: tuple[str, ...] = ()
    outputs: Mapping[str, Any] = field(default_factory=dict)
    fired_detectors: frozenset[str] = frozenset()
    element_probe: Callable[[TargetSpec], bool] | None = None


def evaluate(cond: Condition, state: PageState) -> bool:
    match cond:
        case TextVisible(text_visible=t):
            return contains(state.visible_text, t)
        case TitleContains(title_contains=t):
            return contains(state.title, t)
        case Heading(heading=h):
            return any(normalize(x) == normalize(h) for x in state.headings)
        case RouteIs(route=pattern):
            return match_route(pattern, state.route) is not None
        case DialogTitle(dialog_title=t):
            return any(normalize(x) == normalize(t) for x in state.dialog_titles)
        case DetectorFired(detector=d):
            return d in state.fired_detectors
        case OutputPresent(output_present=name):
            return state.outputs.get(name) is not None
        case ElementPresent(element=target):
            if state.element_probe is None:
                raise ConditionError("an 'element' condition needs a live element probe")
            return state.element_probe(target)
        case AllOf(all_of=items):
            return all(evaluate(c, state) for c in items)
        case AnyOf(any_of=items):
            return any(evaluate(c, state) for c in items)
        case Not(not_=inner):
            return not evaluate(inner, state)
    raise TypeError(f"unknown condition {cond!r}")  # pragma: no cover


def walk(cond: Condition) -> Iterator[Condition]:
    """Every condition in the tree, depth first."""
    yield cond
    match cond:
        case AllOf(all_of=items) | AnyOf(any_of=items):
            for c in items:
                yield from walk(c)
        case Not(not_=inner):
            yield from walk(inner)
        case _:
            return


def referenced_detectors(cond: Condition) -> set[str]:
    return {c.detector for c in walk(cond) if isinstance(c, DetectorFired)}


def referenced_outputs(cond: Condition) -> set[str]:
    return {c.output_present for c in walk(cond) if isinstance(c, OutputPresent)}
