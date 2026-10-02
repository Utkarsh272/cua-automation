"""Tenant overrides: one capability, small reviewed differences per tenant.

Overrides live *inside* the artifact (so they are reviewed, hashed and approved with it) and are
applied at replay time for the tenant being served. Two operations, both addressed by step or
detector id so they survive steps being reordered:

* ``set``: replace one value, e.g. ``steps.enter_member_id.target.strategies[0].text``
* ``insert_step``: add a step after a named one (tenant B's privacy checkbox)

The result is validated like any capability, so an override cannot produce an invalid one.
"""

from __future__ import annotations

import re
from typing import Any

from .artifact import ArtifactError, Capability, InsertStepOp, parse_capability

_TOKEN = re.compile(r"\.([A-Za-z0-9_]+)|\[(\d+)\]")


class OverrideError(ArtifactError):
    pass


def capability_dict(cap: Capability) -> dict[str, Any]:
    return cap.model_dump(mode="json", by_alias=True, exclude_none=True)


def _by_id(items: list[dict[str, Any]], ident: str, path: str) -> dict[str, Any]:
    for item in items:
        if item["id"] == ident:
            return item
    raise OverrideError(f"override path {path!r}: no {ident!r}")


def set_path(data: dict[str, Any], path: str, value: Any) -> None:
    """``steps.<id>.a.b[0].c`` or ``detectors.<id>.when``: the second segment is an id."""
    kind, rest = path.split(".", 1)
    m = re.match(r"[a-z][a-z0-9_]*", rest)
    if kind not in ("steps", "detectors") or m is None:
        raise OverrideError(f"override path {path!r} must start with steps.<id> or detectors.<id>")
    node: Any = _by_id(data.get(kind, []), m.group(0), path)
    tokens = [(k, int(i) if i else None) for k, i in _TOKEN.findall(rest[m.end() :])]
    if not tokens:
        raise OverrideError(f"override path {path!r} must point inside the {kind[:-1]}")
    try:
        for key, index in tokens[:-1]:
            node = node[key] if key else node[index]
        key, index = tokens[-1]
        if key:
            node[key] = value
        else:
            node[index] = value
    except (KeyError, IndexError, TypeError) as exc:
        raise OverrideError(f"override path {path!r} does not exist in the capability") from exc


def apply_ops(data: dict[str, Any], ops: list[dict[str, Any]]) -> dict[str, Any]:
    """Apply override operations (as plain dicts) to a capability dict, in order."""
    for op in ops:
        if op["op"] == "insert_step":
            ids = [s["id"] for s in data["steps"]]
            if op["after"] not in ids:
                raise OverrideError(f"insert_step: no step {op['after']!r}")
            data["steps"].insert(ids.index(op["after"]) + 1, op["step"])
        else:
            set_path(data, op["path"], op["value"])
    return data


def effective_capability(cap: Capability, tenant: str) -> tuple[Capability, int]:
    """The capability as it runs on ``tenant``, and how many overrides were applied."""
    ops = cap.overrides.get(tenant, ())
    if not ops:
        return cap, 0
    data = capability_dict(cap)
    plain = [
        {"op": "insert_step", "after": op.after, "step": capability_step(op)}
        if isinstance(op, InsertStepOp)
        else {"op": "set", "path": op.path, "value": op.value}
        for op in ops
    ]
    apply_ops(data, plain)
    data.pop("overrides", None)
    # content_hash stays: it identifies the reviewed file this view was derived from (and an
    # approved artifact must carry one). It is not re-verified against the derived steps.
    try:
        return parse_capability(data, verify_hash=False), len(ops)
    except ArtifactError as exc:
        raise OverrideError(f"overrides for {tenant} produce an invalid capability: {exc}") from exc


def capability_step(op: InsertStepOp) -> dict[str, Any]:
    return op.step.model_dump(mode="json", by_alias=True, exclude_none=True)
