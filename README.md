# FleetPanda AI Support Agent

A voice-and-chat support agent for FleetPanda's dispatch platform. It answers
natural-language questions about the dispatch database with hard multi-tenant
isolation, and triages incoming support tickets against five data sources.

> **Build status: chat and voice both work end to end.** Data layer, database
> layer, isolation guard, SQL agent, escalation scoring, triage pipeline, router,
> and both transports are built and tested. No stubs remain. 275 tests pass.
>
> Three caveats worth reading before a demo. The agent has never spoken to a real
> model (no API key on the build machine — its tests drive it with a scripted
> fake, Q-012). Voice mode is verified as far as it can be without a microphone:
> transcript repair, spoken rendering and the confirmation gate are all under
> test, but nobody has yet spoken into it (Q-020). And a *pasted* ticket body is
> recognised but not yet parsed, so triage works by ticket id only (Q-015). See
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

An API key is needed only to run the agent, not to run the tests. Set **one** of
`ANTHROPIC_API_KEY` or `OPENAI_API_KEY` — the provider is whichever is present
(Anthropic wins if both are), and nothing outside `src/llm/client.py` knows which:

```bash
cp .env.example .env
```

## Run the tests

```bash
.venv/bin/python -m pytest tests/ -q
```

275 pass, no skips. The eight graded questions are asserted twice: once against
hand-written reference SQL, and once end to end through the agent. No test needs
an API key, a microphone or a network connection.

To see the isolation tests alone — the ones worth reading first:

```bash
.venv/bin/python -m pytest tests/test_tenant_isolation.py tests/test_security.py -v
```

## Chat mode

```bash
.venv/bin/python -m src.interfaces.cli_chat
```

Runs without an API key: tenant binding, ticket triage and every isolation refusal
are deterministic. Only data questions need a model.

```
use CFS            scope the session to Cascade Fuel Services
platform           switch to an internal, cross-tenant session
scope              show the current scope
triage 1083        build a ticket brief
<question>         ask about delivery data
```

Worth trying in a demo, in this order — it walks the whole isolation story in four
lines:

```
use Fuel           -> refuses, offers three candidates (never guesses)
use CFS            -> binds to tenant 1
triage 1083        -> refused: that ticket belongs to another customer
platform           -> then triage 1083 works
```

## Voice mode

```bash
.venv/bin/python -m src.interfaces.voice_chat
```

Push to talk: press Enter to start recording, Enter again to stop. Requires
`OPENAI_API_KEY` — it drives both speech recognition (`whisper-1`) and synthesis
(`tts-1`), so one OpenAI key runs the whole system. macOS will ask for microphone
permission on the first run; grant it to the terminal application, not to Python.

```
[platform] press Enter to speak >
  recording... (press Enter to stop)
  you said: "use CFS"
  thinking...
  Scoped to Cascade Fuel Services (tenant 1).

[tenant 1] press Enter to speak >
  you said: "how many emergency orders in the past 30 days"
  ♪ "In the 30 days to 29 May 2026, the most recent data available,
     Cascade Fuel Services had 17 emergency orders."

  SQL   SELECT COUNT(*) AS n FROM delivery_orders WHERE priority = 'emergency'
        AND order_date >= date((SELECT MAX(delivery_date) ...), '-30 day')
        AND delivery_orders.tenant_id = 1 LIMIT 200
```

Voice and chat are two renderings of one session. `Conversation`
(`src/agent/conversation.py`) owns the scope and the confirmation gate; the two
transports only decide how to say things. The differences are all in the
rendering:

| | Chat | Voice |
|---|---|---|
| Data answer | prose **+ the executed SQL** | prose only — SQL is on screen, never read aloud |
| Ticket brief | the full ~25-line brief | level, score, top two reasons, "the full brief is on screen" |
| Dates | `2026-07-15` | "15 July 2026" |
| Inexact tenant name | confirm before binding | *same gate, inherited* |

Two voice-specific details worth knowing about:

- **Spelled-out short codes.** Speech-to-text renders `"use CFS"` as `"use C F S"`.
  The resolver normalises case but not spacing, so the run is collapsed before
  resolution (`normalize_transcript`). Whisper also punctuates commands, so
  `"Platform."` is stripped to `platform`.
- **Confirmation is load-bearing here.** An inexact name returns `CONFIRM` and
  binds nothing until the next utterance is an explicit yes. Speech-to-text
  produces exactly these near-misses, and anything other than yes — including
  silence or a new question — cancels.

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
| [SECURITY.md](SECURITY.md) | Code-review challenge: three vulnerabilities, attack scenarios, fixes. |

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
    client.py        thin provider wrapper (Anthropic or OpenAI, one branch)
    prompts.py       every system prompt, in one file
  agent/
    session.py       TenantContext: who is asking, and what they may see
    router.py               intent classification and dispatch (stateless)
    conversation.py         session state: scope + the confirmation gate
    sql_agent.py            question -> guarded SQL -> answer
    triage_agent.py         ticket -> five-source brief
    escalation.py           deterministic scoring, no LLM
  interfaces/
    cli_chat.py             terminal transport, renders for the eye
    voice_chat.py           voice transport, renders for the ear
    speech.py               the only file that touches audio
```
