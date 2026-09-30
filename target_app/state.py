"""In-memory state for one CU Core instance: sessions, members, login lockout."""

from __future__ import annotations

import secrets
import threading
import time
from dataclasses import dataclass, field
from decimal import Decimal

from .data import Account, Member, seed_members
from .faults import FaultBoard

SERVICE_ACCOUNT = "svc_automation"
SERVICE_PASSWORD = "Tr0ub4dor&3-synthetic"  # synthetic; mirrors .env.example
LOCKOUT_THRESHOLD = 3
IDLE_TIMEOUT_S = 20 * 60


@dataclass
class PendingAccount:
    kind: str
    nickname: str
    deposit: Decimal
    funding_account: str


@dataclass
class Session:
    sid: str
    user: str
    created_at: float
    last_seen: float
    notice_acked: bool = False
    surprise_acked: bool = False
    pending: dict[str, PendingAccount] = field(default_factory=dict)  # member_id -> pending


class AppState:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self.members: dict[str, Member] = seed_members()
        self.sessions: dict[str, Session] = {}
        self.failed_logins: dict[str, int] = {}
        self.faults = FaultBoard()
        self._next_suffix: dict[str, int] = {}

    # --- auth -------------------------------------------------------------------------------

    def login(self, user: str, password: str) -> tuple[Session | None, str | None]:
        """Returns (session, None) on success or (None, error message)."""
        with self._lock:
            if self.failed_logins.get(user, 0) >= LOCKOUT_THRESHOLD:
                return None, "User ID is locked. Contact your system administrator."
            if user != SERVICE_ACCOUNT or password != SERVICE_PASSWORD:
                self.failed_logins[user] = self.failed_logins.get(user, 0) + 1
                if self.failed_logins[user] >= LOCKOUT_THRESHOLD:
                    return None, "User ID is locked. Contact your system administrator."
                return None, "Invalid user ID or password."
            self.failed_logins[user] = 0
            now = time.time()
            sess = Session(secrets.token_urlsafe(24), user, now, now)
            self.sessions[sess.sid] = sess
            return sess, None

    def get_session(self, sid: str | None) -> Session | None:
        if not sid:
            return None
        with self._lock:
            sess = self.sessions.get(sid)
            if sess is None:
                return None
            now = time.time()
            if now - sess.last_seen > IDLE_TIMEOUT_S:
                del self.sessions[sid]
                return None
            sess.last_seen = now
            return sess

    def drop_session(self, sid: str | None) -> None:
        if sid:
            with self._lock:
                self.sessions.pop(sid, None)

    # --- accounts ---------------------------------------------------------------------------

    def open_account(self, member_id: str, pending: PendingAccount) -> Account:
        with self._lock:
            member = self.members[member_id]
            funding = next(a for a in member.accounts if a.number == pending.funding_account)
            funding.balance -= pending.deposit
            base = member.accounts[0].number.split("-")
            suffix = self._next_suffix.get(member_id, len(member.accounts)) + 1
            self._next_suffix[member_id] = suffix
            acct = Account(
                number=f"{base[0]}-{suffix:03d}-{base[2]}",
                kind=pending.kind,
                balance=pending.deposit,
                nickname=pending.nickname,
            )
            member.accounts.append(acct)
            return acct

    def reset(self) -> None:
        with self._lock:
            self.members = seed_members()
            self.sessions.clear()
            self.failed_logins.clear()
            self._next_suffix.clear()
        self.faults.clear()
