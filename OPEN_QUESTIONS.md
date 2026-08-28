# OPEN_QUESTIONS.md

## Session summary

**Foundation session, 2026-08-28. Steps 0-3 complete. 94 tests pass, 12 skip
(the agent-path tests, as scoped). Five commits, one per step plus a baseline.**

### What was built

| Layer | Files | State |
|---|---|---|
| Recon | `RECON.md` | Complete — 12 sections, every claim script-derived |
| Data | `config.py`, `data/loaders.py`, `data/resolver.py`, `data/sources.py`, `data/repository.py` | Complete, tested |
| Database | `db/connection.py`, `db/schema.py`, `db/guard.py`, `db/executor.py` | Complete, tested |
| Session | `agent/session.py` | Complete (pulled forward from Step 3 — the guard needs it) |
| LLM | `llm/client.py`, `llm/prompts.py` | Complete, unexercised (no live call this session) |
| Agent | `agent/router.py`, `sql_agent.py`, `triage_agent.py`, `escalation.py`, `interfaces/cli_chat.py` | Stubs with full specs in docstrings |

Tenant isolation is three independent layers: a read-only connection SQLite itself
enforces, an AST rewrite that puts `tenant_id = N` on every scoped table reference
including subqueries and CTE bodies, and a post-execution assertion on returned
rows. Cross-tenant questions are refused in a scoped session rather than narrowed.

### What recon turned up that changed the plan

1. **The dataset ends 2026-05-29 — 91 days before today.** Anchored on
   `date('now')`, questions 1, 2, 5 and 8 return *zero rows*. Correct SQL, useless
   answer, and in a live demo it reads as a broken agent. Everything relative is
   now anchored on `MAX(delivery_date)` and the agent states the anchor in its
   reply (D-001). This was the single most consequential finding.
2. **`token_set_ratio` scores a bare substring 100.** `"Fuel"` matches three
   tenants at full confidence. A score-gated resolver would have leaked across
   tenants *before any SQL exists*, where the guard cannot see it. The gate is now
   the number of distinct tenants above threshold, not the score (D-003).
3. **`product_area` and `modules_active` are different vocabularies.** The obvious
   module-mismatch check flags 58 of 85 tickets; the mapped version flags 26 real
   ones (D-002). The naive version would have looked like a working feature.
4. **Only `tank_readings` fans out** (900 rows / 30 customers, 9.06x inflation).
   Every other foreign key is 1:1, so Q2 and Q7 are safe provided nothing reaches
   for it. Narrower than the charter's warning implied.
5. **`SCHEMA.md` documents enum literals that never occur.** The schema card is
   introspected from the live database instead (D-006).
6. **Entity resolution against the provided data has zero failures.** The alias
   table covers every transcript name exactly. Fuzzy matching is therefore for
   voice and free text, not for ingest — which reframes what the resolver is for.
7. **Ticket #1083 (tenant 4) satisfies all three mandated triage test cases at
   once**: health 28, contract expired 2026-07-15, 4th duplicate filing in 26
   days, and asks about `tank_monitor` which that tenant does not have.

Three bugs were found by testing rather than by reading, all in the guard, all
silent: a sqlglot argument rename that disabled predicate injection entirely
while still producing valid SQL; a case-sensitive allowlist against
case-insensitive SQLite identifiers; and a schema-qualified name that could
reference outside the opened database. The first is why there are three layers
and not one (D-004).

### What is stubbed

`router.py`, `sql_agent.py`, `triage_agent.py`, `escalation.py`, `cli_chat.py` —
each raises `NotImplementedError` with its intended data flow written out in the
module docstring. Two Step 4 design arguments are pre-made there: intent
classification should try heuristics before spending an LLM round trip (it sits on
the voice critical path), and KB retrieval over 12 articles does not need a vector
database when `product_area` plus symptom overlap solves it exactly.

`SECURITY.md` is headings only, as scoped. Voice mode is untouched.

### What needs your decision

Ten questions below. The four that actually matter:

- **Q-001** — CLAUDE.md §9 says questions 1 and 7 are cross-tenant; it is 1, 2, 7
  and 8. I implemented the corrected set and left the charter unedited. *This one
  needs a real answer before Step 4.*
- **Q-002** — the `billing→invoicing` / `reporting→analytics` mapping is my
  inference and drives the whole module-mismatch feature.
- **Q-005** — the -10% materiality threshold for "declining volume" (Q8).
- **Q-009** — `UNION` is currently rejected outright. Cheap to support, but not a
  change to make unattended.

### Next three tasks

1. **`sql_agent.py`** — question → `build_sql_prompt()` → guarded SQL → rows →
   prose. The correctness oracle already exists: the eight reference tests in
   `test_sql_questions.py` assert real numbers, so un-skip the agent half and work
   until it matches. Includes one retry that feeds `GuardResult.reasons` back to
   the model, and never retrying a `TenantIsolationError`.
2. **`escalation.py`** — pure scoring functions with unit tests, before any triage
   prompt exists. Signals and thresholds are enumerated in its docstring and
   already in `config.py`. Ticket #1083 is the fixture.
3. **`router.py` + `cli_chat.py`** — enough to demo chat mode end to end, printing
   the guard's rewritten SQL alongside each answer. That demo is what makes the
   isolation work visible in the live session.

---

Questions raised during the build. Per CLAUDE.md §8, none of these blocked: each was resolved
by taking the more conservative option, and the alternative is recorded here for review.

---

### Q-001 · CLAUDE.md §9 undercounts the cross-tenant questions — CONFLICT WITH SOURCE OF TRUTH
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
**Needs you to:** confirm the corrected set and update CLAUDE.md §9, or tell me the documented
set was deliberate.

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

### Q-007 · The date anchor is stated in prose, not in structured output
**Context:** DECISIONS.md D-001. The agent answers "in the 7 days to 2026-05-29 (most recent
data available)". That is honest for a human reader, but a downstream consumer parsing the JSON
response has no machine-readable field telling it the window was shifted 91 days.
**Taken:** prose only for now — no response schema exists yet at Step 3.
**Needs you to:** decide whether the eventual response model carries an explicit
`window_start` / `window_end` / `anchor_mode` triple. My view: yes, and voice mode should say
the anchor out loud on the first query of a session only.

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
