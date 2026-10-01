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
| Day 3: web surface adapter (Playwright): locators, observation, guard, redacted evidence | Done |
| Day 4: discovery agent (any of 6 model providers), verified trace, redacted evidence | Done; real run pending |
| Day 5: compiler, registry, approval | Next |
| Day 6: replay engine | Planned |
| Day 7: control lease and human handoff | Planned |
| Day 8: tenant overrides, cross-tenant replay | Planned |

## Setup

Requires Python 3.11+ and [uv](https://docs.astral.sh/uv/).

```bash
uv sync --extra dev
uv run playwright install chromium     # browser for the surface adapter
cp .env.example .env                   # add one model key, e.g. GROQ_API_KEY (free tier)
```

### Model providers (discovery only)

Replay never calls a model. Discovery works with any of these; only the chosen one needs a key.

| `--provider` | Key in `.env` | Default model | Notes |
| --- | --- | --- | --- |
| `groq` (default) | `GROQ_API_KEY` | `openai/gpt-oss-120b` | Free tier; token-per-minute limits cause short automatic pauses |
| `gemini` | `GEMINI_API_KEY` | `gemini-2.5-flash` | Free tier; supports `--vision`; free-tier prompts may be used by Google (synthetic data only) |
| `openrouter` | `OPENROUTER_API_KEY` | `openrouter/auto` | Use a `:free` model via `--model` |
| `cerebras` | `CEREBRAS_API_KEY` | `gpt-oss-120b` | Free tier |
| `ollama` | none | `qwen3:8b` | Local on your machine; must support tool calling |
| `anthropic` | `ANTHROPIC_API_KEY` | `claude-sonnet-5-5` | Paid; supports `--vision` |

Override a model with `--model` or `<PROVIDER>_MODEL` in `.env`. Check a key before a run:

```bash
uv run cua llm-check --provider groq
```

## Run the target app

```bash
uv run cu-core --tenant a              # Harbor Federal CU  -> http://localhost:8080
uv run cu-core --tenant b              # Riverbend Community CU -> http://localhost:8081
```

Sign on with the synthetic service account from `.env.example` (`CU_CORE_USERNAME` /
`CU_CORE_PASSWORD`).
See `target_app/README.md` for members, flows and the fault catalogue.

## Demo path

### 1. Discovery: an LLM works out the flow once

```bash
uv run cua discover goals/lookup_savings_balance.yaml --provider groq --out evidence
```

This starts CU Core (tenant A) in the background, signs on with the approved `login`
capability, and lets the model drive the browser toward the goal in
`goals/lookup_savings_balance.yaml`. Add `--headed` to watch it. The run stops on success, a
declared business outcome, escalation, a dead end, or the step/time budget.

The model only ever sees input *placeholders* (`{{inputs.member_id}}`) and redacted page text,
and every action passes the policy guard. Evidence lands in `evidence/<run_id>/`:

| File | What it shows |
| --- | --- |
| `result.json` | status, model, token usage, verified outputs |
| `trace.json` | every action with rationale, the element, locators proven on the page, policy decision |
| `events.jsonl` | timeline, including each model call's request id, latency and tokens |
| `prompts/turn-NN.txt` | exactly what the model was shown each turn (redacted) |
| `screenshots/NN-turnNN.png` | the page each turn, sensitive values blacked out |

Other runs worth trying: `--input member_id=99999` (business outcome: not found),
`--input member_id=10088` (the page contains a prompt-injection note), `--fault notice`
(an interstitial the agent has to acknowledge).

### 2. Compile and replay (Days 5-6)

Next: `cua compile` turns the discovery trace into a capability draft for review, and
`cua replay` runs it with no model in the loop.

## Work with capabilities

```bash
uv run cua validate capabilities/lookup_savings_balance/1.0.0.yaml   # schema + hash check
uv run cua hash capabilities/lookup_savings_balance/1.0.0.yaml       # print content hash
uv run cua tool capabilities/lookup_savings_balance/1.0.0.yaml       # agent tool definition
uv run cua schema --out schemas/capability.schema.json               # regenerate JSON Schema
```

## Tests

```bash
uv run pytest            # unit, target-app and real-browser tests; no API key needed
uv run pytest tests/surface   # just the Playwright tests against CU Core (tenants a and b)
uv run ruff check .
uv run mypy
```

## Layout

```
config/          policies, app profile, tenants
capabilities/    versioned capability artifacts (YAML)
schemas/         generated JSON Schema for the artifact format
src/cua/core/    pure logic: schema, results, conditions, policy, authz, redaction, parsing (no browser, no LLM)
src/cua/surface/ the Surface protocol and the Playwright web adapter (dom.js runs in each frame)
src/cua/evidence/ run evidence store; every write goes through the redactor
src/cua/llm/     model clients: OpenAI-compatible (Groq, Gemini, OpenRouter, Cerebras, Ollama), Anthropic
src/cua/discovery/ discovery spec, prompts, agent loop, trace
src/cua/runtime/ step runner shared by login, discovery and (Day 6) replay
goals/           discovery specs: goal + typed contract, written by a person
target_app/      CU Core, the legacy target
tests/           unit tests, target app tests, real-browser tests (tests/surface)
evidence/        curated runs: discovery and replay logs, screenshots, artifacts
```
