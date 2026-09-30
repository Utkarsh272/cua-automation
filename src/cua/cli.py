"""``cua`` command line. Day 2 covers artifact tooling; discover/replay arrive in later days."""

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


if __name__ == "__main__":
    app()
