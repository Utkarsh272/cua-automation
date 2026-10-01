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
| 2026-09-30 | Web surface = Playwright for role+name, injected `dom.js` for everything else | The accessibility tree cannot see labels that live in a neighbouring `<td>` |
| 2026-09-30 | Resolution returns which strategy won; index > 0 = `degraded` | Drift becomes visible on every run, before it breaks anything |
| 2026-09-30 | Ambiguity is its own failure (`TARGET_AMBIGUOUS`), separate from not found | "Two Search buttons" and "no Search button" need different fixes |
| 2026-09-30 | `near_text` picks the candidate with the closest ancestor containing the text | Deterministic tie-break that reads like a person would describe it |
| 2026-09-30 | `suggest_target` only keeps strategies verified to hit the same element now | The compiler never writes a locator that was not proven on the recorded page |
| 2026-09-30 | Browser-level navigation guard via request routing, including frames | Defence in depth: an injected link to `/admin/transfer` is aborted before it leaves the browser |
| 2026-09-30 | Native `confirm()` dialogs are dismissed and recorded, never accepted | Cancel is the safe default; the record lets detectors escalate |
| 2026-09-30 | `content_frame` in the app profile defines "the page" for route/title | In a frameset the top document's title never changes |
| 2026-09-30 | Screenshots mask known values **and** the log redactor's patterns | Review found screenshots showing emails and account numbers that logs had masked |
| 2026-09-30 | Layout-derived labels apply to fields, not buttons | Review found the quick-find Search button inheriting the text box's label |
| 2026-09-30 | Currency parser rejects spaces inside digits ("1 204") | A parser that guesses returns a wrong number instead of `OUTPUT_INVALID` |
| 2026-09-30 | Tenant B: relabel absorbed by the `name` fallback (degraded); the checkbox needs the override | Found by the first real-browser run; kept as a test and as REPORT material |
| 2026-09-30 | Known limit: amounts outside the capability contract are not masked in screenshots | No pattern separates "a sensitive amount" from "any dollar figure"; synthetic data in discovery |
| 2026-10-01 | Provider-agnostic discovery: one OpenAI-compatible client + a native Claude client | Free tiers (Groq, Gemini, OpenRouter, Cerebras) and local Ollama all speak that API; the brief only requires a real run |
| 2026-10-01 | Default provider Groq (`openai/gpt-oss-120b`, low reasoning effort) | Free, fast, OpenAI-compatible tool calling; client waits out 429s using reset headers |
| 2026-10-01 | Per-provider model env vars (`GROQ_MODEL`), not one `DISCOVERY_MODEL` | A Claude model name in `.env` must never be sent to Groq |
| 2026-10-01 | A person writes the discovery spec (goal + typed contract + risk ceiling); the model finds steps | The capability interface is never invented by a model |
| 2026-10-01 | Prompt rebuilt each turn from the action log, not a growing transcript | Small calls fit free-tier token limits; each call is reproducible from evidence |
| 2026-10-01 | Whole prompt redacted at one choke point before it leaves the process | First test run found the member ID leaking through the action log, not the observation |
| 2026-10-01 | Model sees placeholders only; values rendered in-process | It cannot leak or mistype what it never sees |
| 2026-10-01 | Discovery success requires re-reading each output through its proposed locator | Guarantees the compiled capability can replay what discovery found |
| 2026-10-01 | `declare_outcome` must quote text actually on the page | Stops a model from inventing business outcomes; the quote becomes the detector |
| 2026-10-01 | Irreversible actions always escalate during discovery | No confirmation token is ever issued to the agent |
| 2026-10-01 | Shared StepRunner for login now and replay on Day 6 | One implementation of "run a step and wait for its condition" |
| 2026-10-01 | Redaction finalized at end of run (re-redact files, mask held screenshots) | Output values are only known after extraction, but earlier turns already showed them |
| 2026-10-01 | Evidence file names carry no page data | A screenshot named after the route leaked a member ID |
| 2026-10-01 | Credential key matching uses whole names (`access_token`), not substrings | `input_tokens` was being masked, hiding proof of a real model run |
| 2026-10-01 | Same-cell labels exclude the cell's own controls | A dropdown's options were being read as its label |
| 2026-10-01 | Controls carry their form/table title (`in='Find Member'`) in observations | Lets a model tell two "Search" buttons apart without guessing from order |
| 2026-10-01 | Redacted dropdown options are mapped back only when exactly one option matches | The model sees `Checking - [REDACTED:account_number]` but must still be able to choose it |
| 2026-10-01 | Known limit: the model sees non-input values on screen (names, balances) | Needed to navigate; synthetic data here; production needs a zero-retention model agreement |
