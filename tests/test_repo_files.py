"""Everything committed under capabilities/, config/ and schemas/ must be valid and current."""

from __future__ import annotations

import json
from pathlib import Path

from cua.core.artifact import capability_json_schema, load_capability
from cua.core.policy import load_policy
from cua.core.profile import load_profile, load_tenant
from cua.core.semver import satisfies
from target_app.state import SERVICE_PASSWORD

ROOT = Path(__file__).resolve().parents[1]


def test_all_capabilities_load_and_verify(root: Path) -> None:
    files = sorted((root / "capabilities").glob("*/*.yaml"))
    assert files
    for path in files:
        cap = load_capability(path)
        assert path.parent.name == cap.id and path.stem == cap.version


def test_configs_load_and_agree(root: Path) -> None:
    profile = load_profile(root / "config" / "apps" / "cu-core.profile.yaml")
    policy = load_policy(root / "config" / "policies" / "cu-core.yaml")
    assert policy.app == profile.product
    login = load_capability(root / "capabilities" / "login" / "1.0.0.yaml")
    assert login.status == "approved"
    for tenant_file in (root / "config" / "tenants").glob("*.yaml"):
        tenant = load_tenant(tenant_file)
        assert tenant.product == profile.product
        assert (root / tenant.policy).exists()
        lookup = load_capability(root / "capabilities" / "lookup_savings_balance" / "1.0.0.yaml")
        assert satisfies(tenant.product_version, lookup.app.versions)


def test_committed_json_schema_is_current(root: Path) -> None:
    committed = json.loads((root / "schemas" / "capability.schema.json").read_text())
    assert committed == json.loads(json.dumps(capability_json_schema(), sort_keys=True)), (
        "run: uv run cua schema --out schemas/capability.schema.json"
    )


def test_no_credentials_in_repo_files(root: Path) -> None:
    """The synthetic service password lives only in .env.example and the target app itself.

    Artifacts, configs, docs and every other module must use {{secrets.*}} references.
    """
    allowed = {".env.example", "state.py", "test_redact.py"}
    suffixes = {".py", ".yaml", ".yml", ".md", ".json", ".html"}
    for path in root.rglob("*"):
        skip = {".venv", ".git", "__pycache__"} & set(path.parts)
        if path.is_file() and not skip and path.suffix in suffixes and path.name not in allowed:
            text = path.read_text(encoding="utf-8", errors="ignore")
            assert SERVICE_PASSWORD not in text, path


def test_report_uses_the_seven_required_headings() -> None:
    """The brief asks for these exact headings, in this order."""
    headings = [
        line[3:].strip()
        for line in (ROOT / "REPORT.md").read_text(encoding="utf-8").splitlines()
        if line.startswith("## ")
    ]
    assert headings == [
        "1. Architecture",
        "2. Artifact schema",
        "3. Determinism & error handling",
        "4. Heterogeneity & multi-tenant",
        "5. Escalation & handoff",
        "6. Safety",
        "7. Cuts",
    ]
