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
