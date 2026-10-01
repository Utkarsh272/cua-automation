"""``cua`` command line: artifact tooling, provider check, discovery. Replay arrives on Day 6."""

from __future__ import annotations

import json
from pathlib import Path

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
) -> None:
    """Run the LLM discovery agent once against a live CU Core and save the evidence."""
    from .config import load_dotenv, serve_target, tenant_context
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
        with launch_browser(headless=not headed) as browser:
            trace, run_dir = run_discovery(
                spec,
                browser=browser,
                llm=llm,
                ctx=ctx,
                base_url=ctx.tenant.base_url,
                secrets=env_secrets(),
                evidence_root=out,
                include_screenshot=vision,
                extra_headers={"X-Fault": fault} if fault else None,
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


if __name__ == "__main__":
    app()
