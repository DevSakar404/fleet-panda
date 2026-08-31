# Spec — Ticket Triage Agent

← [README](../../README.md) · [Architecture decisions](../explanation/architecture-decisions.md) · Sibling specs: [tenant isolation](tenant-isolation.md) · [SQL agent](sql-agent.md) · [entity resolution](entity-resolution.md) · [voice interface](voice-interface.md)

**Status:** implemented and tested (`tests/test_triage.py`,
`tests/test_escalation.py`, `tests/test_ticket_parser.py`). Triage is invoked
either by ticket id or by pasting a ticket body into the chat.

---

## 1. Contract

```
Router._triage(text, context)
  ├── extract_ticket_id(text)          "triage 1083" / "#1083" / a bare 4-digit line
  │     → visibility check (§8)        a scoped session sees only its own tickets
  │     → TriageAgent.build_brief(ticket)
  │
  └── no id → _triage_pasted(text, context)                            (D-022)
        → looks_like_a_ticket(text)    a labelled line, or two non-empty lines
        → context.is_bound?            unscoped sessions are asked to scope first
        → parse_pasted_ticket(text, tenant_id, tenant_name)
        → TriageAgent.build_brief(ticket)
```

**Input:** a `Ticket` — either loaded from `tickets.json` by id, or parsed from a
pasted body. The parsed form takes its tenant from the bound session and never
from the pasted text; see §8.
**Output:** a `TicketBrief` — a frozen dataclass with a deterministic half
(`context: TicketContext`, `assessment: EscalationAssessment`) and a narrative
half (`summary`, `escalation_reasoning`, `suggested_response`).

Without an LLM configured, `build_brief` returns the deterministic half alone —
the full escalation decision and all five sources are available with no API key.

---

## 2. Pipeline

`src/agent/triage_agent.py`. Three stages; the first two never call a model.

```
ticket
  ├── gather()   → TicketContext          five sources, tenant-scoped, deterministic
  ├── score()    → EscalationAssessment   pure function, no I/O beyond the in-memory repo
  └── narrate()  → 3 prose sections       one LLM call; given the level, not asked for it
```

Everything decidable is decided before the model runs. "Why was this escalated?"
is answerable from `assessment.signals` — a tuple of `(name, points, sentence)` —
without re-running anything.

---

## 3. The five sources

All retrieved through the one `Repository`, which applies tenant scoping centrally
(see [tenant isolation spec §10](tenant-isolation.md)). Every section
**degrades to empty rather than failing** — 6 of 12 tenants have no
`tank_readings`, `billing` tickets have no KB article, 37 of 85 tickets have a
null resolution. `TicketContext.missing_sources` names what came back empty, so
"no article matched" is distinguishable from "we did not look".

| # | Source | File | Retrieval | Cap |
|---|---|---|---|---|
| 1 | Customer profile ("CRM") | `customers.json` | `repository.get_tenant(tenant_id)` — health score, CARR, `modules_active`, contract dates, assigned CSM, onboarding status | 1 |
| 2 | Past tickets | `tickets.json` | this tenant's tickets, excluding the subject ticket | `BRIEF_MAX_PAST_TICKETS = 5` |
| 3 | Duplicates | `tickets.json` | `find_duplicates` — same tenant, `rapidfuzz.token_set_ratio(subject) ≥ 85`, oldest first; **status ignored** (a closed-then-refiled cluster must not read as a first occurrence — DQ-7) | — |
| 4 | Call history | `call_transcripts.json` | `repository.transcripts_for(tenant_id)` — keyed by tenant *name* in the file, resolved to id at load time | `BRIEF_MAX_CALLS = 3` |
| 5 | Knowledge base | `knowledge_base.json` | `find_kb_articles` — scored join, see §4 | `KB_MAX_ARTICLES = 3` |
| — | Operational snapshot | `dispatch.db` | four fixed tenant-scoped queries, see §5 | — |

### "CRM integration" note

The customer-profile fields (health score, CARR, contract end date, CSM) are read
from `customers.json`. In a production deployment this is the seam where a sync
from a CRM / customer-success platform (Salesforce, HubSpot, Vitally, …) would
land — the triage agent depends only on the `Tenant` dataclass shape, not on the
file. Contract proximity is scored on the **real calendar**, not the dataset
clock (D-011), precisely because these are forward-looking CRM facts.

---

## 4. Knowledge-base retrieval

`find_kb_articles(ticket, articles)`. **No embeddings, no vector store** (D-013) —
12 articles, and `product_area` is a literal both tickets and articles carry, so
this is a scored join with a tie-break.

| Component | Points | Source |
|---|---|---|
| `article.product_area == ticket.product_area` | `KB_AREA_MATCH_POINTS = 10` | exact match on the shared vocabulary |
| Each article symptom whose tokens overlap the ticket's | `KB_SYMPTOM_MATCH_POINTS = 4` each | token set intersection, stopwords removed |
| Article title tokens overlap the ticket's | `KB_TITLE_MATCH_POINTS = 2` | token set intersection |

- **Floor:** an article must score `≥ KB_MIN_SCORE = 10` to appear at all. A
  `product_area` match (10) clears it outright; without one, an article needs
  enough symptom/title overlap to reach 10 (two symptom matches plus a
  title-token match, or three symptom matches). Below the floor the agent returns
  nothing, which is the honest answer for `billing` (no KB coverage). Before the
  floor existed, ticket #1048 "Invoice shows wrong gallon count" was served
  KB-011 "Tank monitor alert threshold configuration" because both mention
  gallons.
- **Tie-break:** recency (`updated_at`), so a refreshed article outranks a stale
  one of equal relevance.
- **Return:** top 3.

---

## 5. Operational snapshot

`TriageAgent.operational_snapshot(tenant_id)`. Four **fixed** queries, not routed
through the SQL agent (D-014) — these are known questions, not natural-language
ones, so an LLM round trip would add a failure mode and a cost for no gain. They
still run through `SqlGuard` + `QueryExecutor` with
`TenantContext.for_tenant(tenant_id)`, so the tenant predicate is injected exactly
as it would be for a typed question.

| Field | Query |
|---|---|
| `completed_last_30` | completed orders, `delivery_date > date(MAX(delivery_date), '-30 day')` |
| `completed_prior_30` | completed orders in the 30 days before that window |
| `emergency_last_30` | `priority = 'emergency'`, `order_date >= date(MAX(delivery_date), '-30 day')` |
| `open_orders` | `status IN ('pending', 'in_progress')` |

Windows anchor on `(SELECT MAX(delivery_date) FROM delivery_orders)` — the data
ends 2026-05-29, so a `now()`-relative window reports every tenant as having
stopped delivering (D-001). `volume_change_pct` is `None` (not `0.0`) when there
is no prior window — "no baseline" and "flat" are different, and only one should
produce a decline signal.

> **Convention note:** the `emergency_last_30` window uses `>=` while the paired
> completed-volume windows use `>`. Two adjacent windows must not both claim the
> boundary day (double-count); a single "past 30 days" window reads inclusively.
> The inclusive form is what `tests/test_sql_questions.py` asserts for graded
> Q5 (17 emergency orders for tenant 4); `>` here silently produced 15.

---

## 6. Escalation scoring — the evaluation criteria

`src/agent/escalation.py`. **Pure function, no LLM** (D-010). `score_ticket`
returns an `EscalationAssessment` whose `level` is `sum(signal.points)` bucketed.
`today` is injectable so the contract clock is the real calendar and tests are
deterministic (D-011).

### Signals

| Signal | Points | Trigger |
|---|---|---|
| `health_critical` | 30 | `health_score < 40` |
| `health_at_risk` | 15 | `40 ≤ health_score < 60` |
| `contract_expired` | 25 | contract end date in the past |
| `contract_renewal` | 18 | contract ends within `CONTRACT_RENEWAL_WINDOW_DAYS = 90` |
| `carr_high` | 15 | `CARR ≥ 72,000` |
| `carr_medium` | 8 | `54,000 ≤ CARR < 72,000` |
| `duplicate_cluster` | 20 | `≥ DUPLICATE_CLUSTER_SIZE = 3` prior filings of the same subject |
| `duplicate` | 10 | 1–2 prior filings |
| `module_mismatch` | 10 | ticket's `product_area` needs a module the tenant is not entitled to (`AREA_TO_MODULE` map, D-002) |
| `volume_decline` | 15 | operational volume down more than `DECLINE_THRESHOLD_PCT = -10%` vs the prior 30 days |
| `negative_sentiment` | 10 | any of the last 3 calls had negative sentiment |
| `competitor_mentioned` | 15 | a competitor named on any recent call — the cheapest churn signal in the corpus |
| `ticket_priority` | urgent 10 / high 5 / medium 0 / low 0 | the ticket's own stated priority — contributes, never dominates |

### Account-risk cap (D-012)

Signals split into **account-level** (`config.ACCOUNT_LEVEL_SIGNALS`: health,
contract, CARR, volume decline, sentiment, competitor) and **ticket-level**
(duplicates, module mismatch, priority).

```
account_risk = min(sum(account signals), MAX_ACCOUNT_RISK_POINTS)   # = ESCALATION_URGENT + 10 = 55
score        = account_risk + ticket_risk
```

Uncapped, account signals reach 95 and ticket signals 35, so every ticket from a
struggling tenant scored CRITICAL and the level stopped ranking. The cap makes
the rule one sentence: **a bad account is URGENT on its own; CRITICAL
additionally requires ≥ 15 points about this specific ticket** — a repeat filing
(20), or an entitlement gap plus stated urgency (10 + 5). `account_risk_capped`
records when the cap bit.

### Levels

| Level | Score |
|---|---|
| `CRITICAL` | `≥ 70` |
| `URGENT` | `≥ 45` |
| `ELEVATED` | `≥ 25` |
| `STANDARD` | `< 25` |

`EscalationAssessment.signals` is the audit trail; `.reasons` is the list of
per-signal sentences the brief prints and the narrator explains.

---

## 7. Prompt structure

`src/llm/prompts.py`. One LLM call in `_narrate`.

### System prompt (`TICKET_TRIAGE_SYSTEM_PROMPT`, verbatim intent)

> You are a support triage analyst for FleetPanda. You will receive a support
> ticket and a context pack assembled from five sources … Write the narrative
> sections of a triage brief from that context.
>
> 1. Use only facts present in the context pack. If something is not there, say
>    so rather than inferring it.
> 2. **Do not decide the escalation level. It is computed from scored signals and
>    is given to you; explain the reasoning behind it in plain language.**
> 3. The suggested response draft is addressed to the customer, not the support
>    agent. Keep it short and specific.

### User payload (`build_triage_payload(context, assessment)`)

A single JSON object. Keys:

| Key | Contents |
|---|---|
| `ticket` | id, subject, description, product_area, priority, status, created_at |
| `customer` | name, health_score, carr, contract_end_date, assigned_csm, modules_active, onboarding_status |
| `escalation` | **`level`, `score`, `account_risk`, `ticket_risk`, `reasons`, `missing_module`** — the decision, passed *in* |
| `duplicates` | id, created_at, status per duplicate |
| `past_tickets` | id, subject, status, resolution (up to 5) |
| `calls` | date, topic, sentiment, competitor_mentioned, action_items (up to 3) |
| `kb_articles` | id, title, root_cause, resolution, updated_at (up to 3) |
| `operations` | the six snapshot fields from §5 |
| `sources_with_no_data` | `context.missing_sources` |

The escalation level and its reasons are **inputs**. The model explains the
decision; it does not make it (D-010). `build_triage_payload` lives in
`prompts.py` because what the model is shown is as much a prompt decision as the
system message (D-015).

### Output parsing

The model returns a JSON object with `summary`, `escalation_reasoning`,
`suggested_response`. A markdown fence is stripped before parsing. **A parse
failure degrades to putting the whole reply in `summary`** rather than discarding
it — the deterministic half of the brief is already correct and complete, so a
malformed narrative costs formatting, not the brief.

---

## 8. Tenant isolation within triage

- Every JSON read is tenant-scoped through `Repository` (one filter point,
  `_index_by_tenant`).
- The operational snapshot runs under `TenantContext.for_tenant(tenant_id)` —
  same guard, same injected predicate as a typed question.
- **A pasted ticket takes its tenant from the session, never from its own text**
  (D-022). `parse_pasted_ticket` receives `tenant_id` as an argument and has no
  path that reads one out of the body, so a paste claiming `tenant_id: 7` is still
  scoped to the caller's tenant. An unscoped platform session is asked to scope
  first rather than having a customer guessed for it.
- **Visibility check** (`Router._triage`): in a bound session, a ticket whose
  `tenant_id` differs from the session's returns the *same* "I can't find ticket
  #N" message as a missing ticket — not a distinguishable refusal (enumeration
  oracle, F3 in [security-review.md](../../SECURITY.md)).

---

## 9. Evaluation / test coverage

| File | Asserts |
|---|---|
| `tests/test_escalation.py` (18) | Each signal's points and trigger; `level_for_score` buckets; the account-risk cap; duplicate clustering; `today` injection |
| `tests/test_triage.py` (19) | Five-source fan-in; graceful degradation and `missing_sources`; KB scoring and the floor; the deterministic-only path (no LLM); narrative parse-failure fallback |
| `tests/test_router.py` (21) | `extract_ticket_id` (three patterns); intent classification; the cross-tenant ticket visibility check |
| `tests/test_sql_questions.py` (13) | Graded Q5 (17 emergency orders, tenant 4) — ties the snapshot's inclusive window to the reference SQL |

### The three required scenario tickets

Asserted twice over. `tests/test_escalation.py:test_ticket_1083_is_all_three_test_cases_at_once`
pins the convergent case — #1083 is all three at once. `tests/test_triage.py`
asserts three **separate** tickets from three separate tenants, each chosen so
that exactly one of the three signals fires: a case that isolates one signal is
what shows the signal works, while a case where all three fire cannot tell you
which produced the level.

| Scenario | Isolating ticket | What the brief must show |
|---|---|---|
| Low-health customer (health < 40) with an expiring contract | **#1050** Timber Ridge Oil (t8), health 39, contract in 12 days | `health_critical` (30) + `contract_renewal` (18) → account risk hits the 55 cap → **URGENT** from the account alone, with `ticket_risk == 0` |
| Apparent duplicate of an earlier ticket | **#1058** Prairie Wind Fuels (t9), health 88 | `duplicate` signal naming **#1057**; `assessment.duplicate_ticket_ids` carries it. The account is healthy, so the duplicate is the whole signal |
| Ticket referencing a module the customer lacks | **#1005** Cascade Fuel Services (t1), health 82 | `module_mismatch` → `assessment.missing_module == "invoicing"`. Also pins the honest-empty KB path: `billing` has no article, so `kb_articles == ()` |

---

## 10. Known gaps

| Ref | Gap |
|---|---|
| Q-012 | The narrative call has never run against a live model — every test drives `tests/conftest.py:FakeLLM`. |
| Q-002, Q-005, Q-014 | Domain judgements pending human review: the `billing→invoicing` area/module mapping, the −10% decline cut, the escalation weights. |
