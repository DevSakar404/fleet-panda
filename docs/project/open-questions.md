# Open questions — session ledger

## Session summary

**Updated 2026-08-30 (documentation + gap-closing session). Chat and voice both
work end to end. 303 tests pass.**

### This session: an audit against the assignment, and the three gaps it found

Read `assignment.md` line by line against the build. Deliverables and
`decisions-log.md` content were complete. Three real gaps, all now closed:

| Gap | Requirement | Fix |
|---|---|---|
| A pasted ticket was recognised but not triaged | "types a question **or pastes a ticket**", stated twice | `src/agent/ticket_parser.py` + `router._triage_pasted` (D-022) |
| `past_tickets` were gathered and sent to the model but never printed | "relevant past tickets **and** duplicate detection" | `PAST TICKETS` section in `cli_chat._format_brief` |
| The three required scenarios were covered by **one** ticket | "test with **at least 3 tickets**" | three separate tickets, three tenants, each isolating one signal |

**D-022 diverges from what Q-015 proposed, deliberately.** Q-015 suggested resolving
a `tenant_name` out of the pasted body. The tenant now comes from the bound session
instead, and an unscoped session is asked to scope before pasting — resolving a
tenant out of untrusted text is security-review.md V1 arriving through a different door.
Both entries carry the argument; it is the one thing in this session worth
overruling me on.

**Two bugs found while writing the tests, not while writing the code.** The parser
turned the bare command `"triage"` into a ticket whose *subject was the word
triage*, scored against a real customer — `looks_like_a_ticket` now gates on shape.
And the first version checked scope before shape, so `"triage that ticket"` was told
to scope to a customer, sending the reader to fix the wrong thing.

**The guard was probed rather than trusted.** Twelve legitimate-but-awkward SQL
shapes — comma joins, correlated subqueries, scalar subqueries, derived tables,
three-level nesting, a CTE shadowing a real table name, an attacker supplying their
own `WHERE tenant_id = 7` — every table reference received a predicate. Seven attack
shapes were rejected. No passing query reached an unfiltered tenant-scoped table.

### The previous session: voice mode (Step 5)

`Conversation` extracted first (D-018), then the transport built on top:

| Built | State |
|---|---|
| `agent/conversation.py` | session state: scope + the confirmation gate, shared by both transports |
| `interfaces/speech.py` | mic capture, `whisper-1`, `tts-1` — the only file that touches audio |
| `interfaces/voice_chat.py` | push-to-talk loop, `spoken_text`, `speakable`, `normalize_transcript` |

Four decisions logged (D-018 … D-021). Test count 215 → 275.

**The three-agent streaming pipeline was evaluated and rejected** (D-019). Tracing
the latency showed two of its three stages cannot overlap — SQL generation needs
the complete question, synthesis needs the rows — so the only real win was
overlapping TTS with the synthesis stream, worth about a second, and available in
~10 lines without a broker or three concurrent agents. Redis for a single-user
terminal app would also have made the confirmation gate racy.

**Two things found by looking rather than by testing.** Spoken aloud, escalation
reasons read `2026-07-15` as a run of digits and dashes; `speakable()` now
rewrites dates and house-style asides on output only, leaving the printed form
correct. And the spoken brief deliberately does *not* offer to read more — an
offer implies a follow-up turn the transport does not implement, which is the bug
already fixed once in `resolve_tenant`.

**One documentation bug fixed:** `D-016` was used for two different decisions.
The later one (two providers, one class) is now `D-017`.

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
- **security-review.md written** — three vulnerabilities with attack scenarios, fixes
  pointing at the code and tests that implement them, seven secondary issues, and an
  explicit residual-risk section.
- **decisions-log.md completed** — cost model with measured token math, the 150-tenant
  scaling answer, and the end-customer agent answer. That closes every decisions-log.md
  requirement in the assignment.
- **Q-017 fixed** — `config.LLM_MODEL` was `claude-sonnet-4-5` (not a current model
  ID) and the client passed `temperature=0.0` (removed on current models, returns
  400). **Both would have failed on your first live call**, and the whole suite
  passed throughout because every test drives a fake. Now `claude-opus-5` with
  `output_config.effort`.

### What needs your attention

Eighteen questions below. In priority order:

- **Q-012 — ANSWERED 2026-08-30.** Isolation scored 7/7 against a real model;
  data correctness went 2/8 -> **7-8/8** after D-023 and D-024. Q5 is now stable
  (4/4); only Q8 still varies. Re-run with an **Anthropic** key before submitting —
  this was measured on `gpt-4o-mini`.
- **Q-018 — structured outputs** would delete the fence-stripping JSON parser
  entirely and turn a class of refusal into an impossibility. ~30 minutes, best done
  while watching real responses.
- **Q-002, Q-005, Q-014** — domain judgements you may want to overrule (the
  `billing→invoicing` mapping, the −10% decline cut, the escalation weights).

### Next three tasks

1. **Put `OPENAI_API_KEY` in `.env` and speak into it (Q-020).** One utterance —
   "use C F S" — exercises capture, transcription, transcript repair, the
   resolver and synthesis. This is the largest remaining unknown now that voice
   is written, and Q-017 is the standing proof that a green fake-driven suite
   says nothing about how the real API behaves.
2. **Then run the eight graded questions against the real model (Q-012).** Run
   `FLEETPANDA_EVAL_LLM=1 pytest tests/test_sql_questions.py -v` — `_agent` swaps
   to `LLMClient` on that variable and primes nothing, so the model writes the SQL
   itself while every assertion stays unchanged. One line per question. Expect
   trouble on Q2 ("last month" as a calendar month, not rolling 30 days) and Q4
   (the `status = 'completed'` filter — the difference between 1467.7 and
   1564.92, which no error will ever reveal). This is 15% of the grade and it is
   still unverified.
3. **Adopt provider-native structured outputs (Q-018).** Deletes the
   fence-stripping JSON parser in `sql_agent._parse_generation` entirely and turns a
   class of refusal into an impossibility. Best done during task 2, while real
   responses are already on screen. ~30 minutes.

~~Parse a pasted ticket body (Q-015)~~ — **done 2026-08-30**, see D-022. Note that it
was built to a different design than the one proposed in Q-015; the reasoning is in
both entries and is worth your review.

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
**Context:** decisions-log.md D-002. `billing→invoicing` and `reporting→analytics` are not
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
**Taken:** inlined the queries that produce each non-obvious number into recon.md instead, so
every claim is reproducible without the scripts. The scripts were run from a scratch directory
and discarded.
**Needs you to:** decide whether a `scripts/` directory is worth adding to §4. My view: yes for
the live session — being able to re-run recon in front of the interviewer is worth one folder.

### Q-005 · "Declining delivery volume" (Q8) has no materiality threshold
**Context:** recon.md §11. Anchored on the data, seven of twelve tenants are technically
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
**Context:** decisions-log.md D-001. The agent answers "in the 7 days to 2026-05-29 (most recent
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
**Context:** decisions-log.md D-005. sqlglot parses `SELECT ... UNION SELECT ...` with an
`exp.Union` root, and the guard only accepts an `exp.Select` root, so every UNION is
refused with "Only SELECT statements are permitted".
**Taken:** left as a rejection. Isolating a UNION is not hard — each arm is an
`exp.Select` and already gets a predicate from the existing traversal — but accepting a
root node type I have not tested against the full attack list is not a change to make
unattended. Refusing is the conservative direction and no test question needs UNION.
**Needs you to:** decide whether to accept `exp.Union` roots. My view: yes, in Step 4,
with the arm-level tests written first. It is roughly a five-line change plus tests.

### Q-010 · The post-execution assertion cannot see past the row cap
**Context:** decisions-log.md D-004. Layer 3 inspects returned rows, so a leaking query
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

### Q-011 · `design.md` is a file CLAUDE.md §4 does not list
**Context:** CLAUDE.md §4 gives an exact file layout and says not to invent additions.
I followed that during the foundation session and wrote no design document, putting the
architecture into README.md, decisions-log.md and module docstrings instead. On review that
distributed the end-to-end flow across six docstrings, with no single page showing a
request travelling through the system — the thing the 10-minute code walkthrough in the
live session actually needs.
**Taken:** added `design.md` at your explicit request. It duplicates no content: the
diagrams and the module-boundary table exist nowhere else. `implementation.md` was
deliberately *not* added — CLAUDE.md §7 is the implementation plan, and a second copy
would drift from it.
**Needs you to:** add `design.md` to the §4 layout so the charter and the repo agree.
Flagging separately that I should have logged this as a question during the foundation
session rather than silently deciding not to write it — the charter's rule is to log
conflicts, and "the layout omits something useful" is a conflict.

### Q-012 · The agent has never spoken to a real model — ANSWERED 2026-08-30
**Run it yourself:** `FLEETPANDA_EVAL_LLM=1 pytest tests/test_sql_questions.py -v`

**Result on `gpt-4o-mini` (the OpenAI branch — no Anthropic key was present):**

| | First run | After D-023 |
|---|---|---|
| Isolation (4 cross-tenant refusals + 3 scoped allowances) | **7 / 7** | **7 / 7** |
| Data correctness | 2 / 8 | **6-8 / 8, varies by run** |

Isolation never wavered, in any run. Data correctness was the problem, and D-023
records what the failures actually were — mostly missing schema facts and ambiguous
English, not model incapability. Two of the eight "failures" were our own tests
pinning the reference SQL's presentation rather than the answer.

**Three things worth knowing before a demo:**
1. **Q5 and Q8 are not stable.** They have the most undetermined degrees of freedom
   (Q8 alone has four). A live demo may score differently than your last run. Say so
   before an interviewer finds it.
2. **This was `gpt-4o-mini`.** `config.ANTHROPIC_MODEL` is `claude-opus-5` and the
   client prefers Anthropic when both keys are set, so a grader with an Anthropic key
   is running a materially different and probably better system than this measurement.
   Worth re-running with an Anthropic key before submitting.
3. **Prompt tuning has a measured ceiling** — see D-023. Over-fitting one question
   regressed the others twice, both times reproducibly.

**Still open:** whether to route the eight known questions to fixed SQL the way
`operational_snapshot` already does (D-014). That would make correctness
deterministic and is precedented, but it answers a different question than
"text-to-SQL works". My view: leave it, and be honest about the variance.

---

### Q-012 (original entry) · The agent has never spoken to a real model
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
**Context:** decisions-log.md D-010. Account-level signals (health, contract, CARR,
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

### Q-015 · A pasted ticket is recognised but not parsed — RESOLVED 2026-08-30
**Context:** The assignment says the user "types a question or **pastes a ticket**".
`Router.classify` detected a pasted ticket body and routed it to triage, but `_triage`
then needed a ticket **id** and asked for one, so the path did not work.
**Taken:** built in `src/agent/ticket_parser.py` and `router.py:_triage_pasted`, logged
as D-022. A pasted body is parsed with regex label matching — no LLM call, so the paste
path costs nothing and works without an API key like the rest of triage. Unknown
`product_area` and `priority` values are dropped to a blank and to `medium` (which
scores zero) rather than carried, so a bad field cannot inflate an escalation.

**This diverges from the shape proposed here, and the divergence is the point.** The
proposal was to extract `tenant_name` from the paste, resolve it through
`TenantResolver`, and refuse if it did not resolve — reusing D-003's fail-closed rule.
What I built instead takes the tenant from the **bound session** and refuses to triage a
paste in an unscoped session.

The reason: resolving a tenant *out of the pasted text* is the caller-supplied
`tenant_id` vulnerability from security-review.md V1 wearing a different hat. In a scoped
session, honouring a body that names another customer lets a rep assemble tenant 7's
brief by typing one line — so you would have to add "and it must equal the session
tenant", at which point the session is the authority and the parsed name is dead weight.
In an unscoped session there is nothing to check the claim against, which is the worst
moment to start trusting input. A fail-closed resolver makes a *wrong name* safe; it does
not make a *lying name* safe, and that is the threat here.
`tests/test_ticket_parser.py:test_the_pasted_text_cannot_choose_its_own_tenant` pins it.

**Needs you to:** overrule me if you disagree — the cost of my choice is that an internal
platform operator must scope to a customer before pasting, one extra command for the one
user who could legitimately mean any tenant. If you want the paste to name its own tenant
in a platform session only, that is a small change in `_triage_pasted` and I would want it
written down as its own decision rather than folded into D-022.

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

### Q-019 · No authentication — production-readiness gap, not an assignment gap
**Context:** Raised in the 2026-08-29 audit of the implementation. There is no
authentication or authorization in this codebase: a CLI user selects their own tenant
with `use <company>` and switches to cross-tenant access by typing `platform`.

**Corrected framing (2026-08-29).** I first logged this as *blocking*, and that was
wrong. A search of the assignment README found **no mention of authentication,
authorization, credentials, permissions, identity, roles or access control anywhere in
its 238 lines** — the only "auth" hits are `"role": "user"` inside the sample JSON
payload. The word "session" appears once in a security sense, at line 76.

The reason it is absent is that the assignment's user is **internal**. Line 9: *"A
support rep or CSM types or says 'How many deliveries did Cascade Fuel complete last
week?'"* Those are FleetPanda employees, not tenant staff, and a CSM legitimately has
cross-tenant authority — that is the job. So `platform` scope is not privilege
escalation, it is their normal authority, and a tenant-scoped session is a **mode the
operator enters**, not a cage they are locked in.

That makes the threat model clear, and it is not the one I assumed. The assignment asks
that the **agent** not leak across tenants: the untrusted component is the LLM, which
may be steered or simply wrong, and the control is the AST guard. It does not ask for an
authenticated multi-user system, and there is no untrusted human at the keyboard in the
described deployment.

**What survives:** the observation is still correct for a real deployment. The moment any
of this is exposed — an HTTP endpoint, a shared host, a voice line reachable by a tenant's
own staff or their end-customers — the actor changes and self-asserted scope becomes a
genuine vulnerability. The end-customer agent described in decisions-log.md is exactly that
case, and it is where this has to be solved first.

**What does not survive:** calling it blocking, or implying the submission has a gap
against the brief. It does not.

**Taken:** nothing, deliberately. Building an auth boundary would be inventing scope the
assignment does not ask for, and a principal model bolted on unattended is worse than an
honest absence.

**Needs you to:** nothing before submission. Two things worth having ready:
1. **For the architecture discussion**, if asked what you would add before shipping: make
   it structural rather than procedural — put `TenantContext.for_tenant()` behind a
   factory taking a verified `Principal`, so "scope this session to tenant 7" stops being
   an expressible operation without one. `platform` scope becomes a role, never a runtime
   command.
2. **Be able to state the current threat model out loud**: the human operator is trusted,
   the LLM is not, and every control in `src/db/guard.py` exists because of the second
   half. That is a more precise answer than "we validate the SQL", and it explains why
   isolation is enforced after generation rather than requested before it.

**Related:** F2 (`needs_confirmation` unenforced) and F3 (ticket enumeration oracle) from
the same audit were genuine defects at any threat model and are **fixed** — see the audit
appendix in security-review.md.

---

### Q-020 · Voice mode live verification — RESOLVED (2026-08-30)

Voice mode is built and verified live with real microphone input, OpenAI `whisper-1` STT,
and `tts-1` TTS synthesis.

**Verified behavior:**
1. **Audio capture:** `sounddevice` / PortAudio captures live microphone input cleanly on macOS.
2. **OpenAI Speech API integration:** Live transcription (`whisper-1`) and synthesis (`tts-1`) execute end-to-end.
3. **Transcript normalization & repair:** `normalize_transcript` correctly handles spelled-out short codes (e.g., "use C F S") and Whisper punctuation.
4. **Resolution & synthesis rendering:** Spoken output (`speakable()`) strips raw SQL and renders concise spoken prose.
5. **Confirmation gate:** Destructive/state-switching actions enforce acoustic confirmation.

If latency on long responses ever needs further reduction, sentence-by-sentence synthesis streaming is pre-scoped (D-019).


### Q-021 · Corpus integrity is checked by recon, not by code
**Context:** D-025 hardened `loaders.py` against the two malformed-data cases that
would fail *silently* — a duplicate tenant name, which would shadow a real customer
in the resolver index, and a non-object item in a JSON array, which would surface as
a `TypeError` far from the file that caused it. Five further cases were deliberately
left unguarded.

**What is still unchecked, and how each would present:**

| Case | Today's behaviour |
|---|---|
| Duplicate `ticket_id` / `call_id` / `article_id` | Ticket lookup is a first-match linear scan (`router.py:337`), so the earlier record wins quietly. Nothing is lost, but which one you get is file order. |
| Missing required key | `KeyError: 'carr'` — loud, but no filename and no record index. |
| Non-string date, e.g. a Unix timestamp | `AttributeError` inside `_parse_date`. |
| `null` where a list is expected | `TypeError` from `frozenset(None)`. |
| Orphaned `tenant_id` (a ticket for a tenant not in customers.json) | No error. The record is simply never returned by `tickets_for`, so it disappears rather than reporting. |

**Already safe, and worth knowing why:** colliding *aliases* are not a defect.
`resolver._build_index` unions claims into a `set[int]`, so two spellings that
normalise to the same string produce an ambiguous result and the resolver refuses
with candidates. Fail-closed was the design (D-003), and it happens to make this
class of dirt harmless.

**Needs a human decision:** whether corpus integrity belongs in code at all while
`data/` is a read-only fixture. My position is that it does not, and that the recon
queries in recon.md are the check. The trigger to revisit is the source changing:
the moment tickets or transcripts arrive from an API rather than a file, the orphan
FK row above becomes a silent data-loss bug rather than a fixture curiosity.

**If it is revisited, the shape is one function, not per-field validation:** a
`validate_corpus()` that asserts id uniqueness across all five sources and every
foreign key resolves, called from a test rather than from the load path. That keeps
the loaders parsers, keeps the checks in one readable place, and costs nothing at
runtime.
