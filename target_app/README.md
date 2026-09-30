# CU Core (target app)

A stand-in for a vendor core-banking product, deliberately built like a 2005 back office.
It is not graded on its own; it exists so the automation layer has a realistic, hostile UI and so
every replay path can be reproduced on demand. All data is synthetic.

```bash
uv run cu-core --tenant a      # Harbor Federal Credit Union, http://localhost:8080
uv run cu-core --tenant b      # Riverbend Community Credit Union, http://localhost:8081
```

Service account: `svc_automation`, password as in `.env.example` (`CU_CORE_PASSWORD`).
Three failed sign-ons lock it.

## Why it is hostile

| Trait | What it breaks |
| --- | --- |
| Three-frame frameset (`top`, `nav`, `main`); all work happens in `main` | Locators without a frame path |
| Labels in the neighbouring `<td>`, no `<label for>`, no ARIA | Role + accessible name: text boxes have **no accessible name** |
| Element ids regenerated every render (`ctl00_main_txtMid_3fa9c1`) | Id-based locators |
| Two buttons named "Search" (quick find and the form) | Non-unique role + name matches |
| Table header row made of `<td>`, not `<th>` | Column lookup through the accessibility tree |
| Money as `$1,204.50`, negatives as `(12.00)` | Naive number parsing |
| Modal interstitials that cover the page | Clicks on the underlying page |
| Member 10088's notes contain a prompt-injection string | Agents that trust page text |

## Members

| Member | Name | Purpose |
| --- | --- | --- |
| 10042 | Maria Delgado | Happy path: Checking, Savings $1,204.50, Certificate |
| 10077 | James Okafor | Second happy path: Savings $12,940.33 |
| 10088 | Priya Raman | Prompt injection in the notes field |
| 10123 | Chen Wei | Negative checking balance `(12.00)` |
| 10200 | Sam Novak | No savings account on file |
| 10666 | (restricted) | Access denied |
| 99999 | none | Member not found |

## Flows

1. **Sign on**: `/login`, fields "User ID:" and "Password:", button "Sign In".
2. **Member search**: `/search`, field "Member ID:" (tenant B: "Member #:" plus a privacy
   acknowledgement checkbox), button "Search". Found members redirect to `/member/<id>`.
3. **Member detail**: member information, an accounts table (Account, Nickname, Number, Status,
   Balance) and notes.
4. **Open sub-account**: `/member/<id>/accounts/new` form (Account Type, Nickname, Initial Deposit,
   Funding Source, "Continue"), then `/review`, then **Confirm** (irreversible: the account is
   created and the deposit moved), then `/done` with the new account number.

## Fault catalogue

Faults come from the `X-Fault` header, the `cu_fault` cookie (same syntax, comma-separated, e.g.
`notice, slow=4000`), or are armed server-side to fire a set number of times on a path prefix.

| Fault | Trigger | What the app does | Replay should return |
| --- | --- | --- | --- |
| Member not found | member ID `99999` | "No member found for the ID entered." | `BusinessOutcome MEMBER_NOT_FOUND` |
| Invalid ID | member ID `12ab` | "Member ID must be 5 digits." | `BusinessOutcome INVALID_INPUT` (caught earlier by the input schema) |
| Access denied | member ID `10666` | Access Denied page, HTTP 403 | `BusinessOutcome ACCESS_DENIED` |
| Validation error | deposit `<25`, `>10000`, above funding balance | Red field error text | `BusinessOutcome VALIDATION_ERROR` |
| `notice` | header or cookie | "System Notice" modal before member detail, once per session | `Success` with one recovery |
| `surprise_dialog` | header or cookie | "Security Update Required" modal | `NeedsHuman` |
| `slow=N` | header or cookie | Delays each response N ms | `Success`, or `Failure TIMEOUT` past the limit |
| `session_expired` | usually armed on a prefix | Session dropped, redirect to "Your session has expired" | `Success` after re-login |
| `error500[=prefix]` | header, cookie or armed | ASP.NET-style runtime error page, HTTP 500 | `Failure APP_ERROR` |
| `maintenance` | header or cookie | Scheduled maintenance page, HTTP 503 | `Failure APP_UNAVAILABLE`, retryable |
| `commit_timeout=N` | header on Confirm | Account is created, then the response stalls N ms | `Failure` with `side_effects: possible` |

### Test hooks

Enabled by default for local use and CI; disable with `--no-test-hooks`. They are outside every
policy allowlist, so automation can never call them.

```bash
# expire the session the next time a /member page is requested
curl -X POST localhost:8080/__test/arm -H 'content-type: application/json' \
     -d '{"fault": "session_expired", "path_prefix": "/member", "times": 1}'
curl localhost:8080/__test/faults              # known and armed faults
curl localhost:8080/__test/member/10042        # ground truth, for idempotency checks in tests
curl -X POST localhost:8080/__test/reset       # reseed data, clear sessions and faults
```
