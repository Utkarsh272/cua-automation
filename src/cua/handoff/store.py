"""Intervention requests: what the operator console lists and acts on.

One JSON file per request under ``interventions/`` (so they survive a crash and are easy to
inspect), with an in-memory copy guarded by a lock: the console thread writes decisions, the
engine thread reads them.

Status: ``open`` -> ``claimed`` -> ``resolved`` | ``aborted``; ``open`` -> ``timed_out``.
A claimed request whose operator disappears, or whose hand-back fails verification, goes back
to ``open`` with a note.
"""

from __future__ import annotations

import json
import secrets
import threading
import time
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal

Action = Literal["approve", "step_done", "resume", "abort"]
Kind = Literal["confirmation", "stuck", "dialog", "policy"]

ALLOWED: dict[str, tuple[Action, ...]] = {
    # approve = let automation perform this one step; step_done = "I did it by hand".
    "confirmation": ("approve", "step_done", "abort"),
    "policy": ("step_done", "abort"),
    "dialog": ("resume", "abort"),
    "stuck": ("resume", "abort"),
}


class InterventionError(Exception):
    pass


@dataclass
class Intervention:
    id: str
    status: str
    kind: str
    created_at: str
    run_id: str
    mode: str  # replay | discovery
    capability: str
    tenant: str
    step: str | None
    step_description: str
    reason: str
    allowed: tuple[str, ...]
    expects_on_return: str
    screenshot: str | None = None
    recent_events: list[str] = field(default_factory=list)
    claimed_by: str | None = None
    decision: dict[str, Any] | None = None
    human_actions: list[dict[str, Any]] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)
    last_heartbeat: float = 0.0
    created_monotonic: float = 0.0


def _now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


class InterventionStore:
    def __init__(self, root: str | Path) -> None:
        self.root = Path(root)
        self._lock = threading.Lock()
        self._items: dict[str, Intervention] = {}

    # --- persistence ------------------------------------------------------------------------

    def _save(self, item: Intervention) -> None:
        self.root.mkdir(parents=True, exist_ok=True)
        data = asdict(item)
        data.pop("created_monotonic")
        data.pop("last_heartbeat")
        (self.root / f"{item.id}.json").write_text(json.dumps(data, indent=2), encoding="utf-8")

    # --- engine side ------------------------------------------------------------------------

    def create(
        self,
        *,
        kind: Kind,
        run_id: str,
        mode: str,
        capability: str,
        tenant: str,
        step: str | None,
        step_description: str,
        reason: str,
        expects_on_return: str,
        screenshot: str | None = None,
        recent_events: list[str] | None = None,
        allowed: tuple[str, ...] | None = None,
    ) -> Intervention:
        item = Intervention(
            id=f"int_{datetime.now(UTC).strftime('%Y%m%dT%H%M%S')}_{secrets.token_hex(3)}",
            status="open",
            kind=kind,
            created_at=_now(),
            run_id=run_id,
            mode=mode,
            capability=capability,
            tenant=tenant,
            step=step,
            step_description=step_description,
            reason=reason,
            allowed=allowed or ALLOWED[kind],
            expects_on_return=expects_on_return,
            screenshot=screenshot,
            recent_events=recent_events or [],
            created_monotonic=time.monotonic(),
        )
        with self._lock:
            self._items[item.id] = item
            self._save(item)
        return item

    def get(self, iid: str) -> Intervention:
        with self._lock:
            if iid not in self._items:
                raise InterventionError(f"unknown intervention {iid}")
            return self._items[iid]

    def list(self) -> list[Intervention]:
        with self._lock:
            return sorted(self._items.values(), key=lambda i: i.created_at, reverse=True)

    def add_human_action(self, iid: str, action: dict[str, Any]) -> None:
        with self._lock:
            item = self._items[iid]
            item.human_actions.append({"at": _now(), **action})
            self._save(item)

    def reopen(self, iid: str, note: str) -> None:
        with self._lock:
            item = self._items[iid]
            item.status = "open"
            item.claimed_by = None
            item.decision = None
            item.notes.append(f"{_now()} {note}")
            item.created_monotonic = time.monotonic()
            self._save(item)

    def close(self, iid: str, status: str, note: str = "") -> None:
        with self._lock:
            item = self._items[iid]
            item.status = status
            if note:
                item.notes.append(f"{_now()} {note}")
            self._save(item)

    # --- operator side ----------------------------------------------------------------------

    def claim(self, iid: str, operator: str) -> Intervention:
        with self._lock:
            item = self._items.get(iid)
            if item is None:
                raise InterventionError(f"unknown intervention {iid}")
            if item.status != "open":
                raise InterventionError(f"{iid} is {item.status}, not open")
            item.status = "claimed"
            item.claimed_by = operator
            item.last_heartbeat = time.monotonic()
            item.notes.append(f"{_now()} claimed by {operator}")
            self._save(item)
            return item

    def heartbeat(self, iid: str, operator: str) -> None:
        with self._lock:
            item = self._items.get(iid)
            if item is not None and item.status == "claimed" and item.claimed_by == operator:
                item.last_heartbeat = time.monotonic()

    def decide(self, iid: str, operator: str, action: str, note: str = "") -> Intervention:
        with self._lock:
            item = self._items.get(iid)
            if item is None:
                raise InterventionError(f"unknown intervention {iid}")
            if item.status != "claimed" or item.claimed_by != operator:
                raise InterventionError(f"{iid} is not claimed by {operator}")
            if action not in item.allowed:
                raise InterventionError(f"{action!r} is not allowed here; choose {item.allowed}")
            item.decision = {"action": action, "by": operator, "at": _now(), "note": note}
            item.notes.append(f"{_now()} {operator} chose {action}")
            self._save(item)
            return item
