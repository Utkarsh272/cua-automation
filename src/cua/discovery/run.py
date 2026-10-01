"""Orchestrate one discovery run: open the surface, sign on, run the agent, write evidence."""

from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from playwright.sync_api import Browser

from cua.core.policy import PolicyGuard
from cua.core.redact import Redactor
from cua.core.text import route_of
from cua.evidence.store import RunEvidence, new_run_id
from cua.llm.base import LLMClient
from cua.runtime.steps import StepError, run_login
from cua.surface.web import WebSurface

from .agent import DiscoveryAgent
from .spec import DiscoverySpec
from .trace import DiscoveryTrace


def run_discovery(
    spec: DiscoverySpec,
    *,
    browser: Browser,
    llm: LLMClient,
    ctx: Any,  # cua.config.TenantContext (or a test double with the same fields)
    base_url: str,
    secrets: Callable[[str, str], str],
    evidence_root: str | Path,
    guard: PolicyGuard | None = None,
    include_screenshot: bool = False,
    extra_headers: dict[str, str] | None = None,
) -> tuple[DiscoveryTrace, Path]:
    run_id = new_run_id("discovery")
    guard = guard or PolicyGuard(ctx.policy)
    sensitivities = {k: v.sensitivity.value for k, v in spec.inputs.properties.items()}
    secret_values = [secrets(ctx.tenant.secrets_prefix, k) for k in ("username", "password")]

    def redactor() -> Redactor:
        return Redactor.for_run(
            sensitivities,
            spec.example_inputs,
            secrets=secret_values,
            extra_patterns=ctx.profile.redaction_patterns,
        )

    evidence = RunEvidence(evidence_root, run_id, redactor())
    trace = DiscoveryTrace(
        run_id=run_id,
        capability_id=spec.capability_id,
        description=spec.description,
        goal=spec.goal,
        tenant=spec.tenant,
        app_product=ctx.tenant.product,
        app_version=ctx.tenant.product_version,
        login=ctx.login.ref,
        provider=llm.provider,
        model=llm.model,
        started_at=datetime.now(UTC),
        risk_ceiling=spec.risk_ceiling,
        inputs=spec.inputs,
        outputs=spec.outputs,
        start_route=spec.start_route,
    )
    evidence.event(
        "run_started",
        run_id=run_id,
        capability_id=spec.capability_id,
        goal=spec.goal,
        tenant=spec.tenant,
        provider=llm.provider,
        model=llm.model,
        risk_ceiling=spec.risk_ceiling.value,
        max_steps=spec.max_steps,
    )

    surface = WebSurface.open(
        browser,
        base_url,
        extra_headers=extra_headers,
        content_frame=ctx.profile.content_frame,
        url_guard=lambda u: guard.check_url(u).allowed,
    )
    try:
        surface.start("/")
        try:
            run_login(surface, ctx.login, secrets, ctx.profile.detectors)
        except StepError as exc:
            evidence.event("login_failed", code=exc.code.value, observed=exc.observed)
            trace.status, trace.reason = "failed", f"login failed: {exc}"
            trace.finished_at = datetime.now(UTC)
            return trace, _close(evidence, trace, surface)
        evidence.event("login_ok", login=ctx.login.ref)
        if route_of(surface.content_url) != spec.start_route:
            surface.navigate(spec.start_route, ctx.profile.content_frame)

        agent = DiscoveryAgent(
            surface=surface,
            llm=llm,
            spec=spec,
            guard=guard,
            evidence=evidence,
            trace=trace,
            llm_redactor=redactor(),
            include_screenshot=include_screenshot,
        )
        agent.run()
        evidence.capture(surface, "final", [])
        return trace, _close(evidence, trace, surface)
    except Exception:
        surface.close()
        raise


def _close(evidence: RunEvidence, trace: DiscoveryTrace, surface: WebSurface) -> Path:
    evidence.write_json("trace", trace.model_dump(mode="json"))
    evidence.write_json(
        "result",
        {
            "run_id": trace.run_id,
            "status": trace.status,
            "reason": trace.reason,
            "provider": trace.provider,
            "model": trace.model,
            "steps": len(trace.steps),
            "llm_calls": trace.llm_calls,
            "input_tokens": trace.input_tokens,
            "output_tokens": trace.output_tokens,
            "outputs": trace.extracted,
            "verified_outputs": trace.verified_outputs,
            "outcome": trace.outcome.model_dump() if trace.outcome else None,
        },
    )
    evidence.finalize()
    surface.close()
    return evidence.dir
