# REPORT

> Working draft. Each section is written on the day its decisions are made (see `DECISIONS.md`),
> then tightened to 1 to 3 pages before submission.

## 1. Target selection

**Choice:** a self-built, deliberately legacy credit union back office ("CU Core"), run as two tenants
from one codebase.

- Server-rendered HTML in a three-frame frameset, table layouts, labels in the neighbouring `<td>`,
  no `<label for>`, no ARIA, no test IDs, input IDs regenerated on every page load
  (`ctl00_main_x7f2...`), and two different "Search" buttons. It exercises the same problems real core
  banking screens have.
- Faults are injectable on demand (member not found, access denied, validation error, system notice,
  slow load, session expiry, 500 page, unknown dialog, commit timeout), so every replay classification
  path can be reproduced and evidenced.
- All data is synthetic, so no PII, terms-of-service or credential concerns.
- Tenant B changes branding, relabels "Member ID" to "Member #", renames "Savings" to "Share Savings"
  and adds an acknowledgement checkbox, to show cross-tenant reuse with a small override.

## 2. Architecture

_Draft; completed on Day 7._ One process, seven components, one hard seam (the `Surface`
protocol, `src/cua/surface/base.py`). The LLM is used only by the discovery agent; production
traffic only touches the replay engine. Everything under `src/cua/core/` is free of browser and
LLM imports.

**Web surface (Day 3).** Playwright/Chromium plus a small in-page library (`dom.js`) that runs in
every frame.

- *Perception.* An observation lists visible controls and readable data cells with refs (`e12`),
  the label a person would read (including the legacy "label in the neighbouring cell"), column
  headers and row context, dialogs, and the page text. Values of password fields are never shown.
- *Locating.* Role + name via Playwright's accessibility engine; label, table-cell, text and
  attribute strategies in `dom.js`. A strategy wins only on exactly one visible match; the index of
  the winner is reported so a fallback win is flagged `degraded`.
- *From agent action to durable locator.* `suggest_target(ref)` proposes strategies for an element
  the agent used and keeps only those that resolve uniquely to that same element, most robust
  first. Generated ids are never proposed.
- *Guard in the browser.* Every document request, including frame navigations, is checked against
  the policy allowlist before it leaves the browser and aborted if not allowed. This sits under
  the policy guard as defence in depth.
- *Evidence.* Screenshots black out known sensitive values and anything matching the same patterns
  the log redactor uses, so screenshots and logs agree.

Observed on the real app: CU Core's text boxes have no accessible name, so role + name can never
match them; the label strategy is primary there. On tenant B the relabeled field is still found
through the stable `name` attribute (same vendor product), flagged `degraded`; the extra
compliance checkbox is what actually requires the tenant override.

## 3. Artifact schema

_Drafted on Day 2; see `src/cua/core/artifact.py` and `capabilities/lookup_savings_balance/1.0.0.yaml`._

- A capability is a contract first (id, version, inputs, outputs, declared outcomes, risk) and a step
  list second (targets, waits, postconditions, detectors, success checkpoint).
- Inputs and outputs are JSON Schema, so the same contract is directly usable as an agent tool
  definition. Every field carries `x-sensitivity`, which drives redaction.
- Targets carry an ordered list of locator strategies (role and name, label, table cell, stable
  attribute, CSS, coordinates last) plus a fingerprint used only for drift detection.
- Business outcomes are declared up front; an outcome the artifact does not declare is a failure.
- Artifacts belong to a vendor product and version range, not to a tenant. Tenants contribute small
  overrides addressed by step id.
- Versions are immutable and content-hashed. Semver: patch = locator repair, minor = new optional
  output, major = changed inputs or outcomes.

## 4. Determinism and error handling

_To be completed (Day 6)._ Result contract is already defined in `src/cua/core/results.py`:
`Success | BusinessOutcome | Failure | NeedsHuman`, discriminated by `kind`. Failures carry
`at_step`, `expected`, `observed`, `retryable`, `side_effects` and evidence paths. Detector
precedence: policy, then business outcomes, then recoveries, then postcondition.

## 5. Human-in-the-loop

_To be completed (Day 7)._ Epoch-fenced control lease: `automation -> pending_human -> human ->
resuming -> automation`, with `aborted` on SLA timeout or operator abort. Automation re-verifies the
step precondition before continuing.

## 6. Security and guardrails

_Drafted on Day 2; see `src/cua/core/policy.py` and `src/cua/core/redact.py`._

- Deny-by-default policy per app: allowed origins, routes, action types.
- Risk is the maximum of the declared risk and label heuristics; heuristics can only raise it.
- Irreversible steps need a signed, single-use, expiring confirmation token bound to
  `(run_id, step_id, inputs hash)`; otherwise the run escalates to a human.
- Redaction is driven by the contract's `x-sensitivity` tags first, with pattern matching (SSN,
  Luhn-checked card numbers, emails, phones) as a safety net, applied at a single logging sink.
- Credentials exist only as `{{secrets.*}}` placeholders in artifacts and prompts.

## 7. What I would do next

_To be completed._
