# cua-automation

An LLM works out how to complete a task in a UI that has no API. The successful run is compiled
into a typed, versioned **capability**, and that capability replays deterministically afterwards
with **no model in the loop**.

Build assignment for interface.ai (Software Engineer II). Design notes are in `REPORT.md`; the
running decision log is in `DECISIONS.md`.

## Status

| Part | State |
| --- | --- |
| Day 0: repo, tooling, CI | Done |
| Day 1: CU Core target app (legacy frameset, fault injection, tenant B) | Done |
| Day 2: `core/` artifact schema, result contract, conditions, policy, authz, redaction | Done |
| Day 3: web surface adapter (Playwright) | Next |
| Day 4: discovery agent and the real LLM run | Planned |
| Day 5: compiler, registry, approval | Planned |
| Day 6: replay engine | Planned |
| Day 7: control lease and human handoff | Planned |
| Day 8: tenant overrides, cross-tenant replay | Planned |

## Setup

Requires Python 3.11+ and [uv](https://docs.astral.sh/uv/).

```bash
uv sync --extra dev
uv run playwright install chromium     # browser for the surface adapter (Day 3+)
cp .env.example .env                   # add ANTHROPIC_API_KEY for discovery (Day 4+)
```

## Run the target app

```bash
uv run cu-core --tenant a              # Harbor Federal CU  -> http://localhost:8080
uv run cu-core --tenant b              # Riverbend Community CU -> http://localhost:8081
```

Sign on with the synthetic service account from `.env.example` (`CU_CORE_USERNAME` /
`CU_CORE_PASSWORD`).
See `target_app/README.md` for members, flows and the fault catalogue.

## Work with capabilities

```bash
uv run cua validate capabilities/lookup_savings_balance/1.0.0.yaml   # schema + hash check
uv run cua hash capabilities/lookup_savings_balance/1.0.0.yaml       # print content hash
uv run cua tool capabilities/lookup_savings_balance/1.0.0.yaml       # agent tool definition
uv run cua schema --out schemas/capability.schema.json               # regenerate JSON Schema
```

## Tests

```bash
uv run pytest            # unit + target app tests, no API key needed
uv run ruff check .
uv run mypy
```

## Layout

```
config/          policies, app profile, tenants
capabilities/    versioned capability artifacts (YAML)
schemas/         generated JSON Schema for the artifact format
src/cua/core/    pure logic: schema, results, conditions, policy, authz, redaction (no browser, no LLM)
target_app/      CU Core, the legacy target
tests/           unit tests and target app tests
evidence/        curated run logs and screenshots (from Day 4)
```
