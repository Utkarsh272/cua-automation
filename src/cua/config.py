"""Project configuration: paths, .env loading, and wiring tenants to their policy and profile."""

from __future__ import annotations

import os
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from cua.core.artifact import Capability, load_capability
from cua.core.policy import PolicyConfig, PolicyGuard, load_policy
from cua.core.profile import AppProfile, TenantConfig, load_profile, load_tenant

ROOT = Path(__file__).resolve().parents[2]


def load_dotenv(path: Path | None = None) -> None:
    """Minimal .env loader: KEY=VALUE lines; existing environment variables win."""
    path = path or ROOT / ".env"
    if not path.exists():
        return
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        value = value.strip().strip('"').strip("'")
        if key.strip() and value:
            os.environ.setdefault(key.strip(), value)


@dataclass(frozen=True)
class TenantContext:
    tenant: TenantConfig
    profile: AppProfile
    policy: PolicyConfig
    login: Capability

    @property
    def guard(self) -> PolicyGuard:
        return PolicyGuard(self.policy)

    def url_guard(self) -> Any:
        guard = self.guard
        return lambda url: guard.check_url(url).allowed


def tenant_context(tenant_id: str, root: Path = ROOT) -> TenantContext:
    tenant = load_tenant(root / "config" / "tenants" / f"{tenant_id}.yaml")
    profile = load_profile(root / "config" / "apps" / f"{tenant.product}.profile.yaml")
    policy = load_policy(root / tenant.policy)
    req_id, _constraint = profile.login.split("@", 1)
    login = _latest(root / "capabilities" / req_id)
    return TenantContext(tenant, profile, policy, login)


def _latest(folder: Path) -> Capability:
    from cua.core.semver import parse_version

    files = sorted(folder.glob("*.yaml"), key=lambda p: parse_version(p.stem))
    if not files:
        raise FileNotFoundError(f"no capability versions in {folder}")
    return load_capability(files[-1])


def serve_target(tenant: TenantConfig) -> Any:
    """Start the local CU Core for this tenant in a background thread (demo convenience)."""
    import uvicorn

    from target_app.main import create_app

    parts = urlsplit(tenant.base_url)
    letter = tenant.id.rsplit("-", 1)[-1]
    server = uvicorn.Server(
        uvicorn.Config(
            create_app(letter),
            host=parts.hostname or "127.0.0.1",
            port=parts.port or 8080,
            log_level="warning",
        )
    )
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    deadline = time.monotonic() + 10
    while not server.started:
        if time.monotonic() > deadline:
            raise RuntimeError(f"could not start CU Core on {tenant.base_url} (port in use?)")
        time.sleep(0.05)
    return server
