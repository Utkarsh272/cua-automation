"""The surface contract: the seam between *what to do* and *how a UI is driven*.

Above this line (discovery, compiler, replay, policy) code knows about targets, steps and
conditions. Below it, an adapter knows about DOM, frames and pixels. A desktop adapter
(Windows UI Automation, macOS AX) implements the same protocol; flows do not change.

Key rules every adapter must follow:

* **Uniqueness.** ``resolve`` tries strategies in order and a strategy wins only if it matches
  exactly one visible element. Zero or several matches means "try the next strategy".
  Adapters never pick "the closest" element.
* **No held handles across steps.** Every action re-resolves its target.
* **Observations are snapshots.** Refs (``e12``) are valid only until the next ``observe``.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Literal, Protocol

from cua.core.conditions import PageState
from cua.core.results import FailureCode
from cua.core.targets import FrameRef, Strategy, TargetSpec


@dataclass(frozen=True)
class StrategyAttempt:
    by: str
    matches: int
    note: str = ""


@dataclass
class Resolution:
    """Which strategy won and what it found. ``handle`` is adapter-specific and short-lived."""

    target: TargetSpec
    strategy_index: int
    handle: Any
    attempts: tuple[StrategyAttempt, ...]

    @property
    def strategy(self) -> Strategy:
        return self.target.strategies[self.strategy_index]

    @property
    def degraded(self) -> bool:
        """The primary strategy did not win. Early drift signal."""
        return self.strategy_index > 0


class ResolutionError(Exception):
    """No strategy matched exactly one element."""

    def __init__(self, target: TargetSpec, attempts: tuple[StrategyAttempt, ...]) -> None:
        self.target = target
        self.attempts = attempts
        ambiguous = any(a.matches > 1 for a in attempts)
        self.code = FailureCode.TARGET_AMBIGUOUS if ambiguous else FailureCode.TARGET_NOT_FOUND
        detail = ", ".join(f"{a.by}={a.matches}{' ' + a.note if a.note else ''}" for a in attempts)
        super().__init__(f"{self.code.value}: {target.description} ({detail})")


@dataclass(frozen=True)
class DialogInfo:
    kind: Literal["modal", "native"]
    title: str
    text: str
    buttons: tuple[str, ...] = ()


@dataclass(frozen=True)
class ObservedElement:
    """One thing the discovery agent can act on or read, addressed by ``ref``."""

    ref: str
    role: str  # textbox | checkbox | combobox | button | link | cell
    name: str  # approximate accessible name
    label: str  # visible label (label/for, aria, or the neighbouring table cell)
    tag: str
    frame: tuple[FrameRef, ...]
    value: str = ""
    options: tuple[str, ...] = ()
    row: tuple[str, ...] = ()
    column: str = ""
    context: str = ""  # title of the form/table the control sits in, e.g. "Find Member"
    dialog: str = ""  # title of the dialog/overlay the control sits in, if any
    disabled: bool = False
    sensitive: bool = False  # e.g. password fields; value never shown
    bbox: tuple[int, int, int, int] | None = None


@dataclass
class Observation:
    url: str
    route: str
    title: str
    frames: tuple[str, ...]
    elements: tuple[ObservedElement, ...]
    text: str
    dialogs: tuple[DialogInfo, ...] = ()
    blocked_navigations: tuple[str, ...] = ()
    notes: list[str] = field(default_factory=list)

    def element(self, ref: str) -> ObservedElement:
        for e in self.elements:
            if e.ref == ref:
                return e
        raise KeyError(ref)

    def render(self, max_text: int = 3000) -> str:
        """Compact text form for an LLM prompt. Values of sensitive fields are never included."""
        lines = [f"PAGE route={self.route} title={self.title!r}"]
        for d in self.dialogs:
            buttons = ", ".join(d.buttons)
            lines.append(f"DIALOG ({d.kind}) {d.title!r}: {d.text[:200]!r} buttons=[{buttons}]")
        if self.blocked_navigations:
            lines.append(f"BLOCKED NAVIGATIONS: {', '.join(self.blocked_navigations)}")
        lines.append("ELEMENTS:")
        for e in self.elements:
            parts = [f"  {e.ref} {e.role}"]
            if e.label:
                parts.append(f"label={e.label!r}")
            if e.name and e.name != e.label:
                parts.append(f"name={e.name!r}")
            if e.role == "cell":
                if e.column:
                    parts.append(f"column={e.column!r}")
                if e.row:
                    parts.append(f"row={e.row[0]!r}")
                parts.append(f"text={e.value!r}")
            elif e.value and not e.sensitive:
                parts.append(f"value={e.value!r}")
            if e.options:
                parts.append(f"options={list(e.options)}")
            if e.context:
                parts.append(f"in={e.context!r}")
            if e.dialog:
                parts.append(f"dialog={e.dialog!r}")
            if e.disabled:
                parts.append("disabled")
            lines.append(" ".join(parts))
        text = self.text if len(self.text) <= max_text else self.text[:max_text] + " ..."
        lines.append("TEXT:")
        lines.append(text)
        return "\n".join(lines)


ActionName = Literal["click", "fill", "select", "check", "press"]


class Surface(Protocol):
    """What discovery and replay need from any UI, web or desktop."""

    def navigate(self, route: str, frame: tuple[FrameRef, ...] = ()) -> None: ...

    def resolve(self, target: TargetSpec) -> Resolution: ...

    def count(self, target: TargetSpec) -> int:
        """Matches of the best strategy that matches anything (for ``element`` conditions)."""
        ...

    def act(self, res: Resolution, action: ActionName, value: str | None = None) -> None: ...

    def read(self, res: Resolution) -> str: ...

    def settle(self, timeout_ms: int = 5000) -> None: ...

    def control_name(self, res: Resolution) -> str: ...

    def alerts(self) -> list[str]: ...

    def pause(self, ms: int) -> None:
        """Let the UI make progress without busy-waiting (used by condition polling)."""
        ...

    def page_state(
        self, outputs: dict[str, Any] | None = None, fired: frozenset[str] = frozenset()
    ) -> PageState: ...

    def observe(self) -> Observation: ...

    def screenshot(
        self,
        path: str,
        *,
        redact_values: tuple[str, ...] = (),
        redact_patterns: tuple[str, ...] = (),
        mask: tuple[TargetSpec, ...] = (),
    ) -> str: ...
