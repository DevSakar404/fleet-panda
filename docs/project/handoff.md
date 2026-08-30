# Handoff — FleetPanda AI Support Agent

← [README](../../README.md) · [Architecture decisions](../explanation/architecture-decisions.md) · Specs: [tenant isolation](../reference/tenant-isolation.md) · [ticket triage](../reference/ticket-triage.md)

The living state ledger is [`open-questions.md`](open-questions.md) (session
summary at the top, `Q-NNN` entries below). This document is the fixed-point
summary: what is done, what is not, and what will bite the next person.

---

## Status

Chat and voice both work end to end. **313 tests pass, no skips.** No test
requires an API key, a microphone, or a network connection — every agent test
drives `tests/conftest.py:FakeLLM`. The build history is a sequence of small
commits, one step per commit (recon → data → database → agent → transports).

The eight graded questions are asserted twice: against hand-written reference SQL,
and end to end through the agent.

---

## Complete

| Layer | Files | Notes |
|---|---|---|
| Config | `src/config.py` | Every path, threshold, weight, and domain mapping, each annotated with the recon finding behind it |
| Data | `src/data/{loaders,resolver,sources,repository}.py` | Loaders → frozen dataclasses. Resolver cascade fails closed, gating on candidate *count* not score (D-003). `sources.REGISTRY` is the "add a source in one line" seam. `Repository` is the single tenant-filter point for JSON |
| Database | `src/db/{connection,schema,guard,executor}.py` | Three isolation layers ([spec](../reference/tenant-isolation.md)). Guard injects `tenant_id` per SELECT scope — subqueries, derived tables, CTE bodies. Schema card is introspected, not read from `SCHEMA.md` (D-006) |
| Session | `src/agent/session.py` | `TenantContext` — `TENANT` or `PLATFORM`, frozen, invariant-checked at construction |
| SQL agent | `src/agent/sql_agent.py` | Two LLM calls (D-007); cross-tenant refusal checked twice (D-008); one retry, not a loop |
| Escalation | `src/agent/escalation.py` | Pure Python, no LLM. Additive points, account-risk cap (D-010, D-012) |
| Triage | `src/agent/triage_agent.py` | Five-source fan-in → `TicketBrief` ([spec](../reference/ticket-triage.md)). Every section degrades to empty |
| Router / conversation | `src/agent/{router,conversation}.py` | Router is stateless; `Conversation` owns scope + the confirmation gate, shared by both transports (D-018) |
| Pasted tickets | `src/agent/ticket_parser.py` | A pasted body is parsed into a `Ticket` and triaged. The tenant comes from the session, never from the text (D-022) |
| Chat transport | `src/interfaces/cli_chat.py` | Runs without an API key; prints the guard's rewritten SQL beside every answer |
| Voice transport | `src/interfaces/{voice_chat,speech}.py` | Push-to-talk (D-020); `whisper-1` + `tts-1`; `speech.py` is the only file touching audio. Voice renders our own prose for the ear (D-021) |
| Docs | this set, plus `decisions-log.md`, `security-review.md`, `recon.md`, `design.md` | Cost model, 150-tenant scaling answer, and end-customer isolation answer are all in `decisions-log.md` |

---

## Pending

| Ref | Item | Estimate |
|---|---|---|
| **Q-012** | **Answered 2026-08-30.** Against a live model: isolation **7/7**, data correctness **7-8/8** (was 2/8 before D-023). Q5 is now stable at 4/4 after the anchor column was pinned (D-024); **only Q8 still varies run to run**. Measured on `gpt-4o-mini` — re-run with an Anthropic key, which is what `config` defaults to. | — |
| **Q-020** | **Answered 2026-08-30.** Voice mode verified live end-to-end with real microphone capture, `whisper-1` transcription, `tts-1` synthesis, transcript repair, spoken rendering, and confirmation gating. | — |
| Q-018 | Provider-native **structured outputs** would delete the fence-stripping JSON parser and turn a class of refusal into an impossibility. Best done while watching real responses (during Q-012). | ~30 min |
| Q-002 | The `product_area → module` map (`billing→invoicing`, `reporting→analytics`) is inferred, not documented. Under-flags by design. Needs FleetPanda domain confirmation. | edit |
| Q-005 | The −10% materiality cut for "declining volume" (Q8) is provisional. | edit |
| Q-014 | The escalation weights are a first-pass calibration against a 12-tenant roster. | edit |
| Q-004 | Recon scripts are not committed (queries are inlined in `recon.md`). Consider a `scripts/` dir for the live session. | decision |

---

## Known edge cases and limitations

- **Two clocks.** Dispatch queries and the triage snapshot anchor relative
  windows on `MAX(delivery_date)` (data ends 2026-05-29); escalation contract
  proximity runs on the real calendar (D-001, D-011). A reader must track which
  clock a number is on — the agent always states the data anchor.
- **In a scoped session the anchor is tenant-local, not platform-wide** (D-024).
  The guard injects its predicate into the anchor subquery too, so "the past 30
  days" is measured from *that tenant's* newest row. Ten of twelve tenants match the
  platform anchor; tenants 4 and 11 trail it by a day on `order_date` (DQ-10), so a
  scoped window over them is a day wider. Neither reading is wrong and the system
  does not currently say which it used.
- **Layer 3 is a detector, not a guarantee.** The post-execution row assertion
  can only inspect a `tenant_id` it can see; `SELECT COUNT(*)` projects none.
  Pinned by `test_the_row_assertion_is_a_detector_not_a_guarantee`; Q-010 argues
  against a fourth layer.
- **`SessionScope` is self-asserted.** The caller of `Conversation` chooses
  `TENANT` vs `PLATFORM`. Fine for a CLI; a deployed service must derive it from
  the authenticated principal (F1 in `security-review.md`, open by design). See the
  [isolation spec §11](../reference/tenant-isolation.md#11-deploying-as-a-service).
- **Cross-tenant ticket lookups return "not found", not "refused"** — identical
  to a genuinely missing ticket, to avoid an enumeration oracle over sequential
  ticket ids (F3).
- **`billing` tickets get no KB article**, by design — the corpus has no
  `billing` coverage, and the relevance floor returns nothing rather than a
  least-bad match.
- **6 of 12 tenants have no `tank_readings`; 37 of 85 tickets have a null
  resolution.** Every triage section degrades to empty and is listed in
  `TicketContext.missing_sources`.
- **Unparseable SQL is refused, not repaired.** `sqlglot`'s SQLite dialect
  coverage is a functional limit; the version is pinned.
- **Fuzzy tenant matches never bind silently.** A normalised or fuzzy resolution
  returns `CONFIRM` and binds nothing until the next utterance is an explicit
  affirmative; anything else cancels.
- **No conversation memory.** Each turn is independent apart from the scope and
  the pending confirmation held in `Conversation`. `Router` is stateless.
- **Single user.** No concurrency, no session store, no auth.

---

## How to resume

The next three tasks, from [`open-questions.md`](open-questions.md):

1. **Put `OPENAI_API_KEY` in `.env` and speak into it (Q-020).** One utterance —
   "use C F S" — exercises capture, transcription, transcript repair, the
   resolver, and synthesis.
2. **Re-run the graded questions with an ANTHROPIC key (Q-012, D-023).**

   ```bash
   FLEETPANDA_EVAL_LLM=1 .venv/bin/python -m pytest tests/test_sql_questions.py -v
   ```

   Already run on `gpt-4o-mini`: isolation 7/7, data correctness 6-8/8 depending
   on the run. But `config` defaults to `claude-opus-5` and the client prefers
   Anthropic when both keys are present, so a grader is running a different system
   than that measurement. Q5 and Q8 are the unstable two.
3. **Adopt provider-native structured outputs (Q-018).** Deletes the
   fence-stripping JSON parser in `sql_agent._parse_generation` and turns a class
   of refusal into an impossibility. Best done during task 2, while real responses
   are already on screen.

---

## Where to look during a walkthrough

| Question | File · function |
|---|---|
| "Show me tenant isolation" | `src/db/guard.py:_inject_tenant_predicates`, then `_direct_sources` |
| "How do you know it works?" | `tests/test_tenant_isolation.py` — the deliberately-bypassed-guard test |
| "How does entity resolution feed the SQL agent?" | `resolver.resolve` → `session.TenantContext` → `guard` takes the id from the context |
| "Why does it refuse this?" | `session.allows_question` (authority) vs `guard` reasons (the SQL itself) |
| "Why not a vector DB for triage?" | `triage_agent.find_kb_articles` docstring — 12 articles (D-013) |
| "Why not LangChain?" | `llm/client.py` — 122 lines, one branch |
| "Why isn't voice a streaming pipeline?" | `decisions-log.md` D-019 — the latency table |
