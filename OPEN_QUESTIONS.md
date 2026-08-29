# OPEN_QUESTIONS.md

## Session summary

**Updated 2026-08-29 (overnight session). Chat mode works end to end. No stubs
remain. 198 tests pass. Eleven commits.**

### What changed since the foundation session

| Built | State |
|---|---|
| `agent/sql_agent.py` | question → structured JSON → guard → rows → prose |
| `agent/escalation.py` | deterministic signal scoring, no LLM |
| `agent/triage_agent.py` | five-source fan-in → `TicketBrief` |
| `agent/router.py` | intent classification, tenant binding, isolation on the JSON side |
| `interfaces/cli_chat.py` | terminal transport, runs without an API key |

Nine decisions logged (D-007 … D-015). Test count went 94 → 198.

### The fix you asked for

CRITICAL saturation is fixed (Q-013). Account signals reach 95 points where ticket
signals reach 35, so **all twelve of tenant 4's tickets scored CRITICAL** and the
level had stopped ranking anything. Account-state points are now capped at
`ESCALATION_URGENT + 10`, making the rule one sentence: *a bad account is URGENT on
its own; CRITICAL additionally requires something about this ticket.*

Roster distribution moved from 32 critical / 13 urgent → **16 critical / 29 urgent**.
Tenant 4 now spreads across two levels and four distinct scores with the TankLink
duplicate cluster correctly at the top. The first cap I tried (`CRITICAL - 1`) left
one point of headroom and changed almost nothing — that failed attempt is recorded in
D-012 because it is the argument for the working value.

### Four bugs found by testing rather than reading

1. **Ticket id extraction missed `triage 1083`** — the pattern required the word
   "ticket". Now three patterns in descending confidence; a bare four-digit number
   only counts when it is the whole input or follows a cue word, because every year
   in this corpus is also four digits.
2. **`resolve_tenant` called an unknown name "ambiguous"** — it branched on
   candidates being present rather than on the match method, so `"Wobblegong Oil"`
   was told it "matches more than one customer". Different failures, different
   sentences.
3. **Nearest-suggestion list had no floor** — `"zzzzzzzz"` got three confident
   suggestions at score 0. Floor set at 50, measured: real typos score 67–100,
   nonsense scores 0.
4. **Two "last 30 days" conventions disagreed** — the triage snapshot used `>` where
   graded question Q5 uses `>=`, quietly returning 15 emergency orders where Q5
   asserts 17. Now pinned by a test that ties the two numbers together.

### Since you went to sleep

- **Q-001 resolved** — you chose `{1, 2, 7, 8}`; CLAUDE.md §9 updated to match.
- **SECURITY.md written** — three vulnerabilities with attack scenarios, fixes
  pointing at the code and tests that implement them, seven secondary issues, and an
  explicit residual-risk section.
- **DECISIONS.md completed** — cost model with measured token math, the 150-tenant
  scaling answer, and the end-customer agent answer. That closes every DECISIONS.md
  requirement in the assignment.
- **Q-017 fixed** — `config.LLM_MODEL` was `claude-sonnet-4-5` (not a current model
  ID) and the client passed `temperature=0.0` (removed on current models, returns
  400). **Both would have failed on your first live call**, and the whole suite
  passed throughout because every test drives a fake. Now `claude-opus-5` with
  `output_config.effort`.

### What needs your attention

Eighteen questions below. In priority order:

- **Q-012 — the agent has never spoken to a real model.** Still the biggest unknown.
  Q-017 is proof of the category: nothing in a fake-LLM suite can catch how the real
  API is called. Assume there are more once the key is in.
- **Q-015 — a pasted ticket is recognised but not parsed.** Last functional gap
  against the assignment's stated chat behaviour. ~1 hour.
- **Q-018 — structured outputs** would delete the fence-stripping JSON parser
  entirely and turn a class of refusal into an impossibility. ~30 minutes, best done
  while watching real responses.
- **Q-002, Q-005, Q-014** — domain judgements you may want to overrule (the
  `billing→invoicing` mapping, the −10% decline cut, the escalation weights).

### Next three tasks

1. **Add the key, then work Q-012 and Q-018 together.** Swap `FakeLLM` for
   `LLMClient` in `tests/test_sql_questions.py:_agent` and iterate. Expect trouble on
   Q2 ("last month" as a calendar month, not rolling 30 days) and Q4 (the
   `status = 'completed'` filter — the difference between 1467.7 and 1564.92, which
   no error will ever reveal).
2. **Voice mode (Step 5).** The only remaining assignment deliverable with nothing
   built. `ResolutionResult.needs_confirmation` and `SqlAnswer.date_anchor` already
   exist to drive read-back and the "as of 29 May" caveat. Design around the latency
   budget: two LLM calls per question (D-007), and prompt-caching the schema card
   (worth 26% of cost and more of the latency) is the cheapest win.
3. **Q-015 pasted tickets**, which finishes chat mode.

### Try this before the demo

```
use Fuel       -> refuses, offers three candidates
use CFS        -> binds to tenant 1
triage 1083    -> refused: belongs to another customer
platform       -> then triage 1083 works
```

Four lines that walk the entire isolation story, and none of them need an API key.

---

### Q-001 · CLAUDE.md §9 undercounts the cross-tenant questions — RESOLVED 2026-08-29
**What it says:** "questions 1 and 7 are cross-tenant. In a tenant-scoped session the agent must
refuse them."
**What the data says:** four of the eight are cross-tenant by construction — Q1 ("across all
tenants"), Q2 ("which tenant delivered the most"), Q7 ("by tenant"), Q8 ("list tenants with
declining volume"). Q2 and Q8 both range over all twelve tenants and are meaningless scoped to
one.
**Taken:** implemented the correct set `{1, 2, 7, 8}` as `CROSS_TENANT_QUESTIONS` in
`src/config.py`, because building to the documented set would make the agent answer Q2 and Q8
with a single tenant's rows and present it as a platform-wide ranking — a wrong answer that
looks right. **CLAUDE.md was not edited**, per §1's instruction to write conflicts here rather
than guess.
**Resolved:** you confirmed `{1, 2, 7, 8}`. CLAUDE.md §9 has been updated to match, with a
note recording the correction. `config.CROSS_TENANT_QUESTIONS` and
`tests/test_sql_questions.py` already carried the corrected set; nothing else changed.

### Q-002 · The `product_area` → module mapping is my inference
**Context:** DECISIONS.md D-002. `billing→invoicing` and `reporting→analytics` are not
documented anywhere in the provided data; I inferred them from the vocabularies. `integration`
and `login_access` I treated as platform-wide and gated by no module.
**Taken:** the conservative direction is to under-flag rather than over-flag, so anything not in
the map is never reported as a mismatch. Lives in `src/config.py` where it is one edit to fix.
**Needs you to:** confirm with FleetPanda domain knowledge. If `login_access` is in fact gated
by an SSO module, tenant 3's #1017 becomes a real mismatch and the count moves from 26 to 27+.

### Q-003 · Runtime is Python 3.12 via `uv`, not the system 3.9.6
**Context:** CLAUDE.md §5 requires Python 3.11+. The system interpreter is 3.9.6 with no
third-party packages; `python3.12` and `uv` were available under `~/.local/bin`.
**Taken:** created `.venv` on 3.12.13 and installed dependencies there. `.venv/` is gitignored.
All commands in README assume `.venv/bin/python`.
**Needs you to:** nothing, unless you intended a different interpreter or a global install.

### Q-004 · Recon scripts are not committed
**Context:** CLAUDE.md §4 fixes the directory layout and says not to invent top-level
directories. The three recon scripts have no home in that layout.
**Taken:** inlined the queries that produce each non-obvious number into RECON.md instead, so
every claim is reproducible without the scripts. The scripts were run from a scratch directory
and discarded.
**Needs you to:** decide whether a `scripts/` directory is worth adding to §4. My view: yes for
the live session — being able to re-run recon in front of the interviewer is worth one folder.

### Q-005 · "Declining delivery volume" (Q8) has no materiality threshold
**Context:** RECON.md §11. Anchored on the data, seven of twelve tenants are technically
declining, but t1 at **-1.5%** is noise and t4 at **-16.3%** is a real signal. The question as
written admits any negative delta.
**Taken:** provisional cut at **-10%**, held in `src/config.py:DECLINE_THRESHOLD_PCT`, and the
answer states the threshold it used. Conservative in the sense that it reports fewer tenants
and says why.
**Needs you to:** confirm -10%, or say the agent should list every negative delta and let the
reader judge.

### Q-006 · Field name in CLAUDE.md §7 does not match the data
**What it says:** "tickets referencing modules not in that tenant's `active_modules`".
**What the data says:** the key in `customers.json` is **`modules_active`**.
**Taken:** used `modules_active`. Written as documented it is a `KeyError`. Flagged rather than
silently corrected in CLAUDE.md, same reasoning as Q-001.
**Needs you to:** nothing beyond a one-word edit to CLAUDE.md if you want the file consistent.

### Q-007 · The date anchor is stated in prose, not in structured output — RESOLVED 2026-08-29
**Context:** DECISIONS.md D-001. The agent answers "in the 7 days to 2026-05-29 (most recent
data available)". That is honest for a human reader, but a downstream consumer parsing the JSON
response has no machine-readable field telling it the window was shifted 91 days.
**Taken:** prose only for now — no response schema exists yet at Step 3.
**Resolved:** `SqlAnswer` now carries `date_anchor` and `anchor_mode` as fields (D-009).
`window_start` / `window_end` were not added — the agent does not currently compute the
window boundaries in Python, SQLite does, so publishing them would mean re-deriving
them. Revisit if a consumer needs the exact bounds.

### Q-008 · LLM provider is assumed to be Anthropic
**Context:** CLAUDE.md §7 Step 3 names `ANTHROPIC_API_KEY` specifically; §3.1 says direct
provider SDK calls only.
**Taken:** `src/llm/client.py` wraps the Anthropic SDK and raises a configuration error when the
key is absent, per instruction. No live call is made this session, and no code path depends on
the key existing.
**Needs you to:** nothing, unless you want a provider-agnostic wrapper. My view: don't — one
provider, named directly, is easier to explain in the walkthrough than an abstraction with one
implementation.

### Q-009 · `UNION` queries are rejected outright
**Context:** DECISIONS.md D-005. sqlglot parses `SELECT ... UNION SELECT ...` with an
`exp.Union` root, and the guard only accepts an `exp.Select` root, so every UNION is
refused with "Only SELECT statements are permitted".
**Taken:** left as a rejection. Isolating a UNION is not hard — each arm is an
`exp.Select` and already gets a predicate from the existing traversal — but accepting a
root node type I have not tested against the full attack list is not a change to make
unattended. Refusing is the conservative direction and no test question needs UNION.
**Needs you to:** decide whether to accept `exp.Union` roots. My view: yes, in Step 4,
with the arm-level tests written first. It is roughly a five-line change plus tests.

### Q-010 · The post-execution assertion cannot see past the row cap
**Context:** DECISIONS.md D-004. Layer 3 inspects returned rows, so a leaking query
whose first `MAX_RESULT_ROWS` rows all belong to the bound tenant passes it. This is
not theoretical — the test written to prove the assertion fires initially passed for
the wrong reason, because the first 50 rows of `delivery_orders` are all tenant 1's.
**Taken:** kept the assertion (it reliably catches the aggregate/grouped case, which is
where a guard bug shows up first), and pinned the limitation in
`test_the_row_assertion_is_a_detector_not_a_guarantee` so it is visible rather than
assumed away.
**Needs you to:** decide whether to add a cheap `COUNT(DISTINCT tenant_id)` probe on the
unlimited query as a fourth layer. My view: not worth it — it doubles query cost to
defend against a bug the AST tests already cover, and layer 3's job is to be a smoke
alarm, not a second guard.

### Q-011 · `DESIGN.md` is a file CLAUDE.md §4 does not list
**Context:** CLAUDE.md §4 gives an exact file layout and says not to invent additions.
I followed that during the foundation session and wrote no design document, putting the
architecture into README.md, DECISIONS.md and module docstrings instead. On review that
distributed the end-to-end flow across six docstrings, with no single page showing a
request travelling through the system — the thing the 10-minute code walkthrough in the
live session actually needs.
**Taken:** added `DESIGN.md` at your explicit request. It duplicates no content: the
diagrams and the module-boundary table exist nowhere else. `implementation.md` was
deliberately *not* added — CLAUDE.md §7 is the implementation plan, and a second copy
would drift from it.
**Needs you to:** add `DESIGN.md` to the §4 layout so the charter and the repo agree.
Flagging separately that I should have logged this as a question during the foundation
session rather than silently deciding not to write it — the charter's rule is to log
conflicts, and "the layout omits something useful" is a conflict.

### Q-012 · The agent has never spoken to a real model
**Context:** No `ANTHROPIC_API_KEY` is present in this environment, and CLAUDE.md 7
forbade live calls during the foundation session. Every agent test is driven by
`tests/conftest.py:FakeLLM`, primed with the reference SQL.
**What that does and does not prove:** the plumbing is verified end to end — JSON
parsing including markdown fences, guard rewriting, execution, the retry-with-reasons
path, refusal before a wasted synthesis call, and the anchor reaching the synthesiser.
What is entirely unverified is whether a real model, given `build_sql_prompt()`,
actually produces SQL resembling `REFERENCE_SQL`. Prompt quality is untested.
**Taken:** wrote the expectations so the acceptance test is a one-line swap —
replace `FakeLLM` with `LLMClient` in `tests/test_sql_questions.py:_agent` and the
assertions should hold unchanged.
**Needs you to:** add a key and run that swap. Expect prompt iteration, particularly
on Q2 ("last month" as a calendar month, not a rolling 30 days) and Q4 (the
`status = 'completed'` filter, which is the difference between 1467.7 and 1564.92 and
which no error will reveal).

### Q-013 · Escalation level saturates within a struggling account — FIXED 2026-08-29
**Context:** DECISIONS.md D-010. Account-level signals (health, contract, CARR,
competitor) outweigh ticket-level ones (duplicates, priority, module gap), so all 12
of tenant 4's tickets and all 9 of tenant 8's score CRITICAL. Across the roster the
spread is reasonable — standard 20, elevated 20, urgent 13, critical 32, and the
criticals are concentrated in exactly the three accounts in real trouble (t2, t4, t8).
Within one of those accounts, though, the level cannot tell the worst ticket from the
mildest.
**Taken:** left as is. It is arguably correct — every ticket from an expired-contract,
health-28 account genuinely is critical — and `EscalationAssessment.score` still ranks
them (t4's range is 100–135), so a queue can sort on score even where the level
saturates.
**Fixed:** capped the account-state portion of the score at `ESCALATION_URGENT + 10`
(D-012), so account state sets an URGENT floor and only ticket-level signals reach
CRITICAL. Tenant 4's twelve tickets now spread across two levels and four distinct
scores, with the TankLink duplicate cluster at the top. Roster-wide the split moved
from 32 critical / 13 urgent to 16 critical / 29 urgent.

**Still worth your view:** the cap value (55) is tuned, not derived — the first
attempt at 69 left one point of headroom and changed almost nothing. And the brief
does not yet display `account_risk` / `ticket_risk` separately, though the assessment
carries them; showing both would make "urgent because of the account, critical
because of this ticket" legible at a glance.

### Q-014 · Escalation weights are calibrated by eye, not by outcomes
**Context:** The weights in `config.py` were set by scoring all 85 tickets and
checking the distribution looked sane. There is no outcome data — no record of which
tickets actually escalated, churned, or resolved quietly — so this is a cold-start
scorer and cannot be anything else with the data provided.
**Taken:** kept the weights, put every one in `config.py` with the reasoning inline,
and made the signals an audit trail so a disputed call can be traced without re-running
anything.
**Needs you to:** nothing now. Worth saying out loud in the live session if escalation
logic comes up — the honest answer is that the ordering is defensible and the magnitudes
are a guess awaiting feedback data.

### Q-015 · A pasted ticket is recognised but not parsed — FUNCTIONAL GAP
**Context:** The assignment says the user "types a question or **pastes a ticket**".
`Router.classify` detects a pasted ticket body (multi-line with form labels) and routes
it to triage, but `_triage` then needs a ticket **id** and asks for one. Triage works
today only for tickets already in `tickets.json`, addressed by number.
**Taken:** made the limitation explicit in the reply rather than guessing at the fields.
Parsing free-text into a `Ticket` needs a decision about what happens when the paste has
no tenant, no product_area, or a product_area outside the known vocabulary — and
inventing a `tenant_id` is precisely the failure this system is built to prevent.
**Needs you to:** decide the shape. My view: an LLM extraction call into a Pydantic
`PastedTicket` (subject, description, product_area, tenant_name), then run the tenant
name through `TenantResolver` and **refuse if it does not resolve** rather than
defaulting to the session's tenant. That reuses D-003's fail-closed rule instead of
adding a second one. Roughly an hour, and it is the last gap between the current build
and the assignment's stated chat behaviour.

### Q-016 · Intent classification falls through to the query path without a model
**Context:** `Router.classify` uses heuristics first and only asks the model for
genuinely ambiguous input. With no API key there is no model, so ambiguous input is
treated as a dispatch query.
**Taken:** deliberate — the SQL agent's own refusal ("I could not turn that into a
query") is a better error than "I do not know what you meant", and it keeps the CLI
demoable with no key.
**Needs you to:** nothing. Flagged only because it means the unmodelled CLI behaves
subtly differently from the modelled one, which is worth knowing before a demo.

### Q-017 · The model ID and sampling parameters were both stale — FIXED 2026-08-29
**Context:** `config.LLM_MODEL` was `claude-sonnet-4-5` and `LLMClient.complete` passed
`temperature=0.0`. Neither would have survived the first live call: that model ID is not
current, and `temperature` / `top_p` / `top_k` are **removed** on current Claude models
and return a 400. Every test passed throughout, because every test drives a `FakeLLM`.
**Taken:** model is now `claude-opus-5`; `temperature` replaced with
`output_config.effort` — `medium` for SQL generation (turning "list tenants with
declining volume" into a two-window CTE is not a trivial translation) and `low` for
synthesis (it is handed the rows, does no arithmetic, and sits on the voice critical
path). Determinism now comes from the guard and from tests asserting results rather
than generated text, which is where it was actually enforceable anyway.
**Needs you to:** nothing, but note this is exactly the class of bug Q-012 warns about —
the fake-LLM suite cannot catch anything about how the real API is called. Worth
assuming there are more once you add a key.

### Q-018 · Structured outputs would remove the JSON-parsing hack
**Context:** `SqlAgent._parse_generation` strips markdown fences and hand-parses JSON
because models fence their output despite instructions. The current API supports
constrained structured output (`output_config.format`, or `client.messages.parse()`
against a Pydantic model), which makes malformed output impossible rather than handled.
**Taken:** left as is for now. The parsing path is tested, including the fence case and
five malformed-input cases, and changing it is a contract change to `SqlGeneration`
better done while watching real responses.
**Needs you to:** decide when you add the key. My view: switch to `messages.parse()` with
the existing `SqlGeneration` model — it deletes `_parse_generation` entirely and turns a
whole class of refusal into an impossibility. Roughly 30 minutes including test updates.

### Q-019 · No authentication anywhere — BLOCKING for any deployed surface
**Context:** Found in a security audit of the implementation (2026-08-29), not of the
README's exercise snippet. There is no authentication or authorization in this codebase.
A CLI user selects their own tenant with `use <company>` and escalates to cross-tenant
access by typing `platform`. Session scope is **self-asserted**.

This is structurally identical to README vulnerability V1 (`tenant_id = body.get(...)` —
caller-supplied), which SECURITY.md correctly condemns. The fixed endpoint in that
document says *"the tenant comes from the verified session, never from the request
body"*; the interface actually shipped does the opposite. **The documentation currently
claims a posture the code does not have**, and that gap is the finding as much as the
missing auth is.

**Severity:** not remotely exploitable today — there is no network surface, and the
"attacker" is whoever already owns the process and the SQLite file. It becomes critical
the moment any of this is exposed: an HTTP endpoint, a shared host, or a voice line.

**Taken:** nothing. Fixing it means introducing an auth boundary, a principal model and a
session store — a larger piece of work than the CLI it would protect, and one that should
be designed rather than bolted on overnight.

**Needs you to:** decide the shape before anything ships. My view:
1. `TenantContext` must only ever be constructed from a verified principal. Make that
   structural — move `for_tenant()` behind a factory that takes a `Principal`, so
   "scope this session to tenant 7" is not an expressible operation without one.
2. `platform` scope requires an explicit internal role, never a runtime command.
3. Until then, treat the CLI as a local developer tool and say so in the README, rather
   than letting SECURITY.md imply an auth boundary exists.

Related: F2 (`needs_confirmation` unenforced) and F3 (ticket enumeration oracle) from the
same audit are **fixed** — see the audit section in SECURITY.md.
