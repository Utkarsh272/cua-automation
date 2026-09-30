"""Confirmation tokens for irreversible steps.

An irreversible step (opening an account, moving money) runs only with a token that is:

* **signed** (HMAC-SHA256 with a server secret), so it cannot be forged;
* **bound** to one run, one step and a hash of the exact inputs, so a token issued to open an
  account for member 10042 cannot be replayed for member 10077 or for a different amount;
* **short-lived** (default 5 minutes);
* **single-use**: a nonce is spent on first successful verification.

Without a valid token the policy guard escalates to a human instead of acting.
The in-memory nonce set is demo-grade; production would persist spent nonces.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import secrets
import threading
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Any


class TokenError(Exception):
    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason  # malformed | bad_signature | expired | wrong_binding | replayed


def inputs_hash(inputs: Mapping[str, Any]) -> str:
    canonical = json.dumps(inputs, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(canonical.encode()).hexdigest()


def _b64(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode()


def _unb64(text: str) -> bytes:
    return base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))


@dataclass(frozen=True)
class Confirmation:
    """What a caller presents for an irreversible step."""

    token: str
    run_id: str
    step_id: str
    inputs: Mapping[str, Any]


class ConfirmationTokens:
    def __init__(
        self,
        secret: bytes,
        *,
        ttl_s: int = 300,
        clock: Callable[[], float] = time.time,
    ) -> None:
        if len(secret) < 16:
            raise ValueError("confirmation secret must be at least 16 bytes")
        self._secret = secret
        self._ttl = ttl_s
        self._clock = clock
        self._spent: set[str] = set()
        self._lock = threading.Lock()

    def _sign(self, payload: bytes) -> str:
        return _b64(hmac.new(self._secret, payload, hashlib.sha256).digest())

    def issue(self, *, run_id: str, step_id: str, inputs: Mapping[str, Any], issued_to: str) -> str:
        payload = json.dumps(
            {
                "r": run_id,
                "s": step_id,
                "h": inputs_hash(inputs),
                "sub": issued_to,
                "exp": int(self._clock()) + self._ttl,
                "n": secrets.token_hex(8),
            },
            separators=(",", ":"),
        ).encode()
        return f"{_b64(payload)}.{self._sign(payload)}"

    def verify(self, c: Confirmation) -> str:
        """Verify and spend the token. Returns who it was issued to; raises TokenError."""
        try:
            body, sig = c.token.split(".")
            payload = _unb64(body)
            claims = json.loads(payload)
        except (ValueError, json.JSONDecodeError) as exc:
            raise TokenError("malformed") from exc
        if not hmac.compare_digest(sig, self._sign(payload)):
            raise TokenError("bad_signature")
        if claims["exp"] < self._clock():
            raise TokenError("expired")
        if (claims["r"], claims["s"], claims["h"]) != (c.run_id, c.step_id, inputs_hash(c.inputs)):
            raise TokenError("wrong_binding")
        with self._lock:
            if claims["n"] in self._spent:
                raise TokenError("replayed")
            self._spent.add(claims["n"])
        return str(claims["sub"])
