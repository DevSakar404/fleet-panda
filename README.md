# FleetPanda AI Support Agent

A voice-and-chat support agent for FleetPanda's dispatch platform. It answers
natural-language questions about the dispatch database with hard multi-tenant
isolation, and triages incoming support tickets against five data sources.

> **Build status: foundation.** The data layer, the database layer and the tenant
> isolation guard are complete and tested. The agent itself (routing, text-to-SQL,
> triage) is scaffolded with specifications in each module docstring and raises
> `NotImplementedError`. Voice mode is not started. See
> [OPEN_QUESTIONS.md](OPEN_QUESTIONS.md) for the session summary and next tasks.
>
> The assignment brief as received is preserved in git history at commit
> `0554639`.

## Setup

Requires Python 3.11+ (developed on 3.12).

```bash
uv venv --python python3.12 .venv && uv pip install --python .venv/bin/python -r requirements-dev.txt
```

Without `uv`:

```bash
python3.12 -m venv .venv && .venv/bin/pip install -r requirements-dev.txt
```

An API key is needed only to run the agent, not to run the tests:

```bash
cp .env.example .env
```

## Run the tests

```bash
.venv/bin/python -m pytest tests/ -q
```

94 pass, 12 skip. The skips are the agent-path tests for the eight graded
questions; their reference-SQL counterparts run and assert real numbers today.

To see the isolation tests alone — the ones worth reading first:

```bash
.venv/bin/python -m pytest tests/test_tenant_isolation.py tests/test_security.py -v
```

## Chat mode

Not yet implemented — `src/interfaces/cli_chat.py` is a stub. It will run as:

```bash
.venv/bin/python -m src.interfaces.cli_chat
```

## Voice mode

Not yet started.

## How tenant isolation works

Three independent layers, none of which trusts the others:

1. **Read-only connection** (`src/db/connection.py`) — the database is opened with
   `mode=ro` and `PRAGMA query_only=ON`, so a write is refused by SQLite itself
   even if everything above it fails.
2. **AST guard** (`src/db/guard.py`) — generated SQL is parsed with `sqlglot`,
   validated against an allowlist, and rewritten so that every tenant-scoped table
   reference carries `tenant_id = <session tenant>`, including inside subqueries,
   derived tables and CTE bodies. Isolation is never asked of the prompt.
3. **Post-execution assertion** (`src/db/executor.py`) — returned rows are checked
   for a foreign `tenant_id`. This is a detector rather than a guarantee, and the
   test suite says so.

A tenant-scoped session refuses the four cross-tenant questions outright rather
than narrowing them to one tenant, because a one-tenant answer presented as a
platform ranking is a wrong answer that looks right.

## Documents

| File | What it is |
|---|---|
| [CLAUDE.md](CLAUDE.md) | The build charter. Source of truth for scope and standards. |
| [RECON.md](RECON.md) | Step 0 data exploration. Read this first — it explains most of the design. |
| [DESIGN.md](DESIGN.md) | How the pieces fit: request flows, the three isolation layers, module boundaries. |
| [DECISIONS.md](DECISIONS.md) | Engineering journal, appended as decisions were made. |
| [OPEN_QUESTIONS.md](OPEN_QUESTIONS.md) | Session summary, and questions needing a human. |
| [SECURITY.md](SECURITY.md) | Code-review challenge. Outline only so far. |

## Layout

```
src/
  config.py          every path, threshold and mapping, each annotated with the
                     recon finding that produced it
  data/
    loaders.py       JSON -> frozen dataclasses
    resolver.py      tenant name/alias -> tenant_id, or an honest refusal
    sources.py       DataSource protocol + REGISTRY (add a source in one line)
    repository.py    one read API; the single place JSON records meet a tenant
  db/
    connection.py    read-only sqlite
    schema.py        introspection -> prompt schema card
    guard.py         sqlglot AST validation + tenant predicate injection
    executor.py      row cap, timeout, post-execution tenant assertion
  llm/
    client.py        thin Anthropic wrapper
    prompts.py       every system prompt, in one file
  agent/
    session.py       TenantContext: who is asking, and what they may see
    router.py        STUB   intent classification and dispatch
    sql_agent.py     STUB   question -> guarded SQL -> answer
    triage_agent.py  STUB   ticket -> five-source brief
    escalation.py    STUB   deterministic scoring, no LLM
  interfaces/
    cli_chat.py      STUB   terminal transport
```
