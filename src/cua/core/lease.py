"""The control lease: who may drive a live session right now.

A session has exactly one holder at a time, and every change of holder bumps an **epoch**.
Automation acts with a token ``(epoch)``; the surface refuses any action whose epoch is stale or
made while a person holds the lease. That single rule makes it impossible for automation and a
human to click at the same time, even if an automation thread wakes up late.

States::

    automation --request_human--> pending_human --claim--> human --begin_resume--> resuming
        ^                              |  ^                  |                        |
        |                              |  +----release-------+   (operator went away) |
        +-----------resumed (epoch+1)--|--------------------------------------------- +
                                       |          resume_failed --> pending_human
                              abort / timeout --> aborted

Pure and thread-safe: the operator console calls ``claim``/``abort`` from another thread.
"""

from __future__ import annotations

import threading
from dataclasses import dataclass
from typing import Literal

Holder = Literal["automation", "pending_human", "human", "resuming", "aborted"]


class LeaseError(RuntimeError):
    """An action or transition is not allowed in the current lease state."""


@dataclass(frozen=True)
class LeaseToken:
    epoch: int


class ControlLease:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._holder: Holder = "automation"
        self._epoch = 1
        self._operator: str | None = None
        self.history: list[tuple[int, Holder, str]] = [(1, "automation", "session opened")]

    # --- reading ----------------------------------------------------------------------------

    @property
    def holder(self) -> Holder:
        return self._holder

    @property
    def epoch(self) -> int:
        return self._epoch

    @property
    def operator(self) -> str | None:
        return self._operator

    def token(self) -> LeaseToken:
        with self._lock:
            if self._holder != "automation":
                raise LeaseError(f"automation does not hold the lease ({self._holder})")
            return LeaseToken(self._epoch)

    def check(self, token: LeaseToken) -> None:
        """Raise unless automation holds the lease *and* this token is from the current epoch."""
        with self._lock:
            if self._holder != "automation":
                raise LeaseError(f"automation may not act while the lease is {self._holder}")
            if token.epoch != self._epoch:
                raise LeaseError(f"stale lease token (epoch {token.epoch}, now {self._epoch})")

    # --- transitions ------------------------------------------------------------------------

    def _move(self, allowed: tuple[Holder, ...], to: Holder, why: str, bump: bool = False) -> None:
        if self._holder not in allowed:
            raise LeaseError(f"cannot go from {self._holder} to {to}")
        self._holder = to
        if bump:
            self._epoch += 1
        self.history.append((self._epoch, to, why))

    def request_human(self, reason: str) -> None:
        with self._lock:
            self._move(("automation",), "pending_human", reason, bump=True)

    def claim(self, operator: str) -> None:
        with self._lock:
            self._move(("pending_human",), "human", f"claimed by {operator}")
            self._operator = operator

    def release(self, why: str = "operator heartbeat lost") -> None:
        with self._lock:
            self._move(("human",), "pending_human", why)
            self._operator = None

    def begin_resume(self) -> None:
        with self._lock:
            self._move(("human",), "resuming", "operator handed back")

    def resume_failed(self, reason: str) -> None:
        with self._lock:
            self._move(("resuming",), "pending_human", f"resume check failed: {reason}")
            self._operator = None

    def resumed(self) -> LeaseToken:
        with self._lock:
            self._move(("resuming",), "automation", "precondition verified", bump=True)
            self._operator = None
            return LeaseToken(self._epoch)

    def abort(self, why: str) -> None:
        with self._lock:
            self._move(("pending_human", "human", "resuming"), "aborted", why)
