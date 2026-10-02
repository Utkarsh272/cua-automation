# Evidence

Everything here was produced by running the commands in the top-level `README.md` against the
local target app (CU Core). The discovery runs are genuine model runs (Groq,
`openai/gpt-oss-120b`); each model call's request id, latency and token counts are in that run's
`events.jsonl`. Replay runs record `llm_calls: 0`. All files went through the redactor: member
IDs appear as stable tags (`[identifier:32f69e84]`), other sensitive values as
`[REDACTED:<kind>]`, and screenshots are masked.

## Start here

| What the brief asks for | Look at |
| --- | --- |
| A saved artifact | `artifacts/lookup_savings_balance-1.1.0.yaml` (approved; copy of `capabilities/…/1.1.0.yaml`) |
| Logs from a real discovery run | `discovery_2026-10-01T16-28-20Z_eb11/` |
| Logs from a replay run | `replay_2026-10-02T00-38-22Z_4a31/` |
| A replay that hits an error | `replay_2026-10-02T00-39-05Z_2eca/` (app error, with screenshot and page text) |

## Discovery runs (LLM in the loop)

| Run | Goal | Result |
| --- | --- | --- |
| `discovery_2026-10-01T16-28-20Z_eb11` | `lookup_savings_balance`, member exists | succeeded in 5 turns; both outputs re-read through their proposed locators |
| `discovery_2026-10-01T17-23-30Z_f4d4` | same, unknown member | business outcome `MEMBER_NOT_FOUND`, quoting the page |
| `discovery_2026-10-01T17-23-48Z_cd2e` | same, restricted member | business outcome `ACCESS_DENIED`, quoting the page |
| `discovery_2026-10-02T13-32-00Z_39dc` | `open_sub_account` (a write flow) | succeeded in 12 turns; **paused at the irreversible Confirm**, an operator claimed and approved it in the console, the run continued |

The first three were compiled into `lookup_savings_balance` 1.0.0. The fourth was compiled into
`artifacts/open_sub_account-1.0.0.yaml` (draft; its review notes are in
`capabilities/open_sub_account/1.0.0.review.md`). The operator's side of the pause is
`interventions/int_20261002T133342_e93160.json`; in the run's `events.jsonl` see
`intervention_opened` → `intervention_claimed` → `operator_decision` → `intervention_resolved`.

Per run: `result.json` (status, model, tokens, verified outputs), `trace.json` (each action with
rationale, element, proven locators, policy decision), `events.jsonl` (timeline),
`prompts/turn-NN.txt` (exactly what the model was shown), `screenshots/`.

## Replay runs (no model)

| Run | Capability, tenant | Result |
| --- | --- | --- |
| `replay_2026-10-02T00-38-22Z_4a31` | 1.0.1, tenant A | `success`, both outputs, 1.6 s |
| `replay_2026-10-02T00-38-43Z_edd5` | 1.0.1, tenant A, unknown member | `business_outcome MEMBER_NOT_FOUND` at `click_search` |
| `replay_2026-10-02T00-39-05Z_2eca` | 1.0.1, tenant A, injected server error | `failure APP_ERROR` at `click_search`, `side_effects: none`, screenshot + `page_at_stop.txt` |
| `replay_2026-10-02T14-29-54Z_416d` | 1.0.1, **tenant B**, no overrides | `failure UNEXPECTED_STATE`, quoting "You must acknowledge the member privacy notice…"; the relabelled field is logged as `drift` |
| `replay_2026-10-02T14-30-21Z_8042` | 1.1.0 (with tenant B overrides), tenant A | `success`, not degraded: overrides do not touch tenant A |
| `replay_2026-10-02T15-17-29Z_75ef` | 1.1.0, **tenant B** | `success`, not degraded; `overrides_applied` in the timeline |
| `replay_2026-10-02T16-24-12Z_dc94` | `open_sub_account` 1.0.0 (write flow), tenant A | **paused at the irreversible `click_confirm`**, operator claimed and approved in the console, `confirmed_by: ops.alex`, then `success` |
| `replay_2026-10-02T16-25-43Z_919d` | 1.1.0, tenant A, injected security prompt | **paused as "stuck"**, operator dismissed the prompt in the live browser and handed back, hand-back check passed, then `success` |

Rows four to six are the cross-tenant story: the capability recorded on tenant A fails on tenant B
with the page's own message, `cua probe` proposed the overrides, and the same approved file then
runs on both.

## Human takeover

Three pauses, each with a record in `interventions/` (reason, step, redacted screenshot path,
who claimed, what they decided, notes):

| Intervention | Run | What happened |
| --- | --- | --- |
| `int_20261002T133342_e93160` | discovery `…39dc` | agent proposed the irreversible Confirm; operator approved; agent clicked with a single-use token |
| `int_20261002T162415_d62342` | replay `…dc94` | same step during replay, no model: approve → commit → output read |
| `int_20261002T162545_965ee5` | replay `…919d` | unexpected security prompt: operator fixed it in the same session; automation resumed only after the prompt was verified gone |

In each run's `events.jsonl`: `intervention_opened` → `intervention_claimed` →
`operator_decision` → `intervention_resolved`. The screenshot taken at the pause is in the run's
`screenshots/`.

## Stability (20 replays each, no model)

| Report | Tenant | Result |
| --- | --- | --- |
| `stability_2026-10-02T16-22-55Z_df46.json` | A | 20/20 success, identical outputs, no fallback locators, no recoveries; 1.37 / 1.40 / 1.65 s (min / median / max) |
| `stability_2026-10-02T16-23-39Z_01a4.json` | B (with overrides) | 20/20 success, identical outputs, no fallback locators, no recoveries; 1.43 / 1.48 / 1.72 s |

The 40 individual runs are the `replay_2026-10-02T16-2*` folders listed in each report's
`run_ids`.

## Known limits visible in this evidence

- `int_…965ee5` has an empty `human_actions` list although the operator dismissed the prompt.
  At the time, clicks were recorded only after the request had been claimed in the console, and
  a click made in the browser window before claiming was dropped. Fixed since: such actions are
  now recorded as `human:unclaimed` (covered by a test). The record is left as produced.

- In `discovery_…39dc/prompts/`, a member name and balances appear unmasked. During discovery
  the model sees on-screen values that are not inputs; they are masked afterwards only when they
  are declared outputs (as in the lookup runs). All data is synthetic. See REPORT.md, section 6.
- In the same run, one `t_ms` value in `events.jsonl` reads `10[REDACTED:financial]9`: the
  redactor matched the deposit amount inside a timestamp. Fixed since (a known value no longer
  matches inside a longer number); the log is left as recorded.
- Outputs in a replay's saved `result.json` are masked on purpose; the caller receives the real
  values.
