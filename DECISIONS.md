# Decision log

One line per decision, with the reason. This becomes REPORT.md and interview notes.

| Date | Decision | Reason |
| --- | --- | --- |
| 2026-09-30 | Build own target app instead of a public demo site | Legacy markup on purpose, faults on demand, synthetic data, no ToS risk |
| 2026-09-30 | Python 3.11+, Pydantic v2, FastAPI, Playwright, uv | Strongest stack; Pydantic gives typed schema and JSON Schema export |
| 2026-09-30 | `core/` never imports Playwright or an LLM SDK | Keeps load-bearing logic testable in milliseconds; proves the surface seam |
| 2026-09-30 | Target app is a frameset with regenerated input IDs and adjacent-cell labels | Forces frame-aware, non-ID locators, like real core banking UIs |
| 2026-09-30 | Faults armed via `X-Fault` header or `cu_fault` cookie, plus natural member IDs | Deterministic, reproducible evidence for each replay path |
| 2026-09-30 | Tenant B = same code, `CU_TENANT=b` | Cross-tenant reuse shown with a real second instance, cheaply |
| 2026-09-30 | Artifact is YAML validated by Pydantic, content-hashed | Human-reviewable diffs plus machine validation and tamper detection |
| 2026-09-30 | Inputs and outputs as JSON Schema with `x-sensitivity` | Same format as LLM tool definitions; drives redaction from the contract |
| 2026-09-30 | Declared outcomes only; undeclared outcome = failure | Callers know every non-error result in advance |
| 2026-09-30 | Result is a discriminated union with `side_effects` on failures | After a failed write the caller must know if money may have moved |
| 2026-09-30 | Heuristic risk rules can only raise risk | A mislabeled artifact cannot make a "Confirm" button safe |
| 2026-09-30 | Confirmation tokens are HMAC-signed, single-use, bound to run, step and inputs hash | A token for one member cannot be replayed for another |
| 2026-09-30 | One redaction function at the sink; a test scans outputs for leaked secrets | Proves "never persist secrets" rather than claiming it |
| 2026-09-30 | Label strategy ranks above role+name for CU Core text boxes | Observed in Chromium: the inputs have **no accessible name** (label is in the neighbouring `<td>`), so role+name cannot match |
| 2026-09-30 | `near_text` on role strategies | Two buttons are named "Search"; uniqueness is resolved by the closest container text ("Find Member"), never by picking the first |
| 2026-09-30 | Table header rows are `<td>`, so `table_cell` resolves columns from the DOM | The accessibility tree reports them as plain cells, not column headers |
| 2026-09-30 | Content hash excludes status/approved_by/approved_at | Approving must not change identity; any behavioral edit must |
| 2026-09-30 | Irreversible steps must say `requires_confirmation: true` explicitly, and are never `safe_to_repeat` | Reviewers see the gate in the diff; re-login recovery cannot replay a commit |
| 2026-09-30 | Artifacts reject sensitive-looking literals (SSN, card, ...) | Concrete values belong in inputs or secrets, not in a reviewed file |
| 2026-09-30 | Detectors cannot depend on other detectors | Keeps precedence and evaluation order simple and predictable |
| 2026-09-30 | Tenant overrides are ops (`set`, `insert_step`) keyed by step id | Tenant B needs relabels **and** an extra step; ids keep overrides stable when steps move |
| 2026-09-30 | Missing token on an irreversible action = escalate; bad token = deny | A missing token is a normal pause; a forged or replayed one is a security event |
| 2026-09-30 | Subject binding (`subject: inputs.member_id` + `on_behalf_of`) with an audited `staff` scope | Closes the confused-deputy hole: signed in as staff is not the same as allowed for this member |
| 2026-09-30 | Identifiers are redacted to a stable hash tag, not blanked | Runs stay correlatable in logs without exposing the member ID |
| 2026-09-30 | Unknown fields default to sensitive in the redactor | Fail closed |
| 2026-09-30 | Guard test caught the synthetic password printed in README on its first run | Kept the test; docs now point to `.env.example` |
