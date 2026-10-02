"""Filesystem capability registry: ``capabilities/<id>/<version>.yaml``.

Rules, enforced here rather than by convention:

* **Drafts are editable, approved versions are immutable.** Saving over an approved or
  deprecated version is refused; a change means a new version.
* **Approval is a person's act, recorded in the file.** ``approve`` sets ``status``,
  ``approved_by``, ``approved_at`` and the behavior ``content_hash``, editing the YAML in place so
  reviewer comments survive. From then on any behavioral edit breaks the hash and the file
  refuses to load.
* **Four eyes by default.** Whoever (or whatever) recorded a draft cannot approve it, unless
  explicitly allowed (a single-developer demo).
* **Resolution by constraint.** ``resolve("login", "^1")`` returns the highest approved version
  matching the constraint. Drafts only when asked (attended runs).

Files are reviewed like code, so git is their history. The interface is small enough to move
to a database without changing callers.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path

from ruamel.yaml import YAML

from cua.core.artifact import ArtifactError, Capability, compute_hash, load_capability
from cua.core.semver import parse_version, satisfies


class RegistryError(Exception):
    pass


def _rt_yaml() -> YAML:
    y = YAML()  # round-trip: keeps comments, order and quoting
    y.preserve_quotes = True
    y.width = 110
    y.indent(mapping=2, sequence=2, offset=0)
    return y


class Registry:
    def __init__(self, root: str | Path) -> None:
        self.root = Path(root)

    def path(self, cap_id: str, version: str) -> Path:
        return self.root / cap_id / f"{version}.yaml"

    def ids(self) -> list[str]:
        return sorted(
            p.name for p in self.root.iterdir() if p.is_dir() and not p.name.startswith("_")
        )

    def versions(self, cap_id: str) -> list[str]:
        folder = self.root / cap_id
        if not folder.exists():
            return []
        return sorted((p.stem for p in folder.glob("*.yaml")), key=parse_version)

    def load(self, cap_id: str, version: str) -> Capability:
        path = self.path(cap_id, version)
        if not path.exists():
            raise RegistryError(f"{cap_id}@{version} not found")
        cap = load_capability(path)
        if (cap.id, cap.version) != (cap_id, version):
            raise RegistryError(f"{path} declares {cap.ref}; file name and content disagree")
        return cap

    def resolve(
        self, cap_id: str, constraint: str = "*", *, allow_draft: bool = False
    ) -> Capability:
        candidates = []
        for v in reversed(self.versions(cap_id)):
            if not satisfies(v, constraint):
                continue
            cap = self.load(cap_id, v)
            exact_pin = constraint.strip() == v
            if cap.status == "approved" or (cap.status == "draft" and allow_draft):
                return cap
            if cap.status == "deprecated" and exact_pin:
                return cap
            candidates.append(f"{v} ({cap.status})")
        seen = f"; found {', '.join(candidates)}" if candidates else ""
        raise RegistryError(f"no runnable version of {cap_id} matches {constraint!r}{seen}")

    def save_draft(self, cap: Capability, text: str, *, replace: bool = False) -> Path:
        if cap.status != "draft":
            raise RegistryError("only drafts are saved; approval happens through approve()")
        path = self.path(cap.id, cap.version)
        if path.exists():
            try:
                existing = load_capability(path, verify_hash=False)
            except ArtifactError:
                existing = None
            if existing is not None and existing.status != "draft":
                raise RegistryError(
                    f"{cap.ref} is {existing.status} and immutable; compile a new version instead"
                )
            if not replace:
                raise RegistryError(f"{path} exists; pass replace=True to overwrite the draft")
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
        return path

    def approve(
        self,
        cap_id: str,
        version: str,
        *,
        approver: str,
        allow_self_approval: bool = False,
        now: datetime | None = None,
    ) -> Capability:
        cap = self.load(cap_id, version)
        if cap.status != "draft":
            raise RegistryError(f"{cap.ref} is already {cap.status}")
        if not approver.strip():
            raise RegistryError("approver is required")
        if cap.recorded_by == approver and not allow_self_approval:
            raise RegistryError(
                f"{approver} recorded {cap.ref} and cannot also approve it (four-eyes rule)"
            )
        return self._set_status(
            cap_id,
            version,
            status="approved",
            extra={
                "approved_by": approver,
                "approved_at": (now or datetime.now(UTC)).isoformat(timespec="seconds"),
            },
        )

    def deprecate(self, cap_id: str, version: str) -> Capability:
        cap = self.load(cap_id, version)
        if cap.status != "approved":
            raise RegistryError(
                f"only approved versions can be deprecated ({cap.ref} is {cap.status})"
            )
        return self._set_status(cap_id, version, status="deprecated", extra={})

    def _set_status(
        self, cap_id: str, version: str, *, status: str, extra: dict[str, str]
    ) -> Capability:
        path = self.path(cap_id, version)
        y = _rt_yaml()
        doc = y.load(path.read_text(encoding="utf-8"))
        current = Capability.model_validate(json.loads(json.dumps(doc, default=str)))
        digest = compute_hash(current)  # behavior only: unaffected by the fields set below

        def put(key: str, value: str, after: str) -> None:
            if key in doc:
                doc[key] = value
            else:
                doc.insert(list(doc.keys()).index(after) + 1, key, value)

        put("content_hash", digest, "version")
        put("status", status, "content_hash")
        anchor = "recorded_by" if "recorded_by" in doc else "status"
        for key, value in extra.items():
            put(key, value, anchor)
            anchor = key
        with path.open("w", encoding="utf-8") as fh:
            y.dump(doc, fh)
        cap = self.load(cap_id, version)  # proves the result parses and the hash verifies
        if cap.content_hash != digest:  # pragma: no cover - defensive
            raise RegistryError("approval changed behavior; refusing")
        return cap
