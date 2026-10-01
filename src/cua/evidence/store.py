"""Run evidence: the only way anything about a run is written to disk.

Layout of one run::

    <root>/<run_id>/
      events.jsonl          append-only, one redacted event per line
      <name>.json / .txt    redacted snapshots (trace, results, prompts)
      screenshots/NN-<name>.png   sensitive regions blacked out

Every write passes through the run's :class:`~cua.core.redact.Redactor`; there is no raw write.

**Late-known values.** In discovery, output values (a member's name, a balance) only become known
when the agent extracts them, but earlier turns may already have shown them. So
:meth:`finalize` re-redacts every text file with the final redactor, and screenshots captured
with :meth:`capture` are held in memory and masked again at finalize time using the element
regions recorded at capture, before they are written.
"""

from __future__ import annotations

import io
import json
import secrets
import time
from dataclasses import dataclass
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


class _PngSource(Protocol):
    def screenshot_png(
        self,
        *,
        redact_values: tuple[str, ...] = (),
        redact_patterns: tuple[str, ...] = (),
        mask: tuple[TargetSpec, ...] = (),
    ) -> bytes: ...


Region = tuple[tuple[int, int, int, int], str]
"""(x, y, width, height) and the text shown there."""


@dataclass
class _PendingShot:
    path: Path
    png: bytes
    regions: list[Region]


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
        self._pending: list[_PendingShot] = []
        self._t0 = time.monotonic()

    @property
    def events_path(self) -> Path:
        return self.dir / "events.jsonl"

    def _path(self, name: str, suffix: str) -> Path:
        path = self.dir / f"{name}{suffix}"
        path.parent.mkdir(parents=True, exist_ok=True)
        return path

    def event(self, kind: str, **fields: Any) -> dict[str, Any]:
        self._seq += 1
        record = {
            "seq": self._seq,
            "t_ms": int((time.monotonic() - self._t0) * 1000),
            "ts": datetime.now(UTC).isoformat(timespec="milliseconds"),
            "kind": kind,
            **fields,
        }
        safe = self.redactor.redact(json.loads(json.dumps(record, default=str)))
        with self.events_path.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(safe, ensure_ascii=False) + "\n")
        return safe  # type: ignore[no-any-return]

    def write_json(self, name: str, obj: Any) -> Path:
        path = self._path(name, ".json")
        safe = self.redactor.redact(json.loads(json.dumps(obj, default=str)))
        path.write_text(json.dumps(safe, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
        return path

    def write_text(self, name: str, text: str) -> Path:
        path = self._path(name, ".txt")
        path.write_text(self.redactor.redact_text(text), encoding="utf-8")
        return path

    def _next_shot(self, name: str) -> Path:
        self._shots += 1
        return self._path(f"screenshots/{self._shots:02d}-{name}", ".png")

    def screenshot(
        self, surface: _Screenshotter, name: str, *, mask: tuple[TargetSpec, ...] = ()
    ) -> Path:
        """Mask with what is known now and write immediately."""
        path = self._next_shot(name)
        surface.screenshot(
            str(path),
            redact_values=self.redactor.sensitive_strings(),
            redact_patterns=self.redactor.pattern_sources(),
            mask=mask,
        )
        self.event("screenshot", path=str(path.relative_to(self.dir)))
        return path

    def capture(self, surface: _PngSource, name: str, regions: list[Region]) -> Path:
        """Mask with what is known now; hold in memory until :meth:`finalize`."""
        path = self._next_shot(name)
        png = surface.screenshot_png(
            redact_values=self.redactor.sensitive_strings(),
            redact_patterns=self.redactor.pattern_sources(),
        )
        self._pending.append(_PendingShot(path, png, regions))
        self.event("screenshot", path=str(path.relative_to(self.dir)))
        return path

    def finalize(self) -> None:
        """Apply the final redactor to everything written or held during the run."""
        from PIL import Image

        from cua.surface.web import black_out

        for shot in self._pending:
            img = Image.open(io.BytesIO(shot.png)).convert("RGB")
            boxes = [box for box, text in shot.regions if self.redactor.redact_text(text) != text]
            black_out(img, boxes)
            buf = io.BytesIO()
            img.save(buf, format="PNG")
            shot.path.write_bytes(buf.getvalue())
        self._pending.clear()
        for path in self.dir.rglob("*"):
            if not path.is_file():
                continue
            text = (
                path.read_text(encoding="utf-8")
                if path.suffix in (".txt", ".json", ".jsonl")
                else None
            )
            if text is None:
                continue
            if path.suffix == ".txt":
                redacted = self.redactor.redact_text(text)
            elif path.suffix == ".json":
                obj = self.redactor.redact(json.loads(text))
                redacted = json.dumps(obj, indent=2, ensure_ascii=False) + "\n"
            else:  # .jsonl: structure-aware per line, so numbers never become bare text
                lines = [
                    json.dumps(self.redactor.redact(json.loads(line)), ensure_ascii=False)
                    for line in text.splitlines()
                    if line.strip()
                ]
                redacted = "\n".join(lines) + "\n"
            if redacted != text:
                path.write_text(redacted, encoding="utf-8")
