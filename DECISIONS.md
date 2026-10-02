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
| 2026-10-01 | Compiler is plain deterministic code, no LLM; same traces give the same hash | The artifact must be explainable and reproducible from evidence |
| 2026-10-01 | Compiler uses only locators the surface verified on the recorded page | No locator enters an artifact without having worked once |
| 2026-10-01 | A person declares expected outcome codes in the spec; the model is told them | The same outcome gets the same name across runs |
| 2026-10-01 | Business-outcome runs become detectors from the model's quoted page text | Outcomes are discovered like flows, and the quote is verifiable |
| 2026-10-01 | Quotes containing member data are refused as detectors | A detector keyed on one member's data would never fire again |
| 2026-10-01 | Clicks inside a dialog compile to `recover` detectors, not steps | Interstitials appear sometimes; a step would break the other runs |
| 2026-10-01 | Observed controls record the dialog they sit in | Needed to tell an interstitial click from a flow click |
| 2026-10-01 | Drafts carry no content hash; approval adds it | Reviewers must be able to edit drafts; approved files must not change |
| 2026-10-01 | Approval edits YAML in place (round-trip) | Reviewer comments survive approval |
| 2026-10-01 | Four-eyes: recorder cannot approve; compiled drafts are recorded by the model | A model never approves its own work |
| 2026-10-01 | Deprecated versions run only when pinned exactly | Callers on ranges move on; pinned callers keep working until migrated |
| 2026-10-01 | Hand-written reference moved to tests/fixtures; compiled draft is the real 1.0.0 | The reference still exercises every schema feature in tests |
| 2026-10-01 | Readable YAML: pruned defaults by (key, value), never "all defaults" | Dropping all defaults removed the `by:` discriminator and broke parsing |
| 2026-10-01 | Date-of-birth pattern added to the CU Core redaction profile | Review of the real run showed DOB unmasked |
| 2026-10-01 | `run_finished` logged after the last step event | The real run's timeline was out of order |
| 2026-10-01 | Replay checks inputs, authorization and version compatibility before opening a browser | Cheapest and safest failures first; no session for an invalid call |
| 2026-10-01 | Detectors are checked before each step and on every poll, with fixed precedence fail > escalate > outcome > recover > condition | Business results beat timeouts; interstitials are cleared before the step they block |
| 2026-10-01 | A dialog that no detector explains is `NeedsHuman`, never clicked through | Unknown dialogs are where unreviewed side effects hide |
| 2026-10-01 | One extended wait for slow pages, then `TIMEOUT` | Absorbs transient slowness without hiding real outages |
| 2026-10-01 | Page messages turn a timeout into `UNEXPECTED_STATE` with the quote | Tenant B's failure now names the missing checkbox instead of "timeout" |
| 2026-10-01 | Session expiry restarts from step 1 only if every step so far is repeatable | Re-running a write after re-login could duplicate it |
| 2026-10-01 | Irreversible steps without a token stop before acting (`NeedsHuman`) | Money moves only with a human or pre-authorized token |
| 2026-10-01 | `side_effects` on every failure; anything but `none` is never retryable | Commit-timeout test shows the account was created despite the timeout |
| 2026-10-01 | Evidence paths are relative to the run folder | Runs stay readable after being copied into /evidence |
| 2026-10-01 | Redactor matches known values case-insensitively | The app showed the service account as SVC_AUTOMATION and it leaked into a detector |
| 2026-10-01 | 1.0.1 is a reviewed patch of approved 1.0.0, not an edit | Approved versions are immutable; patch = detector/locator repair |
| 2026-10-01 | NO_SAVINGS_ACCOUNT detector written by hand (Savings row absent) | The only page hint is member data, so discovery cannot learn it safely |
| 2026-10-01 | Write-path tests use a hand-written open_sub_account fixture | Day 7 discovers it for real with a human at Confirm |
| 2026-10-01 | Handoff keeps the same live session; the operator uses the automation's window | Assignment asks for takeover of the session, and state (login, half-filled form) is the valuable part |
| 2026-10-01 | Control lease with an epoch, checked by the surface on every action | Makes "automation and human click at once" impossible, including a late-waking automation call |
| 2026-10-01 | Without a console the engine still returns `NeedsHuman` | Unattended callers need a typed result, not a hung run |
| 2026-10-01 | Operator choices depend on the kind of pause (approve / did it myself / fixed it / abort) | A dialog cannot be "approved"; a policy pause cannot be waved through |
| 2026-10-01 | Approve = a single-use confirmation token issued to the operator, checked by the same policy guard | One path for irreversible actions whether the token comes from an API caller or the console |
| 2026-10-01 | Hand-back is re-verified against the step's own condition; failed checks reopen, twice then abort | "I did it" is a claim until the page proves it |
| 2026-10-01 | Heartbeat timeout releases a claim; unclaimed requests time out | A closed laptop must not hold a member session open forever |
| 2026-10-01 | Human actions captured in-page (trusted click/change only, no keystrokes, no sensitive values) | Enough for audit and compile review without becoming a keylogger |
| 2026-10-01 | A person clicking during a failed run makes `side_effects` at least `possible` | We cannot prove what they changed, so the caller must not retry blindly |
| 2026-10-01 | Human-performed discovery steps compile from the agent's proposed locator, with a review warning | The agent pointed at the control before the pause; a reviewer confirms it is the one used |
| 2026-10-01 | Dropdown options that hold input data must be chosen by placeholder | Otherwise the capability would be tied to one member's account number |
| 2026-10-01 | Console and engine share one process and an in-memory store with JSON files | Simple and inspectable for the demo; production needs a shared store and session streaming |
| 2026-10-02 | Redactor never matches a known value inside a longer number | A real run masked the middle of a timestamp because it contained the deposit amount |
| 2026-10-02 | Overrides are applied at replay to a validated copy; the content hash covers the reviewed file | One approved artifact per capability, and an override cannot produce an invalid one |
| 2026-10-02 | Tenant support is a new minor version (1.1.0), not an edit of 1.0.1 | Overrides change behaviour, so they change the hash and need approval |
| 2026-10-02 | Probe repairs only what it can prove: profile synonyms resolving to one element, one untouched required checkbox | Onboarding help without a model guessing locators; everything else is reported as blocked |
| 2026-10-02 | A relabel found on a step is carried into detectors using the same name | Otherwise "no Savings row" would fire falsely on a tenant that calls it "Share Savings" |
| 2026-10-02 | Probe restarts in a fresh session after each proposal and needs one clean pass | Proves the proposals work together, not one at a time |
| 2026-10-02 | Probe refuses write capabilities | Probing a write flow would change the tenant's data |
| 2026-10-02 | Tool catalog offers only approved, version-compatible capabilities within the caller's scopes | The agent never sees drafts, steps or locators; write tools need the write scope |
| 2026-10-02 | REPORT.md uses the brief's seven headings exactly; a test checks them | The brief asks for exact headings; target choice moved under Architecture |
| 2026-10-02 | A derived (override-applied) capability keeps the reviewed file's content hash | Found by a real run: approved artifacts were rejected on tenant B because the copy had no hash |
| 2026-10-02 | Compiler drops a field set again before any click (fill or select) | The real write-flow run chose the account type twice |
| 2026-10-02 | Tests no longer depend on whether shipped capability files are drafts or approved | Approving 1.0.1 and 1.1.0 must not break the suite |
| 2026-10-02 | open_sub_account 1.0.0 ships as a draft with outcome warnings in its review notes | Honest state: flow discovered and replayable, outcome detectors not yet taught |
| 2026-10-02 | `cua stability`: N replays, stable only if same result, same outputs, no fallback locator | Turns "deterministic" into a measured claim; outputs compared by hash so the report holds no member data |
| 2026-10-02 | Stability refuses write capabilities | Repeating a write N times changes the target system |
| 2026-10-02 | Scale is a design paragraph, not infrastructure | The brief values scalable abstractions and explicitly does not reward queues or clusters |
| 2026-10-02 | A person's clicks before they claim the request are recorded as `human:unclaimed` | Found by a real run: the browser comes to the front, so people click there first; that action was being dropped |
