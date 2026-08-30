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
| Tests | **299 pass, no skips.** No test needs an API key, a microphone, or a network connection. |
| Known gaps | The agent has not yet been run against a live model (Q-012). Voice is verified without a microphone in the loop (Q-020). |

The system has **no HTTP API**. It ships as two terminal transports. Where this
document refers to "the endpoint" or "deploying as a service", that is a
forward-looking note, not a description of code in this repository — see
[`docs/specs/tenant_isolation_spec.md`](docs/specs/tenant_isolation_spec.md#11-deploying-as-a-service).

---

## Prerequisites

- Python 3.11+ (developed on 3.12)
- One LLM key — `ANTHROPIC_API_KEY` **or** `OPENAI_API_KEY` — to run the agent.
  Not required to run the tests. Anthropic wins if both are set; nothing outside
  `src/llm/client.py` knows which provider is in use.
- Voice mode additionally requires `OPENAI_API_KEY` specifically: it drives both
  speech-to-text (`whisper-1`) and text-to-speech (`tts-1`).

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

The eight questions double as the evaluation harness. With a key set, this runs
them against the live model — nothing is primed, so the model writes the SQL
itself and every assertion stays unchanged. One PASS/FAIL line per question is
the score (~2 API calls per question):

```bash
FLEETPANDA_EVAL_LLM=1 .venv/bin/python -m pytest tests/test_sql_questions.py -v
```

---

## Documentation

This documentation is modular (hub-and-spoke, following the
[Diátaxis](https://diataxis.fr/) split). Start here; go deep in the spokes.

| Document | Diátaxis type | What it covers |
|---|---|---|
| **README.md** (this file) | — | Summary, setup, how to run, the map below |
| [docs/architecture_decisions.md](docs/architecture_decisions.md) | Explanation | The *why*. Why AST parsing (`sqlglot`) enforces multi-tenant SQL isolation; why the triage context pipeline is a structured join and deterministic score rather than RAG; the trade-offs behind each. |
| [docs/specs/tenant_isolation_spec.md](docs/specs/tenant_isolation_spec.md) | Reference / build guide | The security design: the `TenantContext` authority object, how the tenant is established and propagated, the three enforcement layers, and how the same guarantee maps onto a FastAPI service (dependency-injected tenant, no caller-supplied `tenant_id`). |
| [docs/specs/ticket_triage_agent_spec.md](docs/specs/ticket_triage_agent_spec.md) | Reference | The triage agent: pipeline stages, the five sources, KB retrieval scoring, the escalation rubric (weights and thresholds), prompt structure, customer-profile ("CRM") source, and evaluation criteria. |
| [docs/handoff.md](docs/handoff.md) | Explanation / status | What is complete, what is pending, and the known edge cases and limitations. |

### Supporting deliverables (assignment context, retained)

| Document | What it is |
|---|---|
| [DECISIONS.md](DECISIONS.md) | The dated engineering journal (D-001 … D-022), plus the cost model, the 150-tenant scaling answer, and the end-customer-agent answer. `docs/architecture_decisions.md` is the narrative digest of this file. |
| [SECURITY.md](SECURITY.md) | Code-review challenge: three vulnerabilities in a sample text-to-SQL endpoint, each with an attack scenario and the implemented fix. |
| [RECON.md](RECON.md) | Step 0 data exploration. Explains most of the design. |
| [DESIGN.md](DESIGN.md) | Request-flow diagrams and module boundaries. |
| [OPEN_QUESTIONS.md](OPEN_QUESTIONS.md) | The state ledger: session summary and questions that need a human. |
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
tests/                 299 tests; entity resolution, tenant isolation, the 8
                       questions, security fixes, escalation, triage, ticket
                       parsing, voice
```

## Non-negotiable constraints

- **No agent framework.** Direct provider SDK calls only.
- **Tenant isolation is enforced in code, never by prompt.** Three independent layers.
- **The database connection is read-only at the driver level.** Even a hallucinated `DELETE` is refused by SQLite itself.
- **Deterministic logic stays in Python.** Escalation scoring, duplicate detection, and module-mismatch detection are pure functions with unit tests. The LLM writes prose; it does not make the call.
- **Entity resolution fails closed.** An ambiguous tenant name returns candidates and asks — it never picks the best fuzzy match.
