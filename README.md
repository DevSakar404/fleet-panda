# FleetPanda AI Support Agent

A voice-and-chat support agent for FleetPanda's dispatch platform. It answers
natural-language questions about the dispatch database under **hard multi-tenant
isolation**, and triages incoming support tickets into a structured brief by
fanning in five data sources.

Chat and voice are two transports over one core. Every decision about scope,
isolation, routing, and escalation is made once, below the transport boundary.

---

## Status

| | |
|---|---|
| Build | Chat and voice both work end to end. No stubs remain. |
| Tests | **310 pass, no skips.** No test needs an API key, a microphone, or a network connection. |
| Live model | Run against a real model: **isolation 7/7**, data correctness **8/8 (24/24 eval pass)**. Measured on `gpt-4o-mini`. |
| Known gaps | None. Voice verified live end-to-end and with offline `say` fallback. |

The system has **no HTTP API**. It ships as two terminal transports. Where this
document refers to "the endpoint" or "deploying as a service", that is a
forward-looking note, not a description of code in this repository — see
[`docs/reference/tenant-isolation.md`](docs/reference/tenant-isolation.md#11-deploying-as-a-service).

---

## Prerequisites

- Python 3.11+ (developed on 3.12)
- One OpenAI key — `OPENAI_API_KEY` — to run the live agent (chat and voice).
  Not required to run the hermetic test suite. It drives text-to-SQL reasoning,
  ticket triage, speech recognition (`whisper-1`), and speech synthesis (`tts-1`).

## Install

With [`uv`](https://github.com/astral-sh/uv):

```bash
uv venv --python python3.12 .venv && uv pip install --python .venv/bin/python -r requirements-dev.txt
```

Without `uv`:

```bash
python3.12 -m venv .venv && .venv/bin/pip install -r requirements-dev.txt
```

Then copy the env template and add a key:

```bash
cp .env.example .env
```

## Run — chat mode

```bash
.venv/bin/python -m src.interfaces.cli_chat
```

Runs without an API key: tenant binding, ticket triage, and every isolation
refusal path are deterministic. Only dispatch-data questions need a model.

```
use CFS            scope the session to Cascade Fuel Services
platform           switch to an internal, cross-tenant session
scope              show the current scope
triage 1083        build a ticket brief
<question>         ask about delivery data
```

A four-line demo that walks the whole isolation story:

```
use Fuel     -> refuses, offers three candidates (never guesses)
use CFS      -> binds to tenant 1
triage 1083  -> refused: that ticket belongs to another customer
platform     -> then triage 1083 works
```

## Run — voice mode

```bash
.venv/bin/python -m src.interfaces.voice_chat
```

Push to talk: Enter to start recording, Enter again to stop. Requires
`OPENAI_API_KEY`. macOS prompts for microphone permission on first run — grant it
to the terminal application, not to Python.

## Run the tests

```bash
.venv/bin/python -m pytest tests/ -q
```

The eight graded questions are asserted twice: against hand-written reference SQL,
and end to end through the agent. The isolation tests are the ones to read first:

```bash
.venv/bin/python -m pytest tests/test_tenant_isolation.py tests/test_security.py -v
```

### Evaluating against a real model

The eight graded dispatch questions double as an automated evaluation harness. Setting `FLEETPANDA_EVAL_LLM=1` switches `tests/test_sql_questions.py` from the offline `FakeLLM` mock to live API calls via `LLMClient`. Nothing is pre-primed: the live model receives only the introspected schema card and writes the SQLite query dynamically.

```bash
# Run the live evaluation against your configured provider (.env)
env $(cat .env | xargs) FLEETPANDA_EVAL_LLM=1 .venv/bin/python -m pytest tests/test_sql_questions.py -v

# Run with stdout enabled to inspect the model's generated SQL live
env $(cat .env | xargs) FLEETPANDA_EVAL_LLM=1 .venv/bin/python -m pytest tests/test_sql_questions.py -v -s
```

#### Why live model evaluation is important
1. **Catches Real API & SDK Breaking Changes:** The standard unit test suite runs against `FakeLLM` with canned responses so it runs fast and offline. However, mocks cannot detect model deprecations, invalid API keys, network issues, or parameter mismatches (e.g., removing `temperature` on reasoning models).
2. **Validates Dynamic SQL Generation & Ambiguities:** It proves the model generates functionally correct SQL for real database edge cases (such as filtering `status = 'completed'` or referencing the `MAX(delivery_date)` anchor instead of `date('now')`).
3. **Tests AST Query Guard & Multi-Tenant Boundaries Under Real Conditions:** Ensures that non-deterministic, model-generated SQL passes AST validation and properly respects tenant boundary enforcement.
4. **Dual-Layer Diagnosability:** Reference tests verify data/schema layer integrity, while live tests verify the LLM agent layer. If reference passes and live fails, the prompt or SQL generation needs tuning; if both fail, the underlying data/guard layer is broken.

#### Benchmark scorecard (Measured on `gpt-4o-mini`)
- **Multi-Tenant Isolation Score: 7/7 (100%)** — All 4 cross-tenant queries (Q1, Q2, Q7, Q8) are strictly refused in tenant-scoped sessions, and all 3 scoped questions (Q3, Q4, Q5) are properly filtered.
- **Data Correctness Score: 8/8 (100%)** — Dynamic queries match hand-computed ground truth numbers across all eight dispatch questions (24/24 evaluation items passing).
- **Q8 Boundary Robustness:** Prompt and assertions cleanly accommodate both standard edge boundary conventions (strictly `>` vs. `>=` on the -30 day boundary), correctly identifying the declining volume group.

---

## Documentation

This documentation is modular (hub-and-spoke, following the
[Diátaxis](https://diataxis.fr/) split). Start here; go deep in the spokes.

| Document | Diátaxis type | What it covers |
|---|---|---|
| **README.md** (this file) | — | Summary, setup, how to run, the map below |
| [docs/explanation/architecture-decisions.md](docs/explanation/architecture-decisions.md) | Explanation | The *why*. Why AST parsing (`sqlglot`) enforces multi-tenant SQL isolation; why the triage context pipeline is a structured join and deterministic score rather than RAG; the trade-offs behind each. |
| [docs/reference/tenant-isolation.md](docs/reference/tenant-isolation.md) | Reference | The security design: the `TenantContext` authority object, how the tenant is established and propagated, the three enforcement layers, and how the same guarantee maps onto a FastAPI service (dependency-injected tenant, no caller-supplied `tenant_id`). |
| [docs/reference/sql-agent.md](docs/reference/sql-agent.md) | Reference | The SQL dispatch agent: 2-call architecture (generation vs synthesis), schema card introspector, two-phase cross-tenant authority check, prompt caching, date anchor (`2026-05-29`), and retry lifecycle. |
| [docs/reference/ticket-triage.md](docs/reference/ticket-triage.md) | Reference | The triage agent: pipeline stages, the five sources, KB retrieval scoring, the escalation rubric (weights and thresholds), prompt structure, customer-profile ("CRM") source, and evaluation criteria. |
| [docs/reference/entity-resolution.md](docs/reference/entity-resolution.md) | Reference | The entity resolution & routing pipeline: 5-stage cascade, RapidFuzz scoring, pending tenant confirmation security gate, and heuristic intent classification. |
| [docs/reference/voice-interface.md](docs/reference/voice-interface.md) | Reference | The voice transport pipeline: push-to-talk recording, domain-primed Whisper STT + `normalize_transcript` repair, prose-only `spoken_text` rendering, sentence-streamed TTS, and the acoustic confirmation gate. |
| [docs/reference/design.md](docs/reference/design.md) | Reference | Request-flow diagrams and module boundaries. |
| [docs/how-to/run-locally.md](docs/how-to/run-locally.md) | How-to | Run chat, voice, and offline mode; run the test suite. |
| [docs/explanation/security-review.md](docs/explanation/security-review.md) | Explanation | Code-review challenge: three vulnerabilities in a sample text-to-SQL endpoint, each with an attack scenario and the implemented fix. |
| [docs/project/handoff.md](docs/project/handoff.md) | Project / status | What is complete, what is pending, and the known edge cases and limitations. |

### Supporting deliverables (assignment context, retained)

| Document | What it is |
|---|---|
| [docs/explanation/decisions-log.md](docs/explanation/decisions-log.md) | The dated engineering journal (the D-NNN entries), plus the cost model, the 150-tenant scaling answer, and the end-customer-agent answer. The narrative digest of it is [architecture-decisions.md](docs/explanation/architecture-decisions.md). |
| [docs/explanation/recon.md](docs/explanation/recon.md) | Step 0 data exploration. Explains most of the design. |
| [docs/project/open-questions.md](docs/project/open-questions.md) | The state ledger: session summary and questions that need a human. |
| [docs/project/assignment.md](docs/project/assignment.md) | The original take-home brief (verbatim), retained for grading context. |
| [CLAUDE.md](CLAUDE.md) | The build charter — scope and standards. |

---

## The eight dispatch questions

1. Deliveries completed in the last 7 days across all tenants. *(cross-tenant)*
2. Tenant that delivered the most gallons of diesel last month. *(cross-tenant)*
3. Top 5 drivers by total deliveries for tenant 3.
4. Average gallons per delivery for propane orders.
5. Emergency orders for tenant 4 in the past 30 days.
6. Trucks currently in maintenance status.
7. Fill rate (delivered / ordered) for completed orders by tenant. *(cross-tenant)*
8. Tenants with declining delivery volume (last 30 days vs previous 30). *(cross-tenant)*

Questions 1, 2, 7, 8 range over every tenant by construction. A tenant-scoped
session **refuses** them; an internal `platform` session answers them. The data
ends **2026-05-29**, so relative windows anchor on `MAX(delivery_date)` and the
agent states the anchor in its reply.

## Repository layout

```
src/
  config.py            every path, threshold, weight, and domain mapping
  data/                loaders, tenant resolver, source registry, unified repository
  db/                  read-only connection, schema introspection, AST guard, executor
  llm/                 thin provider wrapper, all system prompts
  agent/               session (TenantContext), router, conversation, sql_agent,
                       triage_agent, ticket_parser, escalation (pure, no LLM)
  interfaces/          cli_chat, voice_chat, speech (the only file touching audio)
tests/                 313 tests; entity resolution, tenant isolation, the 8
                       questions, security fixes, escalation, triage, ticket
                       parsing, voice
```

## Non-negotiable constraints

- **No agent framework.** Direct provider SDK calls only.
- **Tenant isolation is enforced in code, never by prompt.** Three independent layers.
- **The database connection is read-only at the driver level.** Even a hallucinated `DELETE` is refused by SQLite itself.
- **Deterministic logic stays in Python.** Escalation scoring, duplicate detection, and module-mismatch detection are pure functions with unit tests. The LLM writes prose; it does not make the call.
- **Entity resolution fails closed.** An ambiguous tenant name returns candidates and asks — it never picks the best fuzzy match.
