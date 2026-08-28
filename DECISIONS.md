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
