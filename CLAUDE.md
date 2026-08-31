# CLAUDE.md — FleetPanda AI Engineer Take-Home

Read this file at the start of every session. It is the source of truth for scope,
standards, and constraints. If anything you are about to do conflicts with this
file, stop and write the conflict into `docs/project/open-questions.md` instead of guessing.

---

## 1. Mission and stakes

This is a take-home assignment for an AI Engineer role. The build is graded, but
**the live session carries the most weight (20%)**: a 60–75 minute screen share
covering demo, code walkthrough, 20 minutes of live coding in this codebase, an
edge-case scenario, and an architecture discussion.

This has one consequence that overrides every other preference:

> **The developer must be able to explain every line of this codebase from memory,
> live, under observation, without reading it first.**

So: no cleverness, no dense one-liners, no metaprogramming, no abstractions that
exist "for later." Boring, obvious, well-named code. If there are two ways to do
something and one is shorter but harder to explain out loud, choose the other one.

The developer must also be able to **extend this codebase live** when given a new
requirement. Design so that most plausible new requirements are "add one small
file, add one registration line."

---

## 2. What the system is

A voice-and-chat support agent for FleetPanda, a B2B SaaS dispatch platform
serving ~12 fuel-delivery companies ("tenants") from shared infrastructure.
SOC 2 Type 2. Two capabilities:

1. **Dispatch database questions** (text-to-SQL over `dispatch.db`), always
   scoped to the correct tenant.
2. **Support ticket triage** — pull context from five sources and produce a
   structured brief with an escalation recommendation.

Both capabilities run through one agent core. Chat and voice are transports over
the same core, not two implementations.

### Data files (in `data/` unless stated otherwise)

| File | Notes |
|---|---|
| `dispatch.db` | SQLite, ~10K delivery orders, 12 tenants, 90 days |
| `SCHEMA.md` | Schema docs for dispatch.db |
| `customers.json` | 12 tenant profiles: health score, CARR, active modules, contract dates, CSM |
| `tenant_aliases.json` | Alternate company names → canonical tenant |
| `tickets.json` | ~85 support tickets |
| `call_transcripts.json` | ~43 transcripts keyed by `tenant_name` (string), NOT `tenant_id` |
| `knowledge_base.json` | 12 known-issue articles |

**If any of these files are missing, STOP.** Do not fabricate data, do not
generate synthetic fixtures, do not proceed with assumptions. Write what is
missing into `docs/project/open-questions.md` and report it.

---

## 3. Non-negotiable architectural constraints

1. **No agent framework.** No LangChain, LlamaIndex, CrewAI, LangGraph. Direct
   provider SDK calls only. This is a deliberate decision to be defended in
   docs/explanation/decisions-log.md — frameworks hide the prompts and control flow that the live
   session will interrogate.
2. **Tenant isolation is enforced in code, never by prompt.** A prompt asking the
   model to filter by tenant is not isolation. Enforcement lives in
   `src/db/guard.py` as an AST rewrite, backed by a read-only connection and a
   post-execution assertion on returned rows. Three independent layers.
3. **The database connection is read-only at the driver level.** Open with
   `sqlite3.connect("file:...?mode=ro", uri=True)` and `PRAGMA query_only=ON`.
   Even a hallucinated `DELETE` must be rejected by SQLite itself.
4. **Deterministic logic stays in Python.** Escalation scoring, duplicate
   detection, and module-mismatch detection are pure functions with unit tests.
   The LLM writes narrative prose; it does not make the call.
5. **Entity resolution fails closed.** When a tenant name is ambiguous, return
   candidates and ask — never silently pick the best fuzzy match.
6. **Small files.** Target 100–300 lines. If a file passes ~350 lines, split it
   along a real seam and say why in docs/explanation/decisions-log.md.
7. **Two registries are load-bearing** (see `src/data/sources.py` and the tool
   registry): adding a data source or a capability must not require editing
   agent logic.

---

## 4. Directory structure

Create exactly this. Do not invent additional top-level directories.

```
.
├── CLAUDE.md
├── README.md              # entry point + the documentation index
├── requirements.txt
├── .env.example
├── docs/                  # all project docs, Diátaxis split (see section 10)
│   ├── reference/         # what it is: the five feature specs + design.md
│   ├── explanation/       # why: architecture-decisions.md, decisions-log.md, recon.md, security-review.md
│   ├── how-to/            # tasks: run-locally.md
│   └── project/           # meta: open-questions.md, handoff.md, assignment.md
├── data/                  # provided data files (read-only, never modified)
├── src/
│   ├── config.py          # paths, model names, thresholds — no magic numbers elsewhere
│   ├── data/
│   │   ├── loaders.py     # raw JSON → typed structures
│   │   ├── resolver.py    # tenant name/alias → tenant_id
│   │   ├── sources.py     # DataSource protocol + REGISTRY
│   │   └── repository.py  # unified read API over all sources
│   ├── db/
│   │   ├── connection.py  # read-only sqlite
│   │   ├── schema.py      # introspection → compact schema card
│   │   ├── guard.py       # sqlglot AST validation + tenant predicate injection
│   │   └── executor.py    # row cap, timeout, result-set tenant assertion
│   ├── llm/
│   │   ├── client.py      # thin provider wrapper
│   │   └── prompts.py     # every system prompt, in one place
│   ├── agent/
│   │   ├── session.py     # TenantContext
│   │   ├── router.py      # dispatch_query | ticket_triage | clarify
│   │   ├── sql_agent.py
│   │   ├── triage_agent.py
│   │   └── escalation.py  # pure scoring functions, no LLM
│   └── interfaces/
│       └── cli_chat.py
└── tests/
    ├── conftest.py
    ├── test_entity_resolution.py
    ├── test_tenant_isolation.py
    ├── test_sql_questions.py
    └── test_security.py
```

---

## 5. Coding standards

- Python 3.11+. Type hints on every function signature. `dataclass` or Pydantic
  models over bare dicts crossing module boundaries.
- **Every module starts with a docstring** answering: what this module owns, what
  calls it, what it calls, and why it exists as a separate file. This is the
  walkthrough script.
- **Comment the non-obvious lines in plain English** — specifically any sqlglot
  AST manipulation, any fuzzy-matching threshold, any SQL date arithmetic. If a
  line would take more than five seconds to explain live, it gets a comment.
- No bare `except`. Catch specific exceptions, and either handle or re-raise with
  context.
- No hardcoded paths, model names, or thresholds outside `config.py`.
- Tests use `pytest`. Use `parametrize` for the eight SQL questions. Use fixtures
  for the DB connection and loaded data.
- Do not add dependencies beyond what is needed. Every line in
  `requirements.txt` must be pinned and justified in a trailing comment.

---

## 6. Decision-log protocol

`docs/explanation/decisions-log.md` is an engineering journal, not an essay written at the end. Append
an entry **at the moment a real decision is made**, in this format:

```markdown
### D-00N · <short title>
**Date:** <date>
**Context:** what in the data or the requirements forced a choice
**Options considered:** A / B / C, each one sentence
**Chosen:** X, because …
**Trade-off accepted:** what this costs us
**Where it lives:** `src/path/file.py:function`
```

Only log decisions that were genuinely contested. Do not log "used pytest."
Aim for 3–4 real entries in this foundation session.

Separately maintain a `## Data quality observations` section in the same file.
Append every anomaly found during recon, with the query or code that surfaced it
and one sentence on what a production system would need to do about it.

---

## 7. Current state and what is next

**Authoritative source for state: the session summary at the top of
`docs/project/open-questions.md`.** This section is a map; that file is the ledger, and it is
updated every session.

### Built and tested

| Layer | Files | Notes |
|---|---|---|
| Data | `config.py`, `data/{loaders,resolver,sources,repository}.py` | Resolver cascade is exact canonical → exact alias → normalized → fuzzy → refuse. It gates on **candidate count, not score** (D-003). |
| Database | `db/{connection,schema,guard,executor}.py` | Three isolation layers. The guard injects `tenant_id` per SELECT scope, including subqueries, derived tables and CTE bodies. |
| Session | `agent/session.py` | `TenantContext`: TENANT or PLATFORM. Questions 1, 2, 7 and 8 are cross-tenant and are refused when scoped. |
| Agent | `agent/{sql_agent,escalation,triage_agent,router}.py` | Two LLM calls per question (D-007); escalation is pure Python (D-010, D-012); triage fans in five sources. |
| Transport | `interfaces/{cli_chat,voice_chat}.py`, `interfaces/speech.py` | Chat runs without an API key — triage, scoping and every refusal path are deterministic. Voice is push-to-talk (`whisper-1` in, `tts-1` out) over the shared `Conversation` core (D-018). |
| Docs | `docs/explanation/recon.md`, `docs/reference/design.md`, `docs/explanation/decisions-log.md`, `docs/explanation/security-review.md`, `docs/project/open-questions.md` | Every assignment deliverable is written, voice included. |

**No stubs remain. 310 tests pass.** The eight graded questions are asserted twice:
against hand-written reference SQL, and end to end through the agent.

### Not built

Nothing required remains. Chat and voice both run end to end; the last two
requirements closed were voice mode (Step 5) and pasted-ticket triage.

- ~~**Voice mode (Step 5)**~~ — built. `interfaces/speech.py` owns the audio
  (mic capture, `whisper-1`, `tts-1`); `interfaces/voice_chat.py` is the
  push-to-talk loop with `speakable()` / `normalize_transcript`; the confirmation
  gate and scope live in the shared `agent/conversation.py` (D-018…D-021).
- ~~**Pasted-ticket parsing**~~ — built 2026-08-30 (D-022). A pasted body is parsed
  by `src/agent/ticket_parser.py`; the tenant comes from the bound session, never
  from the text, and an unscoped session is asked to scope before pasting.

### Two things that will bite a new session

1. **The test suite cannot catch anything about how the real API is called.** Every
   agent test drives `tests/conftest.py:FakeLLM`. `config.LLM_MODEL` once held a
   model ID that no longer exists and the client passed a `temperature` parameter
   that current models reject with a 400 — both sat there while the whole suite
   passed (Q-017). Verify current model IDs and parameters before writing API code.
    Verified against OpenAI reference: `gpt-4o-mini` with automatic prefix caching
    is the standard configuration in `src/llm/client.py`.
2. **The data ends 2026-05-29.** Anchored on `date('now')`, four of the eight graded
   questions return zero rows. Relative windows anchor on `MAX(delivery_date)` and
   the agent states the anchor in its reply (D-001). Contract proximity is the
   exception — it runs on the real calendar (D-011). Two clocks, deliberately.

### How the foundation was built

Steps 0–3 (recon → data layer → database layer → scaffolding) are complete and are
in git history, one commit per step. The requirements they were built against —
the resolver cascade, the guard's rejection list, the executor's limits — are now
documented where the code is: module docstrings, `docs/reference/design.md`, and the decision
entries that explain why each is shaped as it is.


## 8. Rules of engagement

- **Never fabricate data or schema.** Read the actual database and the actual
  JSON files. If something is unclear, it goes in `docs/project/open-questions.md`.
- **Never modify anything in `data/`.**
- Run the test suite after each step. Leave the repo green (with the SQL
  question tests skipped, which is expected at this stage).
- **When you change a feature's behavior, update its spec in `docs/reference/` in
  the same change** — the spec is a deliverable, not a snapshot. The map from
  code to spec is in section 10; if an edit crosses a feature boundary, update
  every spec it touches, and log a genuinely contested choice in `docs/explanation/decisions-log.md`.
  A spec that disagrees with the code is worse than no spec: the live session
  reads from it.
- **After editing or moving any doc, run `python3 .github/check_doc_links.py`** —
  it fails on a broken relative link, and CI runs the same check on every push.
- If a design choice has a real trade-off and you cannot resolve it from this
  file, **pick the more conservative option, implement it, and log the question
  in `docs/project/open-questions.md`** with your reasoning and the alternative. Do not block.
- At the end, write a `## Session summary` at the top of `docs/project/open-questions.md`:
  what was built, what was found in recon that changes the plan, what is stubbed,
  what decisions need a human, and the exact next three tasks.

---

## 9. The eight SQL questions the agent must answer

These drive the schema card and the test suite. Keep them visible.

1. How many deliveries were completed in the last 7 days across all tenants?
2. Which tenant delivered the most gallons of diesel last month?
3. Show me the top 5 drivers by total deliveries for tenant 3.
4. What is the average gallons per delivery for propane orders?
5. How many emergency orders did tenant 4 have in the past 30 days?
6. Which trucks are currently in maintenance status?
7. What is the fill rate (gallons delivered / gallons ordered) for completed
   orders by tenant?
8. List tenants with declining delivery volume (last 30 days vs previous 30).

Watch for the fan-out trap on 2 and 7: joining a one-to-many table before
aggregating silently inflates every aggregate. This produces correct-looking SQL
with wrong numbers, and it is the single most likely way to lose the correctness
marks. Note in docs/explanation/recon.md which questions are exposed to it.

Note also that questions **1, 2, 7 and 8** are cross-tenant. In a tenant-scoped
session the agent must refuse them; in an unscoped internal session it may answer.
That distinction lives in `TenantContext` and must be explicit, not implicit.

(Corrected 2026-08-29, was "1 and 7". Q2 "which tenant delivered the most gallons"
and Q8 "list tenants with declining volume" range over every tenant by
construction -- answering them scoped returns one tenant's rows presented as a
platform-wide ranking. See docs/project/open-questions.md Q-001.)

---

## 10. Feature specs — the code-to-spec map

Each feature has a living spec under `docs/reference/`. These are what the live
session reads from and points at, so they must track the code (section 8). When
you touch the code in the left column, update the spec in the right column in the
same change. `docs/reference/design.md` is the system-wide overview; `docs/explanation/architecture-decisions.md`
and `docs/explanation/decisions-log.md` carry the *why* behind contested choices.

| Feature / code | Spec to keep in sync |
|---|---|
| `src/db/{guard,executor,connection,schema}.py` (tenant isolation, three layers) | `docs/reference/tenant-isolation.md` |
| `src/agent/sql_agent.py`, SQL generation + schema card | `docs/reference/sql-agent.md` |
| `src/agent/{triage_agent,escalation,ticket_parser}.py` (brief fan-in, scoring) | `docs/reference/ticket-triage.md` |
| `src/data/resolver.py`, `src/agent/{router,session}.py` (resolution, routing, scope) | `docs/reference/entity-resolution.md` |
| `src/interfaces/{voice_chat,speech}.py`, `src/agent/conversation.py` (voice transport) | `docs/reference/voice-interface.md` |

Rule of thumb: if the change would make a sentence in one of these specs false,
the spec edit is part of the change, not a follow-up.
