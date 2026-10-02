"""Readable YAML for capability files: block style for structure, flow style for small leaves.

A reviewer should be able to read a step in a few lines::

    target:
      strategies:
      - {by: label, text: Member ID, control: textbox}
"""

from __future__ import annotations

import io
from typing import Any

from ruamel.yaml import YAML
from ruamel.yaml.comments import CommentedMap, CommentedSeq

_SCALARS = (str, int, float, bool, type(None))


MAX_FLOW_CHARS = 88


def _leafy(value: Any) -> bool:
    """Small all-scalar maps/lists render inline, but only if the line stays short."""
    if isinstance(value, dict):
        ok = 0 < len(value) <= 6 and all(isinstance(v, _SCALARS) for v in value.values())
        size = sum(len(str(k)) + len(str(v)) + 4 for k, v in value.items())
    elif isinstance(value, list):
        ok = 0 < len(value) <= 8 and all(isinstance(v, _SCALARS) for v in value)
        size = sum(len(str(v)) + 2 for v in value)
    else:
        return False
    return ok and size <= MAX_FLOW_CHARS


def to_commented(value: Any) -> Any:
    if isinstance(value, dict):
        m = CommentedMap()
        for k, v in value.items():
            m[k] = to_commented(v)
        if _leafy(value):
            m.fa.set_flow_style()
        return m
    if isinstance(value, list | tuple):
        seq = CommentedSeq(to_commented(v) for v in value)
        if _leafy(list(value)):
            seq.fa.set_flow_style()
        return seq
    return value


def dump(
    data: dict[str, Any], header: list[str] | None = None, gaps_before: tuple[str, ...] = ()
) -> str:
    doc = to_commented(data)
    for key in gaps_before:
        if key in doc:
            doc.yaml_set_comment_before_after_key(key, before="\n")
    y = YAML()
    y.width = 110
    y.indent(mapping=2, sequence=2, offset=0)
    buf = io.StringIO()
    y.dump(doc, buf)
    text = buf.getvalue()
    # ruamel renders the blank-line "comment" as an empty comment line; keep it blank.
    text = text.replace("\n#\n", "\n\n")
    if header:
        text = "".join(f"# {line}".rstrip() + "\n" for line in header) + text
    return text
