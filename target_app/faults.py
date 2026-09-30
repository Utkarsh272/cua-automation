"""Fault injection.

Faults are how replay classification is demonstrated reproducibly. There are three sources,
merged per request:

1. ``X-Fault`` request header, e.g. ``X-Fault: notice, slow=4000``.
2. ``cu_fault`` cookie with the same syntax (convenient from a browser session).
3. Server-side *armed* faults (``POST /__test/arm``) that fire a limited number of times on
   requests whose path starts with a prefix. These model faults that happen "at step N",
   such as a session expiring in the middle of a flow.

Natural faults need no switch: member 99999 does not exist, 10666 is restricted, an
initial deposit above the limit fails validation.
"""

from __future__ import annotations

import threading
from dataclasses import dataclass

KNOWN_FAULTS = {
    "notice": "Show a 'System Notice' interstitial on search results and member pages.",
    "surprise_dialog": "Show an unrecognized 'Security Update Required' modal.",
    "slow": "Delay responses by N milliseconds (slow=4000).",
    "session_expired": "Invalidate the session; the request is redirected to login.",
    "error500": "Return the runtime error page (optional value = path prefix).",
    "maintenance": "Return the scheduled-maintenance page (HTTP 503).",
    "commit_timeout": "On Confirm, commit the account and then stall N ms (default 30000).",
}


def parse_fault_spec(spec: str | None) -> dict[str, str | None]:
    """Parse ``"notice, slow=4000"`` into ``{"notice": None, "slow": "4000"}``.

    Unknown names are ignored so a typo cannot crash the target app.
    """
    out: dict[str, str | None] = {}
    if not spec:
        return out
    for raw in spec.split(","):
        part = raw.strip()
        if not part:
            continue
        name, _, value = part.partition("=")
        name = name.strip()
        if name in KNOWN_FAULTS:
            out[name] = value.strip() or None
    return out


@dataclass
class ArmedFault:
    name: str
    value: str | None
    path_prefix: str
    remaining: int


class FaultBoard:
    """Server-side faults that fire a bounded number of times."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._armed: list[ArmedFault] = []

    def arm(self, name: str, value: str | None, path_prefix: str, times: int) -> None:
        if name not in KNOWN_FAULTS:
            raise ValueError(f"unknown fault {name!r}")
        with self._lock:
            self._armed.append(ArmedFault(name, value, path_prefix, max(1, times)))

    def take(self, path: str) -> dict[str, str | None]:
        """Consume and return every armed fault that matches this path."""
        fired: dict[str, str | None] = {}
        with self._lock:
            for fault in self._armed:
                if fault.remaining > 0 and path.startswith(fault.path_prefix):
                    fault.remaining -= 1
                    fired[fault.name] = fault.value
            self._armed = [f for f in self._armed if f.remaining > 0]
        return fired

    def clear(self) -> None:
        with self._lock:
            self._armed.clear()

    def snapshot(self) -> list[dict[str, object]]:
        with self._lock:
            return [f.__dict__.copy() for f in self._armed]
