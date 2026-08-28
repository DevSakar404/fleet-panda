# DECISIONS.md — engineering journal

Appended at the moment each decision was made, in the format defined in CLAUDE.md §6.
Only contested decisions are logged.

---

### D-001 · Anchor relative date windows on the data, not on `now()`
**Date:** 2026-08-28
**Context:** Recon (RECON.md §2) found the operational window ends **2026-05-29**, 91 days
before today. Four of the eight graded questions use a relative window ("last 7 days", "past
30 days", "last month", "last 30 vs previous 30"). Anchored on `date('now')` they return
literally zero rows: Q1 = 0, Q5 = 0, Q2 = 0. The SQL is correct, the answer is useless, and in
a live demo it reads as a broken agent.
**Options considered:**
- A. Use `date('now')`. Literally correct, returns 0 rows for half the graded questions.
- B. Freeze a fake "today" (`2026-05-29`) in config and pass it into the prompt. Demo works,
  but the agent lies about what day it is and the lie is invisible in the answer.
- C. Anchor on `MAX(delivery_date)` computed from the data at query time, and state the anchor
  in the natural-language answer.
**Chosen:** C. It is the only option that is both non-empty and honest: the answer says "in the
7 days to 2026-05-29 (most recent data)". A CSM reading that immediately understands they are
looking at a stale fixture, which is exactly the right thing for them to know. It also survives
the data being refreshed — no constant to update.
**Trade-off accepted:** every relative-window query pays a `MAX()` subquery (a full scan today,
since there are no indexes — RECON.md §12), and the SQL is harder to read than
`date('now','-7 day')`. The anchor also shifts if the dataset is extended, so two runs on
different data are not comparable without reading the stated anchor.
**Where it lives:** `src/config.py:DATE_ANCHOR_MODE`, applied in `src/llm/prompts.py` (schema
card preamble) and asserted in `tests/test_sql_questions.py`.

---

### D-002 · Module-mismatch detection needs an explicit vocabulary map
**Date:** 2026-08-28
**Context:** The assignment requires detecting "a ticket referencing a module the customer
doesn't actually have active". The obvious implementation is
`ticket.product_area not in customer.modules_active`. Run against the real data that flags
**58 of 85 tickets** (RECON.md §8), which is not a detector, it is noise. The cause: the two
fields are different vocabularies. `product_area` has `billing`, `integration`, `login_access`,
`reporting`; `modules_active` has `invoicing`, `analytics`, `customer_portal`, `driver_app`,
`route_builder`. They share only `dispatch`, `pricing`, `tank_monitor`.
**Options considered:**
- A. Bare `not in`. Zero code, 58 false positives, detector is worthless.
- B. Only check the three literals that appear in both vocabularies. Honest but misses the real
  cases — t8 filing `billing` tickets with no `invoicing` module is exactly the signal wanted.
- C. Explicit `AREA_TO_MODULE` map plus an `UNGATED` set for platform-wide areas
  (`integration`, `login_access`) that no module gates.
**Chosen:** C. Flags **26 of 85**, and every one inspected is a genuine entitlement gap. The
map is five lines and is the kind of domain knowledge that belongs in config where a FleetPanda
employee can correct it, not buried in a prompt.
**Trade-off accepted:** the map is my inference, not documented anywhere in the provided data —
`billing→invoicing` and `reporting→analytics` are judgement calls a FleetPanda PM might reject.
It is also a hardcoded mapping that goes stale when a module is renamed. Both are why it lives
in `config.py` with a comment rather than in the detection function. Logged for human review as
OPEN_QUESTIONS.md Q-002.
**Where it lives:** `src/config.py:AREA_TO_MODULE` / `UNGATED_PRODUCT_AREAS`, consumed by
`src/agent/escalation.py:detect_module_mismatch`.

---

### D-003 · The resolver gates on candidate count, not on match score
**Date:** 2026-08-28
**Context:** CLAUDE.md §3.5 requires entity resolution to fail closed. The natural reading is
"accept the top fuzzy match if its score clears a threshold". Recon (RECON.md §6) shows that is
unsafe with `rapidfuzz.fuzz.token_set_ratio`, which scores a **subset** of tokens as a perfect
100: the probe `"Fuel"` scores 100 against both Cascade Fuel Services (t1) and Great Lakes Fuel
Co (t5); `"Energy"` scores 100 against t3, t7 **and** t12. A top-score resolver answers `"Fuel"`
with tenant 1 at full confidence — a silent cross-tenant leak originating before any SQL is
generated, so the AST guard never sees it.
**Options considered:**
- A. Threshold on score alone. Fails exactly as above; the score carries no ambiguity signal.
- B. Swap to `fuzz.ratio` or `WRatio`, which punish length mismatch. Fixes `"Fuel"` but breaks
  the legitimate short aliases the data actually uses (`NSP`, `PWF`, `SEG`, `RES`).
- C. Keep `token_set_ratio` for recall, then gate on **how many distinct tenant_ids** clear the
  threshold. Exactly one → resolved. Two or more → `Unresolved` with ranked candidates,
  whatever the scores are.
**Chosen:** C. Recall and safety are separable concerns and this separates them: the scorer's
job is to find candidates, the gate's job is to refuse when candidates disagree. It also
produces the ranked candidate list that voice mode needs in order to ask "did you mean Cascade
Fuel Services or Great Lakes Fuel Co?".
**Trade-off accepted:** the agent will refuse some inputs a human would find obvious — a rep
typing just `"Fuel"` gets a clarifying question rather than an answer. In a multi-tenant SOC 2
system that is the correct direction to be wrong in, but it does cost a conversational turn.
**Where it lives:** `src/data/resolver.py:TenantResolver.resolve`, seeded from the probe table
in RECON.md §6 into `tests/test_entity_resolution.py`.

---

### D-004 · Three independent isolation layers, none trusted to be sufficient
**Date:** 2026-08-28
**Context:** CLAUDE.md section 3.2 mandates AST enforcement. The question this
decision settles is what else runs alongside it, and the answer came from a bug
found while building it: sqlglot 30 renamed the `Select` node's `from` argument to
`from_`, so `select.args.get("from")` returned `None`, `_direct_sources` yielded no
tables, and **every query was rewritten with no tenant predicate at all**. The SQL
parsed, executed, and returned plausible rows. A single-layer design would have
shipped that.
**Options considered:**
- A. AST guard alone. Fails exactly as above, silently, on a dependency upgrade.
- B. Guard plus read-only connection. Stops writes but not cross-tenant reads,
  which is the leak that actually matters here.
- C. Guard, read-only connection, **and** a post-execution assertion that no
  returned row carries a foreign `tenant_id`.
**Chosen:** C. The layers fail differently on purpose: layer 1 is enforced by
SQLite and cannot be reasoned around, layer 2 understands scope and is the only
one that can filter, layer 3 is the only one that inspects real data and therefore
the only one that can catch a bug in layer 2.
**Trade-off accepted:** layer 3 is a detector, not a guarantee — it can only see
rows that came back, so an unfiltered query whose first 200 rows happen to belong
to the bound tenant passes it. That is not hypothetical: the test asserting it
fires initially passed for the wrong reason, because the first 50 orders in the
table are all tenant 1's. The limitation is pinned in
`test_the_row_assertion_is_a_detector_not_a_guarantee` so nobody mistakes layer 3
for the primary control.
**Where it lives:** `src/db/connection.py` (layer 1), `src/db/guard.py:SqlGuard.check`
(layer 2), `src/db/executor.py:QueryExecutor._assert_no_foreign_tenant` (layer 3).

---

### D-005 · The guard traverses scopes structurally, and never indexes an arg by name
**Date:** 2026-08-28
**Context:** Injecting `tenant_id = N` requires knowing which tables are in which
SELECT's scope. Two sub-decisions were contested.
First, *how to find the tables*: `select.find_all(exp.Table)` is the obvious call
and is wrong — it descends into subqueries, so an outer SELECT tries to filter a
table that only exists in an inner scope, producing SQL that references an alias
not in scope. Second, *how to reach the FROM clause*: `select.args["from"]` is the
documented way and broke silently on a minor-version upgrade (see D-004).
**Options considered:**
- A. `find_all(exp.Table)` per SELECT. Simple, wrong across scopes.
- B. `sqlglot.optimizer.scope.traverse_scope`. Correct and purpose-built, but it
  is a second mental model to hold, and CLAUDE.md section 1 says the code has to be
  explainable line by line under observation.
- C. Visit every `exp.Select` in the tree, and for each, read **its own argument
  values** for `From` and `Join` nodes by type rather than by key name.
**Chosen:** C. Every nested SELECT is visited in its own right, so each gets a
predicate on its own WHERE — which is what makes subqueries, derived tables and
CTE bodies safe without any special-casing. Matching on node *type* rather than
argument *name* means a future sqlglot rename cannot silently disable the guard,
which is the failure mode that actually happened.
**Trade-off accepted:** the traversal is slightly more code than `traverse_scope`
would be, and it re-derives something the library already knows. Accepted because
the failure mode of the library approach is opaque and the failure mode of this one
is a test going red. Also: `UNION` is rejected outright rather than handled, since
the root node is not a `Select` — conservative, and logged as OPEN_QUESTIONS Q-009.
**Where it lives:** `src/db/guard.py:SqlGuard._inject_tenant_predicates` and
`_direct_sources`.

---

### D-006 · The schema card is introspected, not the provided SCHEMA.md
**Date:** 2026-08-28
**Context:** The text-to-SQL prompt needs a schema. `data/SCHEMA.md` is 3.4KB and
would fit whole, so pruning is not the issue — accuracy is. Recon found it
documents `shifts.status` as three values where the data has one, and
`customers.status` as two where the data has one (DQ-3).
**Options considered:**
- A. Paste `SCHEMA.md`. One line of code, and it teaches the model literals that
  do not exist. A filter on `status = 'inactive'` then correctly returns zero rows,
  which is indistinguishable from a bug at the UI.
- B. Introspect structure only (`PRAGMA table_info`). Accurate but drops the enum
  literals, which are the single most useful thing in the prompt for a text-to-SQL
  task.
- C. Introspect structure **and** observed distinct values for low-cardinality text
  columns, plus a short block of facts the schema cannot express.
**Chosen:** C, at 3.1KB rendered. The facts block carries the four things recon
proved a model cannot infer and will otherwise get wrong: that `customers` means
end-customers, that `created_at` is a constant, that `tank_readings` fans out 9x,
and that `gallons_delivered` is NULL for non-completed orders.
**Trade-off accepted:** the card costs six introspection queries per cold start
(cached thereafter), and the facts block is hand-written prose that can drift from
the data. Mitigated by generating everything around it, so only the four
hand-written facts are unverified.
**Where it lives:** `src/db/schema.py:introspect` and `SchemaCard.render`.

### D-007 · Two LLM calls, split so the one that writes prose cannot compute
**Date:** 2026-08-29
**Context:** The obvious build is one call: give the model the schema and the
question, let it answer. The cheaper-looking variant is one call that returns SQL
*and* a templated answer. Both put the model in a position to state a number.
**Options considered:**
- A. One call, model answers directly from the schema. It has no data, so it
  hallucinates figures that look exactly like real ones.
- B. One call returning SQL plus a prose template we fill in. No hallucinated
  figures, but the phrasing is fixed and reads badly for the eight very different
  questions — and it cannot say "no rows, because the data stops in May".
- C. Two calls. The first turns a question into SQL and is the only creative step.
  The second receives the *executed rows* and is told to use them exactly and
  invent nothing.
**Chosen:** C. The property worth paying a second call for is that no figure in
any answer originates in the model — every number travelled from SQLite through
the executor into the synthesis prompt. `test_the_synthesis_call_receives_rows_and_the_anchor_not_the_question_alone`
pins that the rows actually arrive.
**Trade-off accepted:** two round trips per question, which matters on the voice
path (D-001's anchor sentence and this latency are the two things Step 5 will have
to work around). Roughly doubles per-question token cost, which the DECISIONS cost
model will need to reflect. A single structured call could return SQL and a
narrative skeleton together, and is worth revisiting if voice latency is bad.
**Where it lives:** `src/agent/sql_agent.py:SqlAgent.answer` and `_synthesise`.

---

### D-008 · Cross-tenant refusal is checked twice, from two different inputs
**Date:** 2026-08-29
**Context:** A tenant-scoped session must refuse "which tenant delivered the most
gallons?" rather than answer it for one tenant. At runtime there is no question
number to look up — `TenantContext.allows_question` works for the test suite and
not for free text — so the intent has to be detected.
**Options considered:**
- A. Keyword heuristics on the question ("all tenants", "by tenant"). Cheap, and
  brittle in exactly the phrasings a real user produces.
- B. Ask the model, in the generation call it is already making, for an
  `is_cross_tenant` flag. Free, and trusts an untrusted component with an
  authority decision.
- C. Inspect the generated SQL: grouping or ordering by `tenant_id` means the
  query is shaped to return one row per tenant. Deterministic, but blind to intent
  the SQL does not express.
- D. B **and** C, either one sufficient to refuse.
**Chosen:** D. They fail differently, which is the point: the flag reads the
*question* and catches "how do we compare to the others" that the model then
writes as a single-tenant query; the AST check reads the *SQL* and catches a
mislabelled or adversarial generation. Requiring both to agree would mean either
one being wrong lets the query through, so it is either-fires-refuses.
**Trade-off accepted:** false refusals are now possible from two directions — a
model that flags a legitimate single-tenant question, or a query that orders by
`tenant_id` incidentally. In a SOC 2 multi-tenant system a spurious refusal is the
right direction to be wrong in, but it is a real cost to conversational quality.
Note also that `SELECT tenant_id` is deliberately *not* treated as cross-tenant —
echoing the tenant back is normal — so the check keys on `GROUP BY` and `ORDER BY`
only.
**Where it lives:** `src/agent/sql_agent.py:_authority_check` and
`_looks_cross_tenant`.

---

### D-009 · The answer carries the date window as data, not only as prose
**Date:** 2026-08-29
**Context:** Resolves OPEN_QUESTIONS Q-007. D-001 anchors relative windows on
`MAX(delivery_date)` and has the agent say so in its reply. That is enough for a
human reading a terminal and not enough for anything else.
**Options considered:**
- A. Prose only. Simplest. A downstream consumer has to parse English to discover
  the numbers are 91 days old, and voice has no way to say the anchor on the first
  answer of a session and stay quiet after.
- B. Structured fields on the response (`date_anchor`, `anchor_mode`) alongside
  the prose.
**Chosen:** B, decided rather than escalated: extra fields are free to ignore and
expensive to retrofit once the router, the CLI and the voice transport all consume
the response type.
**Trade-off accepted:** `SqlAnswer` grows fields that nothing reads yet, which is
the speculative-generality smell — accepted narrowly because the staleness caveat
is a correctness property of every relative-window answer this system gives, not a
feature someone might want later.
**Where it lives:** `src/agent/sql_agent.py:SqlAnswer.date_anchor` / `.anchor_mode`.

### D-010 · Escalation is additive points in Python, not a judgement by the model
**Date:** 2026-08-29
**Context:** The assignment asks for an escalation recommendation that weighs
health score, CARR and contract proximity "not just ticket priority". The
mechanism was contested: the LLM has all five sources in front of it already and
could simply be asked.
**Options considered:**
- A. Ask the LLM for the level. Free, reads well, and gives a different answer on
  Tuesday. "Why was this escalated?" becomes unanswerable, which is fatal in a
  live session and worse in production.
- B. A decision tree / rule cascade. Deterministic and explainable, but the first
  branch dominates: any tree ordered on health misses t2 Heartland (health 45,
  contract expiring tomorrow, 72k CARR, competitor named on a renewal call) —
  the most time-critical account on the roster.
- C. Additive weighted signals, bucketed into four levels, each signal carrying
  the sentence the brief prints.
**Chosen:** C. Additive is what lets independent moderate signals compose into a
high level, which is exactly the t2 case: no single signal fires critical, five
moderate ones sum to 73. The weights order is the actual argument and it lives in
`config.py` where a FleetPanda employee can dispute it.
`test_no_single_signal_reaches_critical` pins that no weight can quietly grow
past the threshold on its own.
**Trade-off accepted:** the point scale is arbitrary — only the ordering is
defensible, and calibration came from eyeballing all 85 tickets rather than from
outcome data, which is the honest ceiling on any cold-start scorer. Also, account-
level signals dominate ticket-level ones, so all 12 of tenant 4's tickets score
CRITICAL and the level cannot rank them against each other; the raw `score` still
can. Logged as OPEN_QUESTIONS Q-013.
**Where it lives:** `src/agent/escalation.py:score_ticket`, weights in
`src/config.py`.

---

### D-011 · `today` is injected; the escalation clock is not the data clock
**Date:** 2026-08-29
**Context:** D-001 established that operational questions anchor on
`MAX(delivery_date)` because the data stops on 2026-05-29. It is tempting to apply
that anchor everywhere for consistency.
**Options considered:**
- A. Anchor contract proximity on the data too. Consistent, and wrong: it would
  report t2's contract as expiring in 93 days when it expires tomorrow.
- B. Use `date.today()` directly. Correct, and makes every test time-dependent —
  the suite would start failing in September for no reason.
- C. Use the real calendar, injected as a parameter defaulting to `date.today()`.
**Chosen:** C. Contract end dates are forward-looking CRM facts, not operational
history, so they do not move with the fixture's staleness. Two different clocks in
one system is a thing worth being explicit about rather than accidentally
consistent on.
**Trade-off accepted:** a reader has to notice that `volume_change_pct` is
data-anchored while `today` is calendar-anchored in the same function. Called out
in the docstring, and `test_contract_signal_moves_with_the_injected_date` makes
the dependency visible.
**Where it lives:** `src/agent/escalation.py:score_ticket`, `today` parameter.

### D-012 · Account risk raises the floor; only the ticket can reach CRITICAL
**Date:** 2026-08-29
**Context:** D-010's additive scorer worked at the roster level (criticals landed
exactly on t2, t4 and t8) and failed at the account level: account signals reach 95
points where ticket signals reach 35, so all twelve of tenant 4's tickets scored
CRITICAL and the level could no longer rank them. A triage queue that says
"everything from this account is the most urgent thing" has stopped triaging.
**Options considered:**
- A. Leave it and sort queues on the raw score. The score does still rank them
  (t4 spanned 100-135), but the level is the thing a human reads, and a level that
  is constant across an account is noise in the brief.
- B. Reweight ticket signals upward. Moves the problem: it would make a duplicate
  filing from a healthy account outrank a genuine crisis at a failing one.
- C. Cap the account-state portion of the score, so account state sets a floor and
  ticket-level signals decide what clears the top threshold.
**Chosen:** C, capping at `ESCALATION_URGENT + 10`. The rule is now stateable in
one sentence: *a bad account is URGENT on its own; CRITICAL additionally requires
something about this ticket.*
**Trade-off accepted:** the first cap tried was `ESCALATION_CRITICAL - 1`, which
was almost useless — one point of headroom meant a lone "filed as high" (5 points)
still promoted everything, and 11 of tenant 4's 12 stayed CRITICAL. The working
value leaves 15 points of headroom, which is a tuned number and therefore a soft
spot; it is named in `config.py` with that reasoning. Also, `score` is no longer a
plain sum of its signals, so the audit trail now needs `account_risk`,
`ticket_risk` and `account_risk_capped` to stay reconstructable — which the
assessment carries and a test verifies.
**Result:** roster distribution moved from 32 critical / 13 urgent to 16 critical /
29 urgent, and tenant 4's twelve tickets now spread across URGENT and CRITICAL with
the TankLink duplicate cluster correctly at the top.
**Where it lives:** `src/agent/escalation.py:score_ticket`,
`src/config.py:MAX_ACCOUNT_RISK_POINTS` and `ACCOUNT_LEVEL_SIGNALS`.

### D-013 · KB retrieval is a join with a tie-break, not a vector search
**Date:** 2026-08-29
**Context:** The assignment asks for "relevant KB articles ranked by relevance and
recency", and the reflex answer is embeddings plus a vector store. The corpus is
**twelve articles**.
**Options considered:**
- A. ChromaDB + `sentence-transformers`. A ~90MB model download, a torch
  dependency, an index to build and explain, to rank twelve documents. It also
  invites "why did you add a vector database for twelve articles?" in the
  architecture discussion, and there is no good answer.
- B. Embeddings without the store -- numpy cosine over twelve vectors. Cheaper,
  still needs an embedding model and an API call per ticket.
- C. `product_area` equality for relevance, symptom-token overlap as the
  tie-break, `updated_at` to break remaining ties.
**Chosen:** C. `product_area` is a literal that tickets and articles genuinely
share -- unlike `product_area` vs `modules_active`, which needed D-002's mapping --
so this is a join, not a semantic-similarity problem. It picks KB-003 (TankLink
connectivity) for the TankLink ticket, which is the right answer, with no model and
no network call.
**Trade-off accepted:** it cannot match a paraphrase that shares no words and no
area -- a ticket saying "gauge readings frozen" would miss a tank_monitor article
about "readings not updating" if the areas differed. At twelve articles that gap is
inspectable; at several hundred it would not be, and embeddings become the right
answer. The threshold to revisit is roughly when the KB stops fitting in one
prompt.
**Where it lives:** `src/agent/triage_agent.py:find_kb_articles`, weights in
`src/config.py`.

---

### D-014 · The operational snapshot uses fixed SQL, not the SQL agent
**Date:** 2026-08-29
**Context:** The triage brief needs four dispatch numbers (completed last 30, prior
30, emergency count, open orders). `sql_agent` exists and could answer them from
natural language.
**Options considered:**
- A. Route them through `sql_agent`. Reuses the pipeline, and spends four LLM
  round trips per brief on four questions that never change, each with its own
  chance of generating something the guard rejects.
- B. Query the database directly, bypassing the guard. Fastest, and puts an
  unguarded query path in a system whose entire claim is that there isn't one.
- C. Fixed SQL strings, run through the same `QueryExecutor` and guard with a
  `TenantContext.for_tenant(...)`.
**Chosen:** C. These are known questions, not natural-language ones, so the LLM
adds latency, cost and a failure mode for no gain. Routing them through the guard
anyway means the tenant predicate is injected exactly as it would be for a typed
question -- the snapshot is not a privileged path.
**Trade-off accepted:** four hand-written queries now live in the triage agent
rather than in the db layer, so a schema change touches two files. Accepted because
moving them to `db/` would create a module whose only job is to hold four strings.
Also noted while writing it: a single-window "past 30 days" reads inclusively
(`>=`) while two adjacent windows must not both claim the boundary day (`>` and
`<=`). Using `>` for the emergency count quietly returned 15 where the graded
question Q5 asserts 17 — two numbers in one system disagreeing. Now pinned by
`test_the_snapshot_agrees_with_the_graded_question`.
**Where it lives:** `src/agent/triage_agent.py:TriageAgent.operational_snapshot`.

### D-015 · `triage_agent.py` split at the prompt boundary, not at a class boundary
**Date:** 2026-08-29
**Context:** `triage_agent.py` reached 367 lines, over the ~350 ceiling in
CLAUDE.md section 6, which asks for a split "along a real seam" and a note here.
**Options considered:**
- A. Extract `find_kb_articles` into `src/agent/kb_match.py`. A genuine concern
  boundary, but it invents a file CLAUDE.md section 4 does not list, and KB
  matching is 25 lines -- a file per function is how a four-file layer becomes a
  twelve-file one.
- B. Extract the context-pack dataclasses. They are the module's vocabulary; moving
  them makes the file shorter and harder to read in one pass.
- C. Move `build_triage_payload` into `llm/prompts.py`.
**Chosen:** C. What the model is shown is as much a prompt decision as the system
message above it, and `prompts.py` already exists so that every prompt decision is
readable in one place during a walkthrough. The seam is real rather than
size-driven: the payload builder's only job is to decide what the model sees.
**Trade-off accepted:** `prompts.py` now imports two agent types, which is a
direction the layering otherwise avoids -- kept under `TYPE_CHECKING` so there is
no runtime cycle, but it is a wart. 311 and 180 lines respectively.
**Where it lives:** `src/llm/prompts.py:build_triage_payload`.

---

## Data quality observations

Anomalies found during Step 0 recon. Each names the check that surfaced it and what a
production system would owe it.

**DQ-1 · The dataset is 91 days stale relative to system time.**
`MAX(delivery_date) = 2026-05-29` against a run date of 2026-08-28. Surfaced by section C of
the DB recon (min/max over every date column). *Production:* a freshness check at load that
refuses to serve, or loudly degrades, when the newest operational row is older than an SLA —
silently answering "0 deliveries last week" is worse than erroring.

**DQ-2 · `delivery_orders.created_at` is a single constant for all 9,769 rows.**
`2026-05-29 10:59:10` everywhere — the fixture's generation timestamp masquerading as a
business column. Surfaced by the same min/max sweep (min == max). *Production:* drop or rename
the column. It will otherwise be used for date arithmetic by a model that has no way to know it
is fake, and it defeats any incremental-load or partitioning scheme built on it.

**DQ-3 · `SCHEMA.md` documents enum values that never occur.**
`shifts.status` is documented as three values and is `'completed'` in all 5,502 rows;
`customers.status` is documented as two and is `'active'` in all 114. Surfaced by
`GROUP BY` on each enum-like column. *Production:* generate the schema card from introspection
plus observed distinct values. A prompt that advertises a nonexistent literal invites filters
that correctly return nothing, which is indistinguishable from a bug at the UI.

**DQ-4 · `gallons_delivered` is 29.87% null, and the nulls are load-bearing.**
2,918 nulls = exactly the count of non-completed orders (990 + 980 + 948); zero completed
orders are null. Surfaced by a null-rate sweep cross-checked against the status histogram.
*Production:* this one is correct as designed, but it is a trap for aggregates — a fill-rate
query that omits `status='completed'` drops 30% of the numerator while keeping the denominator
and returns ~0.65 where the truth is ~0.92, with no error and no null in the output.

**DQ-5 · Two vocabularies describe the same product surface.**
Tickets use `product_area`, customers use `modules_active`, and they share only 3 of 9 literals
(RECON.md §8). Surfaced by set-differencing the two columns. *Production:* one canonical
capability taxonomy with the ticket form constrained to it; failing that, an owned mapping
table with a test that fails when either vocabulary gains a term the map does not cover.

**DQ-6 · `tank_readings` covers only 6 of 12 tenants — and that is correct.**
Tenants `[1, 3, 6, 9, 10, 12]`, which is *exactly* the set with `tank_monitor` in
`modules_active`. Surfaced by set-comparing DB tenant coverage against the CRM entitlement
list. Worth recording because it looks like missing data and is not: it is entitlement showing
through to the operational tables, and it makes `tank_readings` a usable second signal for
module-mismatch detection.

**DQ-7 · 37 of 85 tickets have a null `resolution`, and `closed` does not imply resolved.**
Tenant 4's `TankLink` cluster contains a ticket closed on 2026-04-24 and refiled twice after
(RECON.md §9). Surfaced by grouping identical subjects within a tenant. *Production:* duplicate
detection must not treat `status='closed'` as terminal, and reopen-rate is a churn signal worth
computing.

**DQ-8 · No indexes exist on any table, including `tenant_id`.**
Surfaced by `PRAGMA index_list` over all six tables. Harmless at 9,769 rows; it is the first
thing that breaks at the 150-tenant / 500K-orders scale the assignment asks about.

**DQ-9 · `customers.fleet_size` is 100% NULL.**
Surfaced by the null-rate pass in `schema.py` introspection, not by the original
recon sweep — the column was not on the list of columns the eight questions touch.
`SCHEMA.md` documents it as "Nullable" without saying it is always null.
*Production:* drop the column or backfill it. As it stands it is an attractive
nuisance: a question like "which end-customers have the largest fleets?" produces
valid SQL, no error, and an empty answer.

---

## Cost estimate

**Workload:** 50 ticket triages/day and 100 dispatch questions/day, as specified.
**Method:** measured, not guessed — prompt sizes come from `build_sql_prompt()` and
`build_triage_payload()` on real ticket #1083, converted at ~3.7 chars/token for
this JSON-and-prose mix. Rerun with `messages.count_tokens` once a key exists;
expect the estimate to move by 10–15%, not by a multiple.

### Per-call token math

A dispatch question costs **two** calls (D-007: the model that writes prose is
never in a position to compute a number).

| Call | Input | Output |
|---|---:|---:|
| SQL generation — system prompt with schema card 1,165 + question ~15 | 1,180 | 120 |
| Synthesis — system prompt 184 + rows payload ~92 | 276 | 90 |
| **Per question** | **1,456** | **210** |

A ticket triage costs **one** call. Everything deterministic — the five-source
gather, escalation scoring, KB matching — is computed in Python first, so the model
receives a finished context pack and writes three prose sections.

| Call | Input | Output |
|---|---:|---:|
| Narration — system prompt 196 + context pack 1,144 | 1,340 | 350 |

### Daily totals

| | Input | Output |
|---|---:|---:|
| 100 questions | 145,600 | 21,000 |
| 50 triages | 67,000 | 17,500 |
| **Total/day** | **212,600** | **38,500** |

### Cost at current list prices

| Model | $/day | $/month | $/year |
|---|---:|---:|---:|
| Claude Opus 5 ($5 / $25 per MTok) | $2.03 | $60.77 | $739 |
| Claude Sonnet 5 ($2 / $10) | $0.81 | $24.31 | $296 |
| Claude Haiku 4.5 ($1 / $5) | $0.41 | $12.15 | $148 |

**Roughly $61/month on Opus 5** — against 12 tenants paying $30k–$96k CARR each
(~$756k total), this is 0.008% of revenue. Cost is not the constraint at this
scale; correctness is. That is the actual argument for defaulting to the strongest
model rather than the cheapest.

### The cheapest available optimisation

The 1,165-token SQL system prompt is **byte-identical on all 100 questions per
day** — it is the introspected schema card, which changes only when the schema
does. Prompt caching it (write at ~1.25×, reads at ~0.1×) drops input from 212,600
to ~109,090 tokens/day: **$2.03 → $1.51/day, 26% cheaper**, one `cache_control`
parameter. It also cuts latency on the voice path, which matters more.

Two caveats: the minimum cacheable prefix is ~1024 tokens and the card is 1,165, so
it only just qualifies — trimming the card would silently disable caching. And the
card must be rendered identically every call; introspection is already `lru_cache`d,
so this holds today.

### What this model does not include

STT/TTS for voice mode (unpriced — `faster-whisper` and `edge-tts` run locally, so
the cost is compute, not API), retries (~1 in N questions triggers the guard-
rejection retry, adding one generation call), and the intent classifier, which is
free in the common case because `Router.classify` resolves unambiguous input with
heuristics and only calls the model when genuinely stuck.

---

## Scaling: 150 tenants, 500K+ delivery orders each

That is **75M+ rows** in `delivery_orders` against 9,769 today — a 7,700× increase.

### What breaks, in the order it breaks

1. **The absence of indexes.** There is not one index on any table, including on
   `tenant_id` (RECON.md §12). Every query is a full scan. At 9,769 rows that is
   sub-millisecond and invisible; at 75M it is the whole problem, and it arrives
   before anything else on this list.

2. **The date anchor becomes the second full scan.** D-001 anchors every relative
   window on `(SELECT MAX(delivery_date) FROM delivery_orders)`. That is a
   correctness fix that costs a full-table aggregate *per query*, and at 75M rows
   it doubles the damage from (1). Fix: materialise the anchor (a one-row
   `data_freshness` table, or a cached value refreshed on ingest) rather than
   recomputing it. This is the clearest example of a decision that is right at
   fixture scale and wrong at production scale.

3. **Schema-card introspection at cold start.** `src/db/schema.py` runs `COUNT(*)`
   per table plus a null-count aggregate *per column* — around 40 full scans. It is
   `lru_cache`d to once per process, but that is minutes of cold start on 75M rows,
   and it will look like a hung deploy. Fix: read `pg_stats`/`pg_class` estimates
   instead of exact counts; nothing in the prompt needs an exact row count.

4. **SQLite itself.** One writer, no network access, no roles, no row-level
   security. The isolation guarantee below cannot be built on it. Postgres.

5. **In-memory JSON loading.** `Repository` loads every ticket and transcript into
   the process and indexes them by tenant on first access. 95KB today; at 150
   tenants with proportional history it is a database table, not a file. The
   `DataSource` protocol survives this — `load()` becomes a query — but
   `_index_by_tenant` (which indexes the *whole corpus* to serve one tenant) has to
   become a `WHERE tenant_id = ?`.

6. **The O(n²) bits.** `find_duplicates` compares a ticket against every other
   ticket for that tenant with `token_set_ratio`. Fine at ~7 tickets/tenant;
   quadratic in tickets per tenant. Fix: block on `product_area` first, then trigram
   index (`pg_trgm`) for the similarity search. `find_kb_articles` scans all
   articles linearly — fine at 12, and the point at which it stops being fine is
   also the point at which D-013's "no vector store" decision should be revisited.

### Enforcing tenant isolation at the database level

Application-layer AST rewriting is the right control for *this* build — it is
testable, portable, and it caught real bugs. At 150 tenants it should become a
second line of defence rather than the only one, because it depends on `sqlglot`
parsing SQLite exactly as SQLite executes it (SECURITY.md, residual risk).

Concretely, on Postgres:

```sql
ALTER TABLE delivery_orders ENABLE ROW LEVEL SECURITY;
ALTER TABLE delivery_orders FORCE  ROW LEVEL SECURITY;

CREATE POLICY tenant_isolation ON delivery_orders
  USING (tenant_id = current_setting('app.tenant_id')::int);
```

The details that actually decide whether this works:

- **`FORCE` is not optional.** Without it, RLS is bypassed by the table owner. An
  app connecting as the owner — the default in most deployments — gets no isolation
  at all and no error saying so.
- **The application role must be `NOBYPASSRLS` and must not own the tables.**
  Superuser and `BYPASSRLS` roles ignore policies entirely. Migrations run as owner;
  the app never does.
- **`SET LOCAL`, never `SET`.** This is the one that bites. `SET app.tenant_id`
  persists for the *session*, and every production deployment puts a connection
  pooler in front of Postgres. Under PgBouncer in transaction mode, the next request
  to borrow that connection inherits the previous request's tenant — a cross-tenant
  leak that appears only under load and is invisible in a single-tenant test.
  `SET LOCAL` scopes it to the transaction, which is exactly the pooler's unit of
  reuse. Every query must therefore run inside an explicit transaction.
- **The policy needs an index to be usable.** RLS appends a predicate; it does not
  make it fast. Composite indexes leading with `tenant_id`
  (`(tenant_id, delivery_date)`, `(tenant_id, status, product_type)`) serve both the
  policy and the eight questions.
- **Partition the largest tables by `tenant_id`** (hash for even spread, list if a
  few tenants dominate). This converts isolation into partition pruning — the
  planner stops reading other tenants' data rather than filtering it — and makes
  per-tenant retention and restore a partition operation.
- **Keep the AST guard anyway.** It enforces things RLS does not: the table
  allowlist, the forced `LIMIT`, the refusal of `PRAGMA`/`ATTACH`/DDL, and the
  cross-tenant *question* refusal, which is a policy decision about scope rather
  than a row filter. It is also the only layer covering the JSON sources, where
  `Router` already has to make the same call for ticket triage.

Belt and braces at the boundary: `_assert_no_foreign_tenant` stays, and in Postgres
it can be strengthened cheaply — set the session's `app.tenant_id` and have a
`SECURITY DEFINER` health check verify a known-foreign row is invisible, as a
canary run at deploy rather than per query.

### Adding a data source without modifying agent code

This is what `src/data/sources.py` exists for. A source implements three things —
its name, how to load, and how to find a record's tenant — and is added with one
line in `REGISTRY`. `Repository._index_by_tenant` iterates the registry, so tenant
filtering applies to a new source the moment it is registered, with no change to
the filtering code. `ResolvedNameSource` proves the seam is real: call transcripts
carry a tenant *name* rather than an id, and that difference is contained entirely
within the source class.

Honest limits, because "no agent changes" is not the whole truth:

- **The data layer is genuinely free.** `records_for("new_source", tenant_id)` works
  immediately.
- **The triage brief is not.** `TicketContext` names its five sources as fields, so
  a sixth needs a field there and a line in `build_triage_payload`. That is two
  small edits in known places rather than a refactor, but it is not zero.
- **Retrieval strategy is per-source.** KB articles are ranked by product area;
  a new source needs its own relevance rule.

If a sixth source were a hard requirement, the fix is to make `TicketContext` hold
`dict[str, tuple[Any, ...]]` keyed by source name instead of five named fields —
which trades the type safety and readability that make the current version
explainable. At five sources, named fields are the better trade; the registry is
where the extensibility that actually matters already lives.

---

## The end-customer agent: two layers of tenant isolation

Today there is one boundary: FleetPanda serves 12 tenants, and a session is either
scoped to one or is internal. Serving end-customers — a homeowner asking "when is
my next delivery?" — adds a second boundary *inside* each tenant, and the two are
not symmetrical.

### How the scoping changes

`TenantContext` grows a third scope rather than a parallel system:

```python
class SessionScope(Enum):
    PLATFORM      = "platform"        # FleetPanda internal
    TENANT        = "tenant"          # a fuel company's staff
    END_CUSTOMER  = "end_customer"    # a fuel company's customer
```

The guard already takes its predicate from the context rather than from the caller,
so an end-customer scope injects **two** predicates on every scoped table —
`tenant_id = T AND customer_id = C` — through the same `_inject_tenant_predicates`
traversal. That is the payoff from D-005: the mechanism does not change, only what
it injects.

But a second predicate is not sufficient, for two reasons.

### The table allowlist must shrink, not just the rows

`customer_id` exists on `delivery_orders`, `customers` and `tank_readings`. It does
**not** exist on `drivers`, `trucks` or `shifts` — those are the tenant's
operations, not any customer's. Injecting `customer_id` there is impossible, and
injecting only `tenant_id` would expose Cascade's entire driver roster and fleet to
one of Cascade's customers.

So `END_CUSTOMER` scope needs its own allowlist — `delivery_orders`, `customers`,
`tank_readings` — and a join to `drivers` must be refused rather than filtered. The
current guard has one allowlist for all scopes; making it scope-dependent is a
small change (`SqlGuard.__init__` already takes `allowed_tables`) and a large
security property.

### What an end-customer may and may not see

| May see | Must not see |
|---|---|
| Their own orders: date, status, gallons, address | Any other end-customer of the same tenant |
| Their own tank readings and days-to-empty | Driver names, truck labels, shift schedules |
| Their next scheduled delivery | What the tenant pays FleetPanda, or the tenant's health score |
| Their own order history and volumes | Per-gallon pricing paid by other customers |
| | Anything at all about the other 149 tenants |

The subtle one is **aggregates**. "What's the average delivery size?" is a
reasonable CSM question and a disclosure when a homeowner asks it — with
`customer_id` injected it covers only their own rows, which is correct, but the
failure mode if the second predicate is ever missed is an answer that looks
plausible and is built from their neighbours' data. This is why the post-execution
assertion must also check `customer_id`, not just `tenant_id`.

### Identity is the hard part, and it is harder over voice

Tenant resolution can be fuzzy because a rep saying "Cascade" is asking about a
company they already have authority over — the resolver's job there is convenience,
and D-003 makes it fail closed on ambiguity anyway.

**End-customer resolution must never be fuzzy.** A homeowner calling their fuel
company cannot be identified by name matching: names collide, speech-to-text
mangles them, and a wrong match discloses a stranger's address and delivery
schedule to whoever is on the phone. Concretely:

- Identify from the channel, not the conversation — ANI/verified caller ID, an
  authenticated portal session, or a signed link from an email.
- Where that is unavailable, require an account number **plus** a second factor the
  caller must supply rather than confirm. Never read back a candidate ("I have you
  at 14 Elm Street — is that right?"), because that discloses the data the check was
  meant to protect.
- Fail closed to a human. An unidentified end-customer gets transferred, not
  guessed at. `ResolutionResult.needs_confirmation` already models this distinction;
  for end-customers the threshold is simply "exact or nothing".

### Two more things that are not data scoping

- **The tenant owns the relationship, not FleetPanda.** The agent answers as
  Cascade Fuel Services. Branding, tone, escalation paths, what it may promise
  about a delivery, and data-retention policy are all per-tenant configuration —
  which means a `tenant_id` on the *prompt*, not just on the query.
- **The blast radius is different.** A bug in the CSM agent shows a FleetPanda
  employee the wrong internal data. A bug in the end-customer agent shows a member
  of the public another member of the public's home address. That asymmetry argues
  for shipping end-customer access on a deliberately narrow surface — a handful of
  templated intents ("next delivery", "tank level", "recent orders") backed by
  fixed, reviewed queries — rather than by pointing general text-to-SQL at it. The
  text-to-SQL path is the right tool for a trusted internal user exploring data; it
  is more machinery than the question deserves when the question is always one of
  four things and the cost of being wrong is highest.
