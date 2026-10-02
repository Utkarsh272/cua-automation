from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path

import pytest

from cua.compiler.compile import compile_trace
from cua.core.artifact import ArtifactError, OutcomeDecl
from cua.core.targets import FrameRef
from cua.discovery.trace import DiscoveryTrace
from cua.registry.store import Registry, RegistryError

ROOT = Path(__file__).resolve().parents[2]
REAL = ROOT / "tests" / "fixtures" / "traces" / "groq_lookup_success.json"
NOW = datetime(2026, 10, 1, 12, 0, tzinfo=UTC)


def compiled(version: str = "1.0.0") -> tuple[object, str]:
    t = DiscoveryTrace.model_validate(json.loads(REAL.read_text()))
    r = compile_trace(
        t,
        content_frame=(FrameRef(name="main"),),
        login="login@^1",
        example_inputs={"member_id": "10042"},
        declared_outcomes=(OutcomeDecl(code="MEMBER_NOT_FOUND", description="x"),),
        version=version,
    )
    return r.capability, r.yaml


@pytest.fixture
def reg(tmp_path: Path) -> Registry:
    r = Registry(tmp_path)
    cap, text = compiled()
    r.save_draft(cap, text)  # type: ignore[arg-type]
    return r


def test_draft_overwrite_needs_replace(reg: Registry) -> None:
    cap, text = compiled()
    with pytest.raises(RegistryError, match="exists"):
        reg.save_draft(cap, text)  # type: ignore[arg-type]
    reg.save_draft(cap, text, replace=True)  # type: ignore[arg-type]


def test_four_eyes(reg: Registry) -> None:
    with pytest.raises(RegistryError, match="four-eyes"):
        reg.approve(
            "lookup_savings_balance", "1.0.0", approver="discovery:groq/openai/gpt-oss-120b"
        )
    cap = reg.approve(
        "lookup_savings_balance",
        "1.0.0",
        approver="discovery:groq/openai/gpt-oss-120b",
        allow_self_approval=True,
        now=NOW,
    )
    assert cap.status == "approved"


def test_approval_records_who_when_and_hash_and_keeps_comments(reg: Registry) -> None:
    cap = reg.approve("lookup_savings_balance", "1.0.0", approver="utkarsh.mittal", now=NOW)
    assert (cap.status, cap.approved_by) == ("approved", "utkarsh.mittal")
    assert cap.approved_at == NOW and cap.content_hash and cap.content_hash.startswith("sha256:")
    text = reg.path("lookup_savings_balance", "1.0.0").read_text()
    assert text.startswith("# lookup_savings_balance 1.0.0, compiled by `cua compile`")
    with pytest.raises(RegistryError, match="already approved"):
        reg.approve("lookup_savings_balance", "1.0.0", approver="someone.else")


def test_approved_versions_are_immutable(reg: Registry) -> None:
    reg.approve("lookup_savings_balance", "1.0.0", approver="utkarsh.mittal", now=NOW)
    cap, text = compiled()
    with pytest.raises(RegistryError, match="immutable"):
        reg.save_draft(cap, text, replace=True)  # type: ignore[arg-type]
    path = reg.path("lookup_savings_balance", "1.0.0")
    path.write_text(path.read_text().replace("Member ID", "Member Number"))
    with pytest.raises(ArtifactError, match="content_hash mismatch"):
        reg.load("lookup_savings_balance", "1.0.0")


def test_resolve_by_constraint(reg: Registry) -> None:
    with pytest.raises(RegistryError, match="no runnable version"):
        reg.resolve("lookup_savings_balance", "^1")
    assert reg.resolve("lookup_savings_balance", "^1", allow_draft=True).status == "draft"
    reg.approve("lookup_savings_balance", "1.0.0", approver="utkarsh.mittal", now=NOW)
    cap2, text2 = compiled("1.1.0")
    reg.save_draft(cap2, text2)  # type: ignore[arg-type]
    assert reg.resolve("lookup_savings_balance", "^1").version == "1.0.0"  # 1.1.0 is a draft
    assert reg.resolve("lookup_savings_balance", "^1", allow_draft=True).version == "1.1.0"
    reg.deprecate("lookup_savings_balance", "1.0.0")
    with pytest.raises(RegistryError):
        reg.resolve("lookup_savings_balance", "^1")
    assert reg.resolve("lookup_savings_balance", "1.0.0").status == "deprecated"  # exact pin


def test_only_approved_can_be_deprecated(reg: Registry) -> None:
    with pytest.raises(RegistryError, match="only approved"):
        reg.deprecate("lookup_savings_balance", "1.0.0")


def test_file_name_must_match_content(reg: Registry) -> None:
    src = reg.path("lookup_savings_balance", "1.0.0")
    dst = reg.path("lookup_savings_balance", "2.0.0")
    dst.write_text(src.read_text())
    with pytest.raises(RegistryError, match="disagree"):
        reg.load("lookup_savings_balance", "2.0.0")


def test_repo_registry_lists_real_capabilities() -> None:
    reg = Registry(ROOT / "capabilities")
    assert "login" in reg.ids() and "lookup_savings_balance" in reg.ids()
    assert reg.resolve("login", "^1").status == "approved"
