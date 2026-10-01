"""Run evidence: the only way anything about a run is written to disk.

Layout of one run::

    runs/<run_id>/
      events.jsonl          append-only, one redacted event per line
      <name>.json / .txt    redacted snapshots (observations, results, a11y dumps)
      screenshots/NN-<name>.png   sensitive regions blacked out before writing

Every write passes through the run's :class:`~cua.core.redact.Redactor`. There is no
"raw" write method on purpose.
"""

from __future__ import annotations

import json
import secrets
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Protocol

from cua.core.redact import Redactor
from cua.core.targets import TargetSpec


class _Screenshotter(Protocol):
    def screenshot(
        self,
        path: str,
        *,
        redact_values: tuple[str, ...] = (),
        redact_patterns: tuple[str, ...] = (),
        mask: tuple[TargetSpec, ...] = (),
    ) -> str: ...


def new_run_id(prefix: str = "run") -> str:
    stamp = datetime.now(UTC).strftime("%Y-%m-%dT%H-%M-%SZ")
    return f"{prefix}_{stamp}_{secrets.token_hex(2)}"


class RunEvidence:
    def __init__(self, root: str | Path, run_id: str, redactor: Redactor) -> None:
        self.run_id = run_id
        self.dir = Path(root) / run_id
        self.dir.mkdir(parents=True, exist_ok=True)
        self.redactor = redactor
        self._seq = 0
        self._shots = 0
        self._t0 = time.monotonic()

    @property
    def events_path(self) -> Path:
        return self.dir / "events.jsonl"

    def event(self, kind: str, **fields: Any) -> dict[str, Any]:
        self._seq += 1
        record = {
            "seq": self._seq,
            "t_ms": int((time.monotonic() - self._t0) * 1000),
            "ts": datetime.now(UTC).isoformat(timespec="milliseconds"),
            "kind": kind,
            **fields,
        }
        safe = self.redactor.redact(record)
        with self.events_path.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(safe, default=str, ensure_ascii=False) + "\n")
        return safe  # type: ignore[no-any-return]

    def write_json(self, name: str, obj: Any) -> Path:
        path = self.dir / f"{name}.json"
        safe = self.redactor.redact(json.loads(json.dumps(obj, default=str)))
        path.write_text(json.dumps(safe, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
        return path

    def write_text(self, name: str, text: str) -> Path:
        path = self.dir / f"{name}.txt"
        path.write_text(self.redactor.redact_text(text), encoding="utf-8")
        return path

    def screenshot(
        self, surface: _Screenshotter, name: str, *, mask: tuple[TargetSpec, ...] = ()
    ) -> Path:
        self._shots += 1
        path = self.dir / "screenshots" / f"{self._shots:02d}-{name}.png"
        surface.screenshot(
            str(path),
            redact_values=self.redactor.sensitive_strings(),
            redact_patterns=self.redactor.pattern_sources(),
            mask=mask,
        )
        self.event("screenshot", path=str(path.relative_to(self.dir)))
        return path
