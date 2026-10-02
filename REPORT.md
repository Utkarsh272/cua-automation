# REPORT

Everything below is implemented and tested unless it says *design only*. Evidence is indexed
in `evidence/README.md`; the decision log is `DECISIONS.md`.

## 1. Architecture

One Python process, seven parts, one hard seam.

```
goal spec ─▶ discovery agent ─▶ trace ─▶ compiler ─▶ registry (draft ▶ approved)
              (LLM, once)               (no LLM)          │
                    │                                     ▼
                    └────── Surface protocol ◀──── replay engine (no LLM) ─▶ typed result
                             (web adapter)               │
            policy guard · redactor · evidence      handoff + operator console
```

- **Target: a self-built legacy app ("CU Core").** Frameset, table layouts, labels in the
  neighbouring cell, no test ids, ids regenerated per load, two "Search" buttons, faults on
  demand, a second tenant. A public demo site gives none of this reproducibly, and all data is
  synthetic.
- **`core/` never imports a browser or a model SDK.** Schema, results, conditions, policy,
  authz, redaction, lease and overrides are pure and tested in milliseconds. Replay imports no
  model client at all; every result records `llm_calls: 0`.
- **A person writes the contract, the model finds the steps.** The discovery spec fixes the
  goal, typed inputs/outputs and expected outcomes. The model gets one tool call per turn, sees
  placeholders (`{{inputs.member_id}}`) instead of values, and a run only succeeds if every
  output re-reads through the locator proposed for it.
- **The compiler is deterministic code**: same traces, same artifact, same hash, using only
  locators the surface proved unique on the recorded page.
- **Trade-offs.** Semantic locators over screenshots and coordinates: slower to build, but
  replay does not depend on pixels. A prompt rebuilt each turn instead of a transcript: small
  calls that fit a free tier (the real runs used Groq), at the cost of the model's long memory.
  One process and files instead of services: easy to run and inspect, not horizontally scalable.

## 2. Artifact schema

A capability is **a contract first and a step list second** (`src/cua/core/artifact.py`,
example `capabilities/lookup_savings_balance/1.1.0.yaml`).

- **Contract:** id, semver, inputs and outputs as JSON Schema (so the same file is the agent's
  tool definition, `cua tools`), declared business outcomes, risk class, and `subject` (which
  input identifies whose data is touched). Each field carries `x-sensitivity`, which drives
  redaction.
- **Steps:** action, value as a placeholder, and a target with *ranked* locator strategies
  (role+name, label, table cell, stable attribute, text, css, coordinates last) plus a
  fingerprint used only to report drift. A strategy wins only on exactly one visible match; a
  fallback win marks the result `degraded`. Waits are conditions, never sleeps.
- **Detectors:** page conditions with a declared response: business `outcome`, `recover`
  (bounded), `escalate`, or `fail`. Undeclared outcomes cannot be returned.
- **Success:** an explicit checkpoint (page plus required outputs), verified at the end.
- **Overrides:** per-tenant `set` / `insert_step`, addressed by step id.
- **Lifecycle:** `draft` → `approved` (by someone other than the recorder; content hash fixed,
  file immutable) → `deprecated`. Patch = locator or detector repair, minor = additive, major =
  contract change.

Why this shape: a reviewer reads a YAML diff, a calling agent reads the contract, and the engine
needs nothing from the model transcript. Sensitive-looking literals are rejected by the schema.

## 3. Determinism & error handling

Replay only chooses between branches written in the artifact. Before a browser opens it checks
inputs, authorization and product-version compatibility. Then, before every step and on every
poll, detectors are evaluated with fixed precedence: **fail > escalate > outcome > recover > the
step's own condition**. A "No member found" banner therefore beats a timeout, and a dialog no
detector explains goes to a person instead of being clicked through.

Result contract (`core/results.py`): `Success`, `BusinessOutcome`, `Failure`, `NeedsHuman`.

| Condition | Class | Response |
| --- | --- | --- |
| Bad input; not found; access denied; no savings account | business | declared outcome with the page's message |
| Known notice dialog | recoverable | dismiss (max 2), continue, recorded |
| Session expired | recoverable | re-login and restart, only if every step so far is repeatable |
| Slow page | recoverable once | one extended wait, then `TIMEOUT` (retryable) |
| Error page, maintenance | hard | `APP_ERROR`, `APP_UNAVAILABLE` |
| Unknown page message; locator finds none or several | hard | `UNEXPECTED_STATE` quoting it; `TARGET_NOT_FOUND` / `TARGET_AMBIGUOUS` |
| Unknown dialog, security prompt, irreversible step | human | pause for an operator, or `NeedsHuman` |

Every failure names the step, expected vs observed, a masked screenshot and the page as text,
plus `side_effects` (`none` / `possible` / `committed`). Anything but `none` is never retryable:
the commit-timeout test shows the account *was* created, which is why a blind retry would open
two. Drift is reported on success (`degraded` plus a `drift` event), before it breaks anything.
`cua stability` replays N times: 20/20 identical successes on each tenant, median 1.4 s.

Real runs found three bugs the tests had missed (a detector quoting the service account's
name, digits masked inside a timestamp, overrides rejected on approved files); each is now a test.

## 4. Heterogeneity & multi-tenant

**Surface seam.** The engine, compiler and agent talk to a `Surface` protocol: `observe`,
`resolve(target)`, `act`, `read`, `page_state`, `screenshot`. The artifact stores *what a person
would point at* (role, label, row and column), not selectors. The legacy web adapter is built:
frames are part of every target, and labels and table cells are resolved in-page because the
accessibility tree cannot see a label in a neighbouring `<td>`. *Design only:* a desktop adapter
would implement the same protocol over UI Automation / AX (role+name and label map directly;
frames become windows), with screenshot+coordinates as the last-ranked strategy that the schema
already reserves. Steps, detectors and the result contract would not change.

**Tenants.** An artifact belongs to a vendor product and version range, not to a tenant. Tenant
differences are overrides inside the same reviewed, hashed file, applied at replay to a validated
copy. `cua probe` checks a capability against a tenant with no model: on tenant B it proposed
two relabels (from the app profile's synonym list, accepted only when they resolve to exactly
one element), carried one into a detector, and inserted the required privacy checkbox. It
re-runs until a clean pass and writes a draft for a person to approve. `1.1.0` then runs on both
tenants with first-choice locators (evidence: fails on B without overrides, succeeds with).
Without an override a relabel degrades gracefully (attribute fallback, flagged) and an unknown
requirement fails with the page's own message. At scale: one artifact per product version, probe on
onboarding and after upgrades, per-tenant `degraded` rates as the drift alarm.

**Scale (design only).** What is built is one process on one machine. What makes it scalable
is that a replay is stateless (artifact + inputs + tenant config), takes about 1.5 s and calls
no model, so runs can be spread over any number of workers. What breaks first, and the fix:
a browser launched and a fresh login per run → a worker pool with warm contexts and a
per-tenant session pool that respects the app's concurrent-session limit; registry, lease,
spent tokens and interventions in files or memory → a shared store with the same hash and
approval rules; no idempotency key → one per invocation, so a caller that sees
`side_effects: possible` can ask "did it commit?" instead of retrying.

## 5. Escalation & handoff

**Detecting stuck.** Replay: an irreversible action without a confirmation, an `escalate`
detector, or an unexplained dialog. Discovery: the same policy check, the model's `escalate`
tool, and hard stops (three actions with no change, a repeated action, budgets).

**Same live session.** The intervention request carries capability, step, reason, a redacted
screenshot and what must be true before hand-back. The operator claims it in a minimal console
and uses the automation's own browser window. A **control lease** with one holder and an epoch
(`automation → pending_human → human → resuming → automation | aborted`) is checked by the
surface on every action, so automation cannot click while a person drives and a stale call is
refused after hand-back.

**Decisions.** *Approve* (automation performs the one step with a single-use token issued to
the operator), *I did it myself*, *fixed it*, or *abort*; the kind of pause limits the choices.

**Hand-back is verified.** The engine re-checks the step's condition (or that the dialog is
gone) before resuming; a failed check reopens the request, twice, then aborts. Lost heartbeats
release a claim; unclaimed requests time out. The person's clicks and edits are recorded with
their id, never a sensitive value. Without a console the same places return `NeedsHuman`.

In `evidence/`: discovery and replay of `open_sub_account` both pause at Confirm and continue
after operator approval, and a replay pauses on a security prompt the operator clears by hand.

**Mocked on purpose:** the console is one page and the operator must be at the machine showing
the browser. Remote streaming, a queue and assignment are design only.

## 6. Safety

- **Deny by default.** Per-app policy of allowed origins, routes and action types, enforced
  twice: by the policy guard before each action and in the browser on every document request
  (frames included).
- **Risk.** read / reversible write / irreversible. Risk is the maximum of what the artifact
  declares and rules on the real control name and route, so a mislabelled artifact cannot make
  "Confirm" safe. Writes need the write scope. Irreversible actions need an HMAC token bound to
  run, step and inputs hash, single-use, five-minute expiry.
- **Authorization.** Tenant, scope, approval state, and subject binding: a caller acting for a
  member may only touch that member's records; staff mode is separate and audited.
- **Data.** One redactor at the evidence sink, driven by the contract's sensitivity tags, known
  secrets and patterns; identifiers become stable hash tags; screenshots are masked with the same
  rules. Credentials exist only as `{{secrets.*}}` references; tests scan evidence output and
  the repo for leaked values.
- **Prompt injection.** Page text is data. A member note telling the agent to move money is
  refused by policy and aborted at the network layer (tested).
- **Limits.** During discovery the model sees on-screen values that are not inputs (a name, a
  balance); those stay visible in that run's saved prompts unless they are declared outputs.
  Acceptable with synthetic data; production needs a zero-retention model agreement or masking
  by field type. Amounts outside the contract are not masked in screenshots. Token nonces and
  the lease are in memory.

## 7. Cuts

Deliberately left out:

- **Desktop surface**: designed at the seam, not built.
- **Remote operator experience**: no session streaming, queueing or assignment.
- **Free-form human actions are not compiled into steps**; they are recorded and flagged for
  review.
- **Write-flow outcomes**: `open_sub_account` declares outcomes that have no detectors yet (the
  compiler's review notes say so); the lookup capability has all of them.
- **Scale plumbing**: no queue, worker pool, shared store or session pool (section 4).

Next, in order: (1) bounded model-assisted repair of a single failed step, policy-checked and
saved as a patch draft, never applied silently; (2) replay-reliability scoring per tenant from
`degraded` and failure rates, gating unattended use; (3) a UI Automation adapter to prove the
seam; (4) the shared store from section 4.
