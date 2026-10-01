"""Real-browser fixtures: CU Core (tenants a and b) on free ports, one shared Chromium."""

from __future__ import annotations

import os
import re
import socket
import threading
import time
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import httpx
import pytest
import uvicorn

from cua.core.artifact import ActionKind, Capability, load_capability
from cua.core.policy import PolicyGuard, load_policy
from cua.core.profile import AppProfile, load_profile
from target_app.main import create_app
from target_app.state import SERVICE_ACCOUNT, SERVICE_PASSWORD

ROOT = Path(__file__).resolve().parents[2]
SANDBOX_CHROMIUM = "/opt/pw-browsers/chromium"
if not os.environ.get("CUA_CHROMIUM_PATH") and os.path.exists(SANDBOX_CHROMIUM):
    os.environ["CUA_CHROMIUM_PATH"] = SANDBOX_CHROMIUM

from cua.surface.web import WebSurface, launch_browser  # noqa: E402

SECRETS = {
    "cu_core.username": os.environ.get("CU_CORE_USERNAME", SERVICE_ACCOUNT),
    "cu_core.password": os.environ.get("CU_CORE_PASSWORD", SERVICE_PASSWORD),
}


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


class _Server:
    def __init__(self, tenant: str) -> None:
        self.port = _free_port()
        self.url = f"http://127.0.0.1:{self.port}"
        config = uvicorn.Config(create_app(tenant), port=self.port, log_level="warning")
        self.server = uvicorn.Server(config)
        self.thread = threading.Thread(target=self.server.run, daemon=True)

    def __enter__(self) -> _Server:
        self.thread.start()
        deadline = time.monotonic() + 10
        while not self.server.started:
            if time.monotonic() > deadline:
                raise RuntimeError("CU Core did not start")
            time.sleep(0.05)
        return self

    def __exit__(self, *exc: object) -> None:
        self.server.should_exit = True
        self.thread.join(timeout=5)

    def reset(self) -> None:
        httpx.post(f"{self.url}/__test/reset")

    def arm(self, fault: str, path_prefix: str = "/", times: int = 1) -> None:
        r = httpx.post(
            f"{self.url}/__test/arm",
            json={"fault": fault, "path_prefix": path_prefix, "times": times},
        )
        r.raise_for_status()


@pytest.fixture(scope="session")
def servers() -> Iterator[dict[str, _Server]]:
    with _Server("a") as a, _Server("b") as b:
        yield {"a": a, "b": b}


@pytest.fixture(scope="session")
def browser() -> Iterator[Any]:
    try:
        with launch_browser() as b:
            yield b
    except Exception as exc:  # pragma: no cover - environment without a browser
        pytest.skip(f"Chromium not available: {exc}")


@pytest.fixture(scope="session")
def profile() -> AppProfile:
    return load_profile(ROOT / "config" / "apps" / "cu-core.profile.yaml")


@pytest.fixture(scope="session")
def login_cap() -> Capability:
    return load_capability(ROOT / "capabilities" / "login" / "1.0.0.yaml")


@pytest.fixture(scope="session")
def lookup_cap() -> Capability:
    return load_capability(ROOT / "capabilities" / "lookup_savings_balance" / "1.0.0.yaml")


def guard_for(*urls: str) -> PolicyGuard:
    policy = load_policy(ROOT / "config" / "policies" / "cu-core.yaml")
    return PolicyGuard(policy.model_copy(update={"allowed_origins": tuple(urls)}))


class SurfaceFactory:
    def __init__(self, browser: Any, servers: dict[str, _Server], profile: AppProfile) -> None:
        self.browser, self.servers, self.profile = browser, servers, profile
        self.opened: list[WebSurface] = []

    def __call__(self, tenant: str = "a", faults: str | None = None) -> WebSurface:
        server = self.servers[tenant]
        guard = guard_for(server.url)
        surface = WebSurface.open(
            self.browser,
            server.url,
            extra_headers={"X-Fault": faults} if faults else None,
            content_frame=self.profile.content_frame,
            url_guard=lambda u: guard.check_url(u).allowed,
        )
        surface.start("/")
        self.opened.append(surface)
        return surface


@pytest.fixture
def open_surface(
    browser: Any, servers: dict[str, _Server], profile: AppProfile
) -> Iterator[SurfaceFactory]:
    for s in servers.values():
        s.reset()
    factory = SurfaceFactory(browser, servers, profile)
    yield factory
    for surface in factory.opened:
        surface.close()


_TEMPLATE = re.compile(r"\{\{\s*([^}]+?)\s*\}\}")


def render(text: str, inputs: dict[str, str]) -> str:
    """Minimal template rendering for tests. The replay engine (Day 6) owns the real one."""

    def sub(m: re.Match[str]) -> str:
        ref = m.group(1)
        if ref.startswith("inputs."):
            return inputs[ref.split(".", 1)[1]]
        return SECRETS[ref.split(".", 1)[1]]

    return _TEMPLATE.sub(sub, text)


def run_steps(
    surface: WebSurface, cap: Capability, inputs: dict[str, str] | None = None
) -> dict[str, str]:
    """Execute a capability's steps in order, with no detectors or policy.

    A test harness for checking that artifact targets resolve on the real app; it is not the
    replay engine.
    """
    raw: dict[str, str] = {}
    for step in cap.steps:
        if step.action == ActionKind.NAVIGATE:
            assert step.route
            surface.navigate(step.route, step.frame)
            continue
        assert step.target is not None
        res = surface.resolve(step.target)
        if step.action == ActionKind.EXTRACT:
            assert step.output
            raw[step.output] = surface.read(res)
        elif step.action == ActionKind.FILL:
            surface.act(res, "fill", render(step.value or "", inputs or {}))
        elif step.action == ActionKind.SELECT:
            surface.act(res, "select", render(step.option or "", inputs or {}))
        else:
            surface.act(res, step.action.value)  # type: ignore[arg-type]
    return raw
