"""``cua`` command line: artifact tooling, discovery, compile, approve, replay, operator console."""

from __future__ import annotations

import json
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

import typer
from pydantic import ValidationError
from ruamel.yaml import YAML

from .core.artifact import (
    ArtifactError,
    Capability,
    capability_json_schema,
    compute_hash,
    load_capability,
    to_tool_definition,
)

app = typer.Typer(help="Computer-use automation: discover once, replay deterministically.")


def _roundtrip() -> YAML:
    y = YAML()  # round-trip mode keeps comments and key order
    y.preserve_quotes = True
    y.width = 120
    return y


@app.command()
def validate(path: Path) -> None:
    """Validate an artifact against the schema and check its content hash."""
    try:
        cap = load_capability(path)
    except ArtifactError as exc:
        typer.secho(f"INVALID {path}\n{exc}", fg=typer.colors.RED, err=True)
        raise typer.Exit(1) from exc
    hash_note = "hash ok" if cap.content_hash else "no hash (draft)"
    typer.secho(
        f"OK {cap.ref} [{cap.status}] risk={cap.risk.value} steps={len(cap.steps)} "
        f"detectors={len(cap.detectors)} {hash_note}",
        fg=typer.colors.GREEN,
    )


@app.command("hash")
def hash_cmd(
    path: Path,
    write: bool = typer.Option(
        False, "--write", help="Store the hash in the file (keeps comments)."
    ),
) -> None:
    """Print (or write) the content hash of an artifact.

    The hash covers behavior only; approval fields (status, approved_by, approved_at) are excluded,
    so approving does not change identity but any behavioral edit does.
    """
    y = _roundtrip()
    data = y.load(path.read_text(encoding="utf-8"))
    try:
        cap = Capability.model_validate(json.loads(json.dumps(data)))
    except ValidationError as exc:
        typer.secho(str(exc), fg=typer.colors.RED, err=True)
        raise typer.Exit(1) from exc
    digest = compute_hash(cap)
    if write:
        if "content_hash" in data:
            data["content_hash"] = digest
        else:
            data.insert(list(data.keys()).index("version") + 1, "content_hash", digest)
        with path.open("w", encoding="utf-8") as fh:
            y.dump(data, fh)
    typer.echo(digest)


@app.command()
def tool(path: Path) -> None:
    """Show the capability as an agent tool definition."""
    typer.echo(json.dumps(to_tool_definition(load_capability(path)), indent=2))


@app.command()
def schema(
    out: Path | None = typer.Option(None, help="Write to this file instead of stdout."),
) -> None:
    """Export the artifact JSON Schema."""
    text = json.dumps(capability_json_schema(), indent=2, sort_keys=True) + "\n"
    if out:
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(text, encoding="utf-8")
        typer.echo(f"wrote {out}")
    else:
        typer.echo(text)


@app.command("llm-check")
def llm_check(
    provider: str = typer.Option(
        "groq", help="groq | gemini | openrouter | cerebras | ollama | anthropic"
    ),
    model: str | None = typer.Option(None, help="Override the provider's default model."),
) -> None:
    """One tiny tool-calling request, to confirm the key, the model and tool support work."""
    from .config import load_dotenv
    from .llm.base import LLMError, ToolSpec
    from .llm.providers import make_client

    load_dotenv()
    try:
        llm = make_client(provider, model)
        resp = llm.decide(
            "You are a connectivity check. Always call the ping tool.",
            "Call ping with ok=true.",
            [
                ToolSpec(
                    "ping",
                    "Confirm you can call tools.",
                    {
                        "type": "object",
                        "properties": {"ok": {"type": "boolean"}},
                        "required": ["ok"],
                    },
                )
            ],
        )
    except LLMError as exc:
        typer.secho(f"FAILED {provider}: {exc}", fg=typer.colors.RED, err=True)
        raise typer.Exit(1) from exc
    if resp.call is None or resp.call.name != "ping":
        typer.secho(
            f"{provider}/{resp.model} answered without calling the tool: {resp.text[:200]!r}",
            fg=typer.colors.YELLOW,
        )
        raise typer.Exit(1)
    typer.secho(
        f"OK {provider}/{resp.model} tool call={resp.call.args} latency={resp.latency_ms}ms "
        f"tokens in/out={resp.input_tokens}/{resp.output_tokens} retries={resp.retries}",
        fg=typer.colors.GREEN,
    )


@app.command()
def discover(
    goal_file: Path = typer.Argument(
        ..., help="Discovery spec, e.g. goals/lookup_savings_balance.yaml"
    ),
    provider: str = typer.Option(
        "groq", help="groq | gemini | openrouter | cerebras | ollama | anthropic"
    ),
    model: str | None = typer.Option(None, help="Override the provider's default model."),
    out: Path = typer.Option(Path("runs"), help="Evidence root; the run gets its own folder."),
    serve: bool = typer.Option(
        True, "--serve/--no-serve", help="Start the local CU Core for the tenant."
    ),
    headed: bool = typer.Option(False, help="Show the browser window."),
    vision: bool = typer.Option(False, help="Also send a (redacted) screenshot each turn."),
    input_: list[str] = typer.Option([], "--input", help="Override an example input: name=value"),
    fault: str | None = typer.Option(None, help="CU Core fault header, e.g. notice"),
    console: bool = typer.Option(
        False, help="Pause for a human at risky steps (operator console; shows the browser)."
    ),
    console_port: int = typer.Option(8090, help="Port for the operator console."),
) -> None:
    """Run the LLM discovery agent once against a live CU Core and save the evidence."""
    from .config import load_dotenv, serve_target, tenant_context
    from .core.policy import PolicyGuard
    from .core.templating import env_secrets
    from .discovery.run import run_discovery
    from .discovery.spec import DiscoverySpec, load_spec
    from .llm.base import LLMError
    from .llm.providers import make_client
    from .surface.web import launch_browser

    load_dotenv()
    spec = load_spec(goal_file)
    if input_:
        overrides = dict(kv.split("=", 1) for kv in input_)
        data = spec.model_dump(mode="json", by_alias=True)
        data["example_inputs"] = {**spec.example_inputs, **overrides}
        spec = DiscoverySpec.model_validate(data)
    ctx = tenant_context(spec.tenant)
    try:
        llm = make_client(provider, model)
    except LLMError as exc:
        typer.secho(str(exc), fg=typer.colors.RED, err=True)
        raise typer.Exit(2) from exc

    server = serve_target(ctx.tenant) if serve else None
    typer.echo(
        f"discovering {spec.capability_id} on {ctx.tenant.base_url} with {provider}/{llm.model}"
    )
    try:
        with (
            _operator_console(console, console_port) as handoff,
            launch_browser(headless=not (headed or console)) as browser,
        ):
            trace, run_dir = run_discovery(
                spec,
                browser=browser,
                llm=llm,
                ctx=ctx,
                base_url=ctx.tenant.base_url,
                secrets=env_secrets(),
                evidence_root=out,
                guard=PolicyGuard(ctx.policy, _tokens()),  # type: ignore[arg-type]
                include_screenshot=vision,
                extra_headers={"X-Fault": fault} if fault else None,
                handoff=handoff,
            )
    finally:
        if server is not None:
            server.should_exit = True

    color = (
        typer.colors.GREEN
        if trace.status in ("succeeded", "business_outcome")
        else typer.colors.RED
    )
    typer.secho(f"\n{trace.status.upper()}: {trace.reason}", fg=color, bold=True)
    for st in trace.steps:
        el = st.element.label or st.element.name if st.element else ""
        extra = f" -> {st.output}" if st.output else ""
        note = f" [{st.policy.verdict}]" if st.policy and st.policy.verdict != "allow" else ""
        err = f" ({st.error})" if st.error else ""
        typer.echo(f"  {st.index:>2}. {st.tool:<15} {el!s:<30} {st.status}{note}{extra}{err}")
    typer.echo(
        f"\nmodel {trace.provider}/{trace.model}: {trace.llm_calls} calls, "
        f"{trace.input_tokens} in / {trace.output_tokens} out tokens"
    )
    typer.echo(f"evidence: {run_dir}")
    if trace.status not in ("succeeded", "business_outcome"):
        raise typer.Exit(1)


@app.command("compile")
def compile_cmd(
    trace_file: Path = typer.Argument(..., help="trace.json of a succeeded discovery run"),
    outcome_trace: list[Path] = typer.Option(
        [], "--outcome-trace", help="trace.json of a run that ended in a business outcome"
    ),
    goal: Path | None = typer.Option(None, help="Discovery spec (default: goals/<id>.yaml)"),
    version: str = typer.Option("1.0.0", help="Version to write"),
    replace: bool = typer.Option(False, help="Overwrite an existing draft of this version"),
    registry_root: Path = typer.Option(Path("capabilities"), "--registry"),
) -> None:
    """Compile discovery trace(s) into a capability draft plus review notes. No LLM involved."""
    from .compiler.compile import CompileError, compile_trace
    from .config import tenant_context
    from .discovery.spec import load_spec
    from .discovery.trace import DiscoveryTrace
    from .registry.store import Registry, RegistryError

    def read(p: Path) -> DiscoveryTrace:
        return DiscoveryTrace.model_validate_json(p.read_text(encoding="utf-8"))

    trace = read(trace_file)
    outcomes = [read(p) for p in outcome_trace]
    ctx = tenant_context(trace.tenant)
    goal = goal or Path("goals") / f"{trace.capability_id}.yaml"
    spec = load_spec(goal) if goal.exists() else None
    try:
        result = compile_trace(
            trace,
            content_frame=ctx.profile.content_frame,
            login=ctx.profile.login,
            outcome_traces=outcomes,
            example_inputs=spec.example_inputs if spec else None,
            declared_outcomes=spec.outcomes if spec else (),
            subject=spec.subject if spec else None,
            version=version,
        )
    except CompileError as exc:
        for n in exc.notes:
            typer.secho(n.line(), fg=typer.colors.RED if n.level == "error" else None, err=True)
        raise typer.Exit(1) from exc
    reg = Registry(registry_root)
    try:
        path = reg.save_draft(result.capability, result.yaml, replace=replace)
    except RegistryError as exc:
        typer.secho(str(exc), fg=typer.colors.RED, err=True)
        raise typer.Exit(1) from exc
    review = path.with_suffix(".review.md")
    review.write_text(result.review_markdown, encoding="utf-8")
    for n in result.notes:
        color = {"warn": typer.colors.YELLOW, "error": typer.colors.RED}.get(n.level)
        typer.secho(n.line(), fg=color)
    cap = result.capability
    typer.secho(
        f"\nwrote {path} ({len(cap.steps)} steps, {len(cap.detectors)} detectors) and {review}",
        fg=typer.colors.GREEN,
    )


@app.command()
def approve(
    cap_id: str,
    version: str,
    by: str = typer.Option(..., "--by", help="Who reviewed and approves this version"),
    allow_self_approval: bool = typer.Option(
        False, help="Let the recorder approve their own draft (single-person demo only)"
    ),
    registry_root: Path = typer.Option(Path("capabilities"), "--registry"),
) -> None:
    """Approve a reviewed draft: sets status, approver and the behavior hash. Then immutable."""
    from .registry.store import Registry, RegistryError

    try:
        cap = Registry(registry_root).approve(
            cap_id, version, approver=by, allow_self_approval=allow_self_approval
        )
    except (RegistryError, ValueError) as exc:
        typer.secho(str(exc), fg=typer.colors.RED, err=True)
        raise typer.Exit(1) from exc
    typer.secho(
        f"approved {cap.ref} by {cap.approved_by} ({cap.content_hash})", fg=typer.colors.GREEN
    )


@app.command("list")
def list_cmd(registry_root: Path = typer.Option(Path("capabilities"), "--registry")) -> None:
    """List capabilities and versions with their status."""
    from .registry.store import Registry

    reg = Registry(registry_root)
    for cap_id in reg.ids():
        for v in reg.versions(cap_id):
            try:
                cap = reg.load(cap_id, v)
                line = (
                    f"{cap.ref:<34} {cap.status:<10} risk={cap.risk.value:<16} "
                    f"steps={len(cap.steps):<3} detectors={len(cap.detectors):<3} "
                    f"origin={cap.provenance.origin}"
                )
                typer.echo(line)
            except Exception as exc:
                typer.secho(f"{cap_id}@{v:<28} INVALID: {exc}", fg=typer.colors.RED)


@app.command()
def tools(
    tenant: str = typer.Option("tenant-a"),
    write: bool = typer.Option(False, help="Include write capabilities (caller has write scope)"),
    registry_root: Path = typer.Option(Path("capabilities"), "--registry"),
) -> None:
    """Print the agent tool catalog for a tenant: approved, compatible capabilities only."""
    from .config import tenant_context
    from .registry.catalog import tool_catalog
    from .registry.store import Registry

    ctx = tenant_context(tenant)
    scopes = frozenset({"invoke:*"} | ({"write"} if write else set()))
    catalog = tool_catalog(
        Registry(registry_root),
        tenant=tenant,
        product=ctx.tenant.product,
        product_version=ctx.tenant.product_version,
        scopes=scopes,
    )
    typer.echo(json.dumps(catalog, indent=2))


@app.command()
def probe(
    cap_id: str = typer.Argument(..., help="Capability id, e.g. lookup_savings_balance"),
    version: str = typer.Option("^1", help="Version or constraint to probe"),
    tenant: str = typer.Option(..., help="Tenant to check the capability against"),
    input_: list[str] = typer.Option([], "--input", help="Example input: name=value"),
    draft: str | None = typer.Option(
        None, help="Write the proposals as a new draft version (e.g. 1.1.0) for review"
    ),
    by: str = typer.Option("onboarding-probe", help="Recorded as the author of the draft"),
    serve: bool = typer.Option(True, "--serve/--no-serve", help="Start the local CU Core"),
    registry_root: Path = typer.Option(Path("capabilities"), "--registry"),
) -> None:
    """Check a read-only capability against a tenant and propose overrides. No LLM involved."""
    from .compiler.compile import readable_dict
    from .compiler.yaml_out import dump
    from .config import load_dotenv, serve_target, tenant_context
    from .core.artifact import ArtifactError, parse_capability
    from .core.templating import env_secrets
    from .onboarding.probe import Probe
    from .registry.store import Registry, RegistryError
    from .surface.web import launch_browser

    load_dotenv()
    reg = Registry(registry_root)
    try:
        cap = reg.resolve(cap_id, version, allow_draft=True)
    except RegistryError as exc:
        typer.secho(str(exc), fg=typer.colors.RED, err=True)
        raise typer.Exit(2) from exc
    ctx = tenant_context(tenant)
    server = serve_target(ctx.tenant) if serve else None
    typer.echo(
        f"probing {cap.ref} on {tenant} ({ctx.tenant.base_url}, v{ctx.tenant.product_version})"
    )
    try:
        with launch_browser() as browser:
            report = Probe(browser=browser, ctx=ctx, secrets=env_secrets()).run(cap, _kv(input_))
    finally:
        if server is not None:
            server.should_exit = True

    colors = {
        "proposed": typer.colors.YELLOW,
        "degraded": typer.colors.YELLOW,
        "blocked": typer.colors.RED,
        "ok": typer.colors.GREEN,
    }
    for f in report.findings:
        typer.secho(f.line(), fg=colors.get(f.level))
    if not report.works:
        typer.secho("\nNOT WORKING on this tenant; see the blocked finding.", fg=typer.colors.RED)
        raise typer.Exit(1)
    if not report.ops:
        typer.secho(f"\n{cap.ref} works on {tenant} as is.", fg=typer.colors.GREEN, bold=True)
        return
    typer.secho(
        f"\n{cap.ref} works on {tenant} with {len(report.ops)} override(s) "
        f"(verified in {report.rounds} rounds):",
        fg=typer.colors.GREEN,
        bold=True,
    )
    typer.echo(dump(report.proposal()))
    if draft is None:
        typer.echo("Re-run with --draft <new version> to write these as a draft for review.")
        return

    data = readable_dict(cap)
    data.update(version=draft, status="draft", recorded_by=by)
    for key in ("content_hash", "approved_by", "approved_at"):
        data.pop(key, None)
    note = f"{draft} adds {tenant} overrides proposed by the locator probe from {cap.ref}."
    data["provenance"]["notes"] = f"{data['provenance'].get('notes') or ''} {note}".strip()
    data["overrides"] = {
        **data.get("overrides", {}),
        tenant: [*data.get("overrides", {}).get(tenant, []), *report.ops],
    }
    header = [
        f"{cap.id} {draft}: {cap.ref} plus overrides for {tenant}, proposed by `cua probe`.",
        "Steps and locators for other tenants are unchanged. Review the overrides section, then",
        f"`cua approve {cap.id} {draft} --by <you>`.",
    ]
    text = dump(
        data,
        header=header,
        gaps_before=(
            "description",
            "inputs",
            "outcomes",
            "steps",
            "detectors",
            "success",
            "overrides",
        ),
    )
    try:
        new_cap = parse_capability(_roundtrip().load(text), verify_hash=False)
        path = reg.save_draft(new_cap, text)
    except (ArtifactError, RegistryError) as exc:
        typer.secho(str(exc), fg=typer.colors.RED, err=True)
        raise typer.Exit(1) from exc
    typer.secho(f"wrote {path}", fg=typer.colors.GREEN)


@app.command()
def diff(a: Path, b: Path) -> None:
    """Behavioral diff of two capability files (ignores status, approval and provenance)."""
    import difflib

    from .compiler.compile import readable_dict
    from .compiler.yaml_out import dump

    skip = {"content_hash", "status", "approved_by", "approved_at", "recorded_by", "provenance"}

    def norm(p: Path) -> list[str]:
        data = readable_dict(load_capability(p, verify_hash=False))
        return dump({k: v for k, v in data.items() if k not in skip}).splitlines(keepends=True)

    lines = list(difflib.unified_diff(norm(a), norm(b), fromfile=str(a), tofile=str(b)))
    if not lines:
        typer.echo("no behavioral differences")
        return
    for line in lines:
        color = (
            typer.colors.GREEN
            if line.startswith("+")
            else typer.colors.RED
            if line.startswith("-")
            else None
        )
        typer.secho(line.rstrip("\n"), fg=color)


def _caller(tenant: str, on_behalf_of: str | None, write: bool) -> object:
    import getpass

    from .core.authz import Caller

    scopes = {"invoke:*"} | ({"write"} if write else set())
    if on_behalf_of is None:
        scopes.add("staff")  # staff-assist use; recorded in the audit trail
    return Caller(f"cli:{getpass.getuser()}", frozenset(scopes), frozenset({tenant}), on_behalf_of)


def _tokens() -> object | None:
    import os

    from .core.confirmation import ConfirmationTokens

    secret = os.environ.get("CUA_CONFIRMATION_SECRET", "")
    if len(secret) < 16 or secret.startswith("change-me"):
        return None
    return ConfirmationTokens(secret.encode())


@contextmanager
def _operator_console(enabled: bool, port: int) -> Iterator[Any]:
    """Start the operator console for this run and yield the handoff (None when disabled)."""
    if not enabled:
        yield None
        return
    from .config import ROOT
    from .handoff.session import Handoff
    from .handoff.store import InterventionStore
    from .operator.console import ConsoleServer, load_operators

    accounts = load_operators(ROOT / "config" / "operators.yaml")
    problems = []
    if not accounts:
        problems.append("set CUA_OPERATOR_TOKEN (16+ chars) in .env so an operator can sign in")
    if _tokens() is None:
        problems.append(
            "set CUA_CONFIRMATION_SECRET (16+ chars) in .env so approvals can be signed"
        )
    if problems:
        for line in problems:
            typer.secho(line, fg=typer.colors.RED, err=True)
        raise typer.Exit(2)
    store = InterventionStore("interventions")
    with ConsoleServer(store, accounts, port=port) as server:
        typer.secho(f"operator console: {server.url}", fg=typer.colors.CYAN, bold=True)
        yield Handoff(
            store,
            announce=lambda msg: typer.secho(
                f"PAUSED: {msg}\n  open {server.url}", fg=typer.colors.YELLOW, bold=True
            ),
        )


def _kv(pairs: list[str]) -> dict[str, str]:
    out = {}
    for kv in pairs:
        key, sep, value = kv.partition("=")
        if not sep:
            raise typer.BadParameter(f"expected name=value, got {kv!r}")
        out[key] = value
    return out


@app.command()
def replay(
    cap_id: str = typer.Argument(..., help="Capability id, e.g. lookup_savings_balance"),
    version: str = typer.Option("^1", help="Version or constraint (^1, 1.0.1, ...)"),
    tenant: str = typer.Option("tenant-a"),
    input_: list[str] = typer.Option([], "--input", help="name=value (repeatable)"),
    on_behalf_of: str | None = typer.Option(
        None, help="End-user principal (subject binding). Omit for staff-assist mode (audited)."
    ),
    write: bool = typer.Option(False, help="Grant the write scope (needed for write capabilities)"),
    attended: bool = typer.Option(False, help="Allow draft versions (a person is watching)"),
    run_id: str | None = typer.Option(
        None, help="Run id (needed when passing confirmation tokens)"
    ),
    confirm: list[str] = typer.Option([], "--confirm", help="step_id=token for irreversible steps"),
    fault: str | None = typer.Option(
        None, help="CU Core fault header, e.g. notice or error500=/member"
    ),
    out: Path = typer.Option(Path("runs"), help="Evidence root"),
    serve: bool = typer.Option(True, "--serve/--no-serve", help="Start the local CU Core"),
    headed: bool = typer.Option(False, help="Show the browser"),
    console: bool = typer.Option(
        False, help="Pause the live session for a human instead of stopping (shows the browser)."
    ),
    console_port: int = typer.Option(8090, help="Port for the operator console."),
    registry_root: Path = typer.Option(Path("capabilities"), "--registry"),
) -> None:
    """Replay a capability deterministically (no LLM) and print the typed result."""
    from .config import load_dotenv, serve_target, tenant_context
    from .core.authz import StaticOwnership
    from .core.policy import PolicyGuard
    from .core.results import result_json
    from .core.templating import env_secrets
    from .registry.store import Registry, RegistryError
    from .replay.engine import ReplayEngine
    from .surface.web import launch_browser

    load_dotenv()
    try:
        cap = Registry(registry_root).resolve(cap_id, version, allow_draft=attended)
    except RegistryError as exc:
        typer.secho(str(exc), fg=typer.colors.RED, err=True)
        raise typer.Exit(2) from exc
    ctx = tenant_context(tenant)
    ownership = StaticOwnership(
        {p: frozenset(m) for p, m in ctx.tenant.members_by_principal.items()}
    )
    guard = PolicyGuard(ctx.policy, _tokens())  # type: ignore[arg-type]
    server = serve_target(ctx.tenant) if serve else None
    typer.echo(f"replaying {cap.ref} [{cap.status}] on {tenant} ({ctx.tenant.base_url}); no LLM")
    try:
        with (
            _operator_console(console, console_port) as handoff,
            launch_browser(headless=not (headed or console)) as browser,
        ):
            engine = ReplayEngine(
                browser=browser,
                ctx=ctx,
                secrets=env_secrets(),
                evidence_root=out,
                guard=guard,
                extra_headers={"X-Fault": fault} if fault else None,
                handoff=handoff,
            )
            result = engine.run(
                cap,
                _kv(input_),
                caller=_caller(tenant, on_behalf_of, write),  # type: ignore[arg-type]
                ownership=ownership,
                attended=attended,
                confirmations=_kv(confirm),
                run_id=run_id,
            )
    finally:
        if server is not None:
            server.should_exit = True
    color = {"success": typer.colors.GREEN, "business_outcome": typer.colors.YELLOW}.get(
        result.kind, typer.colors.RED
    )
    typer.secho(result_json(result), fg=color)
    typer.echo(f"evidence: {out / result.run_id}")
    raise typer.Exit(
        {"success": 0, "business_outcome": 0, "failure": 1, "needs_human": 3}[result.kind]
    )


@app.command()
def stability(
    cap_id: str = typer.Argument(..., help="Read-only capability id"),
    version: str = typer.Option("^1", help="Version or constraint"),
    tenant: str = typer.Option("tenant-a"),
    runs: int = typer.Option(10, min=2, max=200, help="How many times to replay"),
    input_: list[str] = typer.Option([], "--input", help="name=value (repeatable)"),
    fault: str | None = typer.Option(None, help="CU Core fault header for every run"),
    out: Path = typer.Option(Path("runs"), help="Evidence root (each run plus the report)"),
    serve: bool = typer.Option(True, "--serve/--no-serve", help="Start the local CU Core"),
    registry_root: Path = typer.Option(Path("capabilities"), "--registry"),
) -> None:
    """Replay a capability N times (no LLM) and report how consistently it behaves."""
    from .config import load_dotenv, serve_target, tenant_context
    from .core.authz import StaticOwnership
    from .core.policy import PolicyGuard
    from .core.templating import env_secrets
    from .evidence.store import new_run_id
    from .registry.store import Registry, RegistryError
    from .replay.engine import ReplayEngine
    from .replay.stability import measure
    from .surface.web import launch_browser

    load_dotenv()
    try:
        cap = Registry(registry_root).resolve(cap_id, version)
    except RegistryError as exc:
        typer.secho(str(exc), fg=typer.colors.RED, err=True)
        raise typer.Exit(2) from exc
    ctx = tenant_context(tenant)
    ownership = StaticOwnership(
        {p: frozenset(m) for p, m in ctx.tenant.members_by_principal.items()}
    )
    inputs = _kv(input_)
    server = serve_target(ctx.tenant) if serve else None
    typer.echo(f"replaying {cap.ref} on {tenant} {runs} times; no LLM")
    try:
        with launch_browser() as browser:
            engine = ReplayEngine(
                browser=browser,
                ctx=ctx,
                secrets=env_secrets(),
                evidence_root=out,
                guard=PolicyGuard(ctx.policy),
                extra_headers={"X-Fault": fault} if fault else None,
            )

            def once() -> Any:
                r = engine.run(
                    cap,
                    inputs,
                    caller=_caller(tenant, None, False),  # type: ignore[arg-type]
                    ownership=ownership,
                )
                typer.echo(f"  {r.run_id}  {r.kind}  {r.duration_ms} ms")
                return r

            try:
                report = measure(once, cap, tenant, runs)
            except ValueError as exc:
                typer.secho(str(exc), fg=typer.colors.RED, err=True)
                raise typer.Exit(2) from exc
    finally:
        if server is not None:
            server.should_exit = True
    path = out / f"{new_run_id('stability')}.json"
    path.write_text(report.to_json(), encoding="utf-8")
    typer.secho(report.to_json(), fg=typer.colors.GREEN if report.stable else typer.colors.RED)
    typer.echo(f"report: {path}")
    raise typer.Exit(0 if report.stable else 1)


@app.command()
def token(
    run_id: str = typer.Option(...),
    step: str = typer.Option(..., help="The irreversible step id, e.g. confirm"),
    input_: list[str] = typer.Option([], "--input", help="name=value, exactly as replay will use"),
    by: str = typer.Option(..., help="Who authorizes this action"),
) -> None:
    """Issue a single-use confirmation token for one irreversible step of one run (demo stand-in
    for the operator console)."""
    from .config import load_dotenv

    load_dotenv()
    tokens = _tokens()
    if tokens is None:
        typer.secho(
            "set CUA_CONFIRMATION_SECRET (16+ chars) in .env", fg=typer.colors.RED, err=True
        )
        raise typer.Exit(2)
    typer.echo(tokens.issue(run_id=run_id, step_id=step, inputs=_kv(input_), issued_to=by))  # type: ignore[attr-defined]


if __name__ == "__main__":
    app()
