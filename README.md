# cua-automation

An LLM works out how to complete a task in a UI that has no API. The successful run is compiled
into a typed, versioned **capability**, and that capability replays deterministically afterwards
with **no model in the loop**.

Take-home for interface.ai (Assignment A, computer-use automation). Design write-up:
`REPORT.md`. Evidence from real runs: `evidence/` (start with `evidence/README.md`). Decision
log: `DECISIONS.md`.

## What is here

| Brief | Where |
| --- | --- |
| 3.1 Goal-driven agent loop | `src/cua/discovery/` (spec, prompts, agent), any of six model providers |
| 3.2 Structured artifact | `src/cua/core/artifact.py`, `capabilities/`, `schemas/capability.schema.json`, compiler in `src/cua/compiler/` |
| 3.3 Deterministic replay | `src/cua/replay/engine.py`, result contract in `src/cua/core/results.py` |
| 3.4 Guardrails | `src/cua/core/policy.py`, `authz.py`, `confirmation.py`, `redact.py`, `config/policies/` |
| 3.5 Evidence | `src/cua/evidence/`, output in `evidence/` |
| 3.6 Human handoff | `src/cua/core/lease.py`, `src/cua/handoff/`, `src/cua/operator/` |
| 3.7 Heterogeneity and tenants | `src/cua/surface/base.py` (the seam), `src/cua/core/overrides.py`, `src/cua/onboarding/probe.py` |
| Target app | `target_app/` ("CU Core": a deliberately legacy credit union back office, two tenants) |

## Quick start (no model key needed)

Everything runs locally; the target app starts by itself. Replay, the probe and the tests never
call a model, so this works with no API key:

```bash
uv sync --extra dev
uv run playwright install chromium
cp .env.example .env
uv run cua replay lookup_savings_balance --input member_id=10077            # success
uv run cua replay lookup_savings_balance --input member_id=99999            # business outcome
uv run cua replay lookup_savings_balance --input member_id=10077 --fault error500=/member   # failure with evidence
uv run cua replay lookup_savings_balance --tenant tenant-b --input member_id=10077          # second tenant
uv run pytest
```

Only discovery (`cua discover`) needs a key; a free Groq key is enough.

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
| `prompts/turn-NN.txt` | what the model was shown each turn; re-redacted at the end of the run, so values it saw before they were extracted (a name, a balance) appear masked here |
| `screenshots/NN-turnNN.png` | the page each turn, sensitive values blacked out |

Other runs worth trying: `--input member_id=99999` (business outcome: not found),
`--input member_id=10088` (the page contains a prompt-injection note), `--fault notice`
(an interstitial the agent has to acknowledge).

### 2. Teach it the business outcomes (optional, recommended)

Run discovery on cases that cannot succeed. The model declares the outcome and quotes the page
text that shows it; that quote becomes a detector.

```bash
uv run cua discover goals/lookup_savings_balance.yaml --out evidence --input member_id=99999   # not found
uv run cua discover goals/lookup_savings_balance.yaml --out evidence --input member_id=10666   # access denied
```

### 3. Compile: trace(s) to a capability draft (no LLM)

```bash
uv run cua compile evidence/<success-run>/trace.json \
  --outcome-trace evidence/<not-found-run>/trace.json \
  --outcome-trace evidence/<denied-run>/trace.json --replace
```

Writes `capabilities/lookup_savings_balance/1.0.0.yaml` (status `draft`) and a review checklist
next to it (`1.0.0.review.md`) listing everything a person should confirm. Compare with the
hand-written reference:

```bash
uv run cua diff tests/fixtures/reference/lookup_savings_balance.hand_written.yaml \
  capabilities/lookup_savings_balance/1.0.0.yaml
```

### 4. Review and approve

```bash
uv run cua approve lookup_savings_balance 1.0.0 --by <your-name>
uv run cua list
```

Approval records who approved and when, and fixes the behavior hash: from then on the file is
immutable (an edit makes it refuse to load) and changes need a new version. Whoever recorded a
draft cannot approve it (four-eyes; the compiled draft is recorded by the discovery model).

### 5. Replay: the production path, no model in the loop

```bash
uv run cua replay lookup_savings_balance --input member_id=10077 --out evidence               # success
uv run cua replay lookup_savings_balance --input member_id=99999 --out evidence               # business outcome
uv run cua replay lookup_savings_balance --input member_id=10077 --fault error500=/member --out evidence  # failure
uv run cua replay lookup_savings_balance --input member_id=10077 --fault notice --out evidence # recovered interstitial
```

Prints one typed result (`success`, `business_outcome`, `failure`, `needs_human`) and writes
redacted evidence to `evidence/<run_id>/` (events, result, and on failure a masked screenshot plus
the page as text). Exit code: 0 success or business outcome, 1 failure, 3 needs a human.
`--version 1.0.1` pins a version (default `^1`: highest approved); `--attended` allows drafts;
`--on-behalf-of member:james.okafor` runs with subject binding instead of the audited staff mode.

| Replay situation | Result |
| --- | --- |
| Bad input (`member_id=12ab`) | `business_outcome INVALID_INPUT`, no browser opened |
| Member not found / restricted / no savings | `business_outcome` with the declared code |
| System notice dialog | `success`, with a `dismiss_system_notice` recovery recorded |
| Session expires mid-flow | `success` after re-login and restart (all steps were safe to repeat) |
| Slow page | one extended wait (recorded), then `failure TIMEOUT`, retryable |
| Error page / maintenance | `failure APP_ERROR` / `APP_UNAVAILABLE` (retryable) |
| Unknown dialog or security prompt | `needs_human`, with an intervention request in `interventions/` (with `--console`: pauses for an operator) |
| Page shows a message the artifact does not know | `failure UNEXPECTED_STATE`, quoting the page |
| Irreversible step without a confirmation token | `needs_human` before acting; nothing committed |
| Timeout after an irreversible click | `failure` with `side_effects: possible`, never retryable |

### 6. Human takeover of the live session

Add `--console` to `discover` or `replay`. The browser window opens, and at an irreversible step,
a security prompt or an unknown dialog the run **pauses** instead of stopping. One-time setup in
`.env` (any 16+ character values; `python -c "import secrets; print(secrets.token_urlsafe(24))"`):

```
CUA_CONFIRMATION_SECRET=...   # signs the approval for an irreversible step
CUA_OPERATOR_TOKEN=...        # the operator's console sign-in (config/operators.yaml)
```

Discover a write flow with a person at the commit:

```bash
uv run cua discover goals/open_sub_account.yaml --console
```

When the terminal prints `PAUSED`, open http://127.0.0.1:8090, sign in with the operator token,
**Claim**, then either **Approve** (the agent clicks Confirm with a single-use token issued to
you) or click Confirm yourself in the automation's browser window and choose **I did this step
myself**. The run continues, reads the new account number and finishes. Then:

```bash
uv run cua compile runs/<discovery run>/trace.json     # the draft from the real run is in capabilities/open_sub_account/
uv run cua approve open_sub_account 1.0.0 --by <reviewer>
uv run cua replay open_sub_account --write --console \
  --input member_id=10077 --input account_type=Savings --input nickname=Holiday \
  --input initial_deposit=50.00 --input funding_account=0077-001-2208
```

Replay runs with no model and pauses at Confirm for the operator again. Other things to try:

```bash
# an unexpected security prompt: claim, click "Remind me later" in the window, hand back
uv run cua replay lookup_savings_balance --console --fault surprise_dialog --input member_id=10077
```

| Operator does | Result |
| --- | --- |
| Approve | automation performs the step once; `confirmed` event names the operator |
| Does the step, "I did this step myself" | automation checks the step's condition and continues without repeating it |
| Hands back too early | refused with the reason; the request reopens (twice, then the run aborts) |
| Abort | `failure OPERATOR_ABORTED`, nothing further is done |
| Nobody claims within 10 minutes | `failure OPERATOR_TIMEOUT` |

What the operator did in the window is in the run's `events.jsonl` (`human_action`) and in
`interventions/<id>.json`.

Without a console, an API caller can still pre-authorize an irreversible step with a single-use
token bound to the run, step and inputs:

```bash
uv run cua token --run-id demo-1 --step click_confirm --by op:utkarsh --input member_id=10042 ...
uv run cua replay <write-capability> --write --run-id demo-1 --confirm click_confirm=<token> --input ...
```

### 7. Reuse on a second tenant

Tenant B (Riverbend, CU Core 4.7.0, port 8081) relabels two things and adds a required privacy
checkbox. The capability recorded on tenant A fails there, and says why:

```bash
uv run cua replay lookup_savings_balance --version 1.0.1 --tenant tenant-b --input member_id=10077
# failure UNEXPECTED_STATE: "You must acknowledge the member privacy notice before searching."
```

The locator probe checks the capability against the tenant (no model) and proposes overrides:

```bash
uv run cua probe lookup_savings_balance --version 1.0.1 --tenant tenant-b --input member_id=10042
uv run cua probe lookup_savings_balance --version 1.0.1 --tenant tenant-b --input member_id=10042 --draft 1.1.0
```

The second form writes the proposals into a new draft (`1.1.0` is in the repo). Review the
`overrides:` section, approve it, and the same artifact runs on both tenants:

```bash
uv run cua approve lookup_savings_balance 1.1.0 --by <reviewer>
uv run cua replay lookup_savings_balance --version 1.1.0 --tenant tenant-b --input member_id=10077
uv run cua replay lookup_savings_balance --version 1.1.0 --tenant tenant-a --input member_id=10077
```

The probe only repairs what it can prove: a relabel from the app profile's synonym list that
resolves to exactly one element, and a single required checkbox no step touches. Anything else
is reported as blocked. Only read-only capabilities are probed.

### 8. What an agent sees

```bash
uv run cua tools --tenant tenant-a            # approved, compatible, read-only capabilities
uv run cua tools --tenant tenant-a --write    # also write capabilities
```

One tool per capability (name, description with outputs and business outcomes, input JSON
Schema), plus metadata: version, content hash, risk, whether a human confirmation is needed.
Drafts, deprecated versions, incompatible product versions and the `login` plumbing are never
offered.

### 9. How stable is it?

```bash
uv run cua stability lookup_savings_balance --runs 20 --input member_id=10077
uv run cua stability lookup_savings_balance --runs 20 --tenant tenant-b --input member_id=10077 --out evidence
```

Replays N times with no model and writes a report: result counts, success rate, how many runs
needed a fallback locator or a recovery, whether every run returned the same outputs (compared
by hash, never stored), and min / median / max duration. Exit code 1 if it is not stable. Only
read-only capabilities are measured.

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
src/cua/runtime/ step runner shared by login and discovery
src/cua/compiler/ trace -> capability draft (deterministic, no LLM) and readable YAML output
src/cua/registry/ versioned capability store: drafts, four-eyes approval, immutability; agent tool catalog
src/cua/onboarding/ locator probe: check a capability against a tenant, propose overrides
src/cua/replay/  the replay engine: deterministic execution and the typed result contract
src/cua/handoff/ intervention requests and the live-session handoff (lease lives in core/lease.py)
src/cua/operator/ operator console (FastAPI + one page)
goals/           discovery specs: goal + typed contract, written by a person
target_app/      CU Core, the legacy target
tests/           unit tests, target app tests, real-browser tests (tests/surface);
                 fixtures/ holds the real Groq trace and the hand-written reference capability
evidence/        curated runs: discovery and replay logs, screenshots, artifacts
```
