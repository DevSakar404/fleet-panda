# Spec — Multi-Tenant Isolation

← [README](../../README.md) · [Architecture decisions](../explanation/architecture-decisions.md) · Sibling specs: [ticket triage](ticket-triage.md) · [SQL agent](sql-agent.md) · [entity resolution](entity-resolution.md) · [voice interface](voice-interface.md)

**Status:** implemented and tested (`tests/test_tenant_isolation.py`,
`tests/test_security.py`). This spec is the build guide for the isolation
mechanism: the authority object, how a tenant is established and propagated, the
first resolution boundary, and the three enforcement layers. §11 states how the
same guarantee maps onto a FastAPI service.

---

## 1. Threat model

A **tenant-scoped session** (a support rep or CSM working one account) must never
obtain another tenant's data — not through a mistaken query, a hallucinated
query, or a prompt-injected one. FleetPanda is SOC 2 Type 2 on shared
infrastructure; cross-tenant exposure is the critical failure.

Two distinct things are enforced:

1. **Row-level scoping** — every SQL read is filtered to the bound tenant, in
   code, regardless of what SQL the model produced.
2. **Cross-tenant *questions*** ("which tenant delivered the most?") are
   **refused** in a scoped session, not silently narrowed to one tenant. A
   one-tenant answer presented as a platform ranking is a wrong answer that looks
   right. This is an authority check, separate from row scoping — see §7.

Isolation is **never** asked of the prompt. The SQL system prompt explicitly
tells the model *not* to add a tenant filter, so that the guard's injected
predicate is the only one and a refusal is never a matter of model cooperation.

---

## 2. The authority object — `TenantContext`

`src/agent/session.py`. A frozen dataclass; the single input every lower layer
reads to decide scoping.

```python
@dataclass(frozen=True, slots=True)
class TenantContext:
    scope: SessionScope          # TENANT | PLATFORM
    tenant_id: int | None = None
```

| Invariant | Enforced by |
|---|---|
| A `TENANT` context always carries a `tenant_id` | `__post_init__` raises `ValueError` otherwise |
| A `PLATFORM` context never carries a `tenant_id` | `__post_init__` raises `ValueError` otherwise |
| Scope cannot be widened mid-request | `frozen=True` — any "change" is a new object a caller must deliberately construct |
| Widening to `PLATFORM` is always explicit | `TenantContext.platform()` is a named constructor, never a default |

Constructors: `TenantContext.for_tenant(tenant_id)` and `TenantContext.platform()`.

Derived properties:

- `is_bound` → `True` when `scope is TENANT`. This is the signal the guard and
  the executor branch on: **bound ⇒ inject a predicate on every scoped table;
  unbound ⇒ inject nothing.**
- `allows_question(n)` → `False` when the session is `TENANT` and `n` is one of
  the cross-tenant graded questions (`config.CROSS_TENANT_QUESTIONS = {1, 2, 7, 8}`);
  `True` for every question in a `PLATFORM` session. See §7.

---

## 3. How the tenant is established and propagated

There is no HTTP request and no middleware. The tenant is bound interactively and
then threaded, unchanged, through every call.

### Binding (`src/agent/conversation.py`, `src/agent/router.py`)

`Conversation` holds the session's `context` (default `TenantContext.platform()`)
and one piece of pending state: `pending_tenant`.

```
user: "use CFS"
  → Conversation._bind_tenant
    → Router.resolve_tenant("CFS")
      → TenantResolver.resolve("CFS")           # the cascade — see §4
```

The resolver's outcome decides what happens:

| Resolver outcome | `RouterResponse.kind` | Effect on the session |
|---|---|---|
| Exact canonical name / exact curated alias | `ANSWER` | `context = TenantContext.for_tenant(id)` — bound immediately |
| Normalised or fuzzy match (`needs_confirmation`) | `CONFIRM` | **Nothing is bound.** `pending_tenant` is armed; the session stays on its previous scope |
| Ambiguous (≥2 tenants match) | `CLARIFY` | Nothing bound; ranked candidates returned |
| Unresolved | `CLARIFY` | Nothing bound; nearest suggestions (below the confidence bar) returned |

The **confirmation gate** is a security control, and it lives in `Conversation`
once so both transports inherit it rather than reimplementing it (a control in
two copies eventually disagrees with itself — D-018). On the turn after a
`CONFIRM`:

- `Conversation.handle` checks `pending_tenant` **before any command** — an
  outstanding "did you mean X?" consumes the next utterance whatever it is.
- Only an exact affirmative (`Conversation.AFFIRMATIVES` — `yes`, `y`, `yeah`,
  `yep`, `yup`, `correct`, `that's right`, `thats right`) binds
  `TenantContext.for_tenant(pending)`. Anything else —
  including silence, a new question, or "no" — cancels and leaves scope
  unchanged. A loop that re-asks until it hears "yes" is a loop that eventually
  gets one by accident, so an unrecognised reply cancels rather than re-prompts.

This matters most over voice, where speech-to-text produces exactly these
near-miss company names.

### Propagation

```
Conversation.context
  → Router.route(text, context)
    → SqlAgent.answer(question, context)
      → QueryExecutor.run(sql, context)
        → SqlGuard.check(sql, context)          # reads context.is_bound / context.tenant_id
        → QueryExecutor._assert_no_foreign_tenant(result, context)
```

The same frozen object is passed down the whole stack. No layer reconstructs it,
and no layer can mutate it.

---

## 4. The first isolation boundary — entity resolution

`src/data/resolver.py`. Everything downstream — including the guard — trusts the
integer this returns; a wrong answer here is a cross-tenant leak no later layer
can catch. It **fails closed** (D-003). Cascade, cheapest and most certain first:

| Step | Match | Confidence | On success |
|---|---|---|---|
| 1 | Exact canonical name | 100 | resolve |
| 2 | Exact entry in the curated alias table | 100 | resolve *iff exactly one tenant claims it*, else `AMBIGUOUS` |
| 3 | Exact after `normalize()` (lowercase, strip legal suffixes, depunctuate, collapse whitespace) | 99 | resolve iff single claimant; sets `needs_confirmation` |
| 4 | Fuzzy — `rapidfuzz.token_set_ratio ≥ 88` | score | see below; sets `needs_confirmation` |
| 5 | Nothing cleared the bar | 0 | `UNRESOLVED` + nearest suggestions above floor 50 (never auto-selected) |

**The fuzzy gate is a count, not a score** (D-003). `token_set_ratio` scores any
token subset as a perfect 100 — the probe `"Fuel"` scores 100 against both
*Cascade Fuel Services* and *Great Lakes Fuel Co*. A `max(score)` resolver would
answer `"Fuel"` with one tenant at full confidence and leak its data. So the
score decides *which tenants are in the candidate set*, and the **size of that
set** decides whether to answer: one tenant above the line → resolved; two or
more → refuse and list them, however high the scores.

`needs_confirmation` (steps 3–4) is what arms the confirmation gate in §3. The
lookup index is built once (`@lru_cache`), with canonical names seeded into the
alias table so a company resolves whether or not someone listed it as its own
alias.

---

## 5. Layer 1 — read-only connection

`src/db/connection.py`. The database physically cannot be written through this
connection, so even a hallucinated `DELETE` that somehow passed the guard is
refused by SQLite itself.

```python
if not db_path.exists():
    raise DatabaseUnavailableError(...)          # a plain path would CREATE an empty db
connection = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True, check_same_thread=False)
connection.execute("PRAGMA query_only = ON;")
```

| Independent mechanism | Effect |
|---|---|
| `mode=ro` URI flag | The OS layer opens the file read-only |
| `PRAGMA query_only = ON` | The connection refuses write statements |
| Existence check first | A missing file errors loudly instead of silently opening an empty database |

| Catches | Blind to |
|---|---|
| Any write, including one the guard missed | Cross-tenant *reads* — a `SELECT` is still a `SELECT` |

Layers 1 and 2 are independent: a write cannot execute even if the guard is
bypassed entirely.

---

## 6. Layer 2 — the AST guard

`src/db/guard.py`. `SqlGuard.check(sql, context) -> GuardResult`. Parses,
validates, rewrites. **Never raises on bad SQL** — returns
`GuardResult(allowed=False, reasons=(...))`, because a refusal is a normal
conversational outcome the agent has to explain.

### 6.1 Parse

```python
statements = sqlglot.parse(sql, dialect="sqlite")   # parse, NOT parse_one
```

`parse_one` silently keeps only the first statement, so
`SELECT 1; DROP TABLE trucks` would validate as a clean SELECT. `parse` returns
the list; the guard rejects anything that is not **exactly one** statement.

### 6.2 Validate — rejection list

A statement is rejected (before it runs, with a reason string) if any of:

| Rule | Rejects |
|---|---|
| Not an `exp.Select` | `INSERT` / `UPDATE` / `DELETE` / DDL as the top statement |
| Any `FORBIDDEN_NODES` anywhere in the tree | `Insert, Update, Delete, Drop, Create, Alter, Command, Transaction, Commit, Rollback, Into`. `Command` is the catch-all `sqlglot` parses bare verbs into (stops `PRAGMA query_only = OFF` and `ATTACH DATABASE …`). `Into` catches `SELECT * INTO backup FROM t` (which stays rooted at `exp.Select` and would otherwise slip past the root SELECT check). |
| Table name starts with `sqlite_` | Reads of SQLite's internal catalogue (schema disclosure) |
| Table not in `config.TENANT_SCOPED_TABLES` | Anything outside `{customers, drivers, trucks, delivery_orders, shifts, tank_readings}` |
| Schema qualifier present and `≠ main` | Cross-database references (`otherdb.delivery_orders`) — belt to the `ATTACH` braces |
| A SELECT references no known table at all | Degenerate / probing queries |

CTE names are collected first (`statement.find_all(exp.CTE)`) and are **exempt**
from the table allowlist — they look like tables in a `FROM` but are not real
ones.

### 6.3 Rewrite — tenant predicate injection

Only when `context.is_bound`. `_inject_tenant_predicates`:

```python
for select in statement.find_all(exp.Select):        # EVERY scope: outer, each CTE body, each subquery
    for table in self._direct_sources(select):       # only FROM/JOIN of THIS select — does not descend
        name = self._canonical_table_name(table)     # table.name.lower() — the ONE normalisation point
        if name in cte_names or name not in allowlist:
            continue
        qualifier = table.alias_or_name              # "FROM delivery_orders o" → "o.tenant_id"
        predicate = exp.EQ(this=exp.column("tenant_id", table=qualifier),
                           expression=exp.Literal.number(tenant_id))
        select.where(predicate, append=True, copy=False)   # AND onto any existing WHERE, in place
```

Three properties this design guarantees:

- **Per-scope filtering.** Because every nested SELECT is visited in its own
  right, a subquery is filtered by *its own* `WHERE`, not the outer one. This is
  what makes correlated subqueries and derived tables safe.
- **`_direct_sources` never descends.** It reads `select.args.values()` and picks
  out `From`/`Join` nodes by *type* — it deliberately does not call
  `select.find_all(exp.Table)`, which would make an outer SELECT try to filter a
  table that only exists inside a subquery. It also never indexes the FROM
  argument by the key name `"from"`: `sqlglot` 30 renamed it to `from_`, and the
  name-indexed version failed **silently** — no tables found, no predicates
  injected, a syntactically perfect query with no tenant filter (D-005). The
  guard does not depend on a key name it cannot verify.
- **One normalisation point.** `_canonical_table_name` (`table.name.lower()`) is
  the only place identifiers are cased. The allowlist check and the injection
  pass both go through it, so they can never disagree about whether
  `DELIVERY_ORDERS` is the allowlisted `delivery_orders` — a disagreement in the
  direction of "allow but do not inject" would produce an approved, unfiltered
  query.

A caller-supplied `WHERE tenant_id = 7` is **ANDed, not replaced**: a
cross-tenant attempt becomes `tenant_id = 7 AND tenant_id = 4` and returns zero
rows.

### 6.4 Row-count ceiling

`_enforce_limit`: a missing `LIMIT` is added at `config.MAX_RESULT_ROWS` (200); a
larger literal `LIMIT` is lowered to it; a smaller one is left alone (the model
asking for the top 5 drivers should get 5); a non-literal `LIMIT` is replaced
outright.

### 6.5 Result

```python
GuardResult(allowed=True,
            rewritten_sql=statement.sql(dialect="sqlite"),
            tables=frozenset(referenced),
            injected_predicates=injected)
```

| Catches | Blind to |
|---|---|
| Scope, table access, forbidden statements, missing predicates | Its own bugs — which is why layer 3 exists |

---

## 7. Cross-tenant question refusal (authority, not row scoping)

Row scoping (§6.3) filters *rows*. It does not stop a scoped session from *asking*
a question that only makes sense platform-wide — filtered, "which tenant
delivered the most gallons?" collapses to one group and returns that tenant's
number dressed as a ranking. So the question itself is refused, checked twice
from independent inputs (D-008), in `SqlAgent._authority_check`:

```python
if not context.is_bound:
    return []                                    # PLATFORM session — allowed
if generation.is_cross_tenant:                   # the model's flag: reads the QUESTION
    reasons.append("That question compares tenants …")
elif self._looks_cross_tenant(generation.sql):   # structural: GROUP BY / ORDER BY on tenant_id
    reasons.append("The query groups or ranks by tenant …")
```

Selecting `tenant_id` in the projection is *not* treated as cross-tenant —
echoing the bound tenant back is harmless. Grouping or ordering by it is.

The router applies the same principle to the **JSON side**: a scoped session
triaging a ticket that belongs to another tenant gets the *same* "I can't find
ticket #N" message as a genuinely missing ticket. Distinguishing them turned the
tool into an enumeration oracle — ticket ids are sequential four-digit integers,
so a scoped user could map every id in use across the platform by sorting replies
into "refused" and "not found" (F3 in [security-review.md](../../SECURITY.md)).

---

## 8. Layer 3 — post-execution row assertion

`src/db/executor.py`. Acts *after* execution and reasons about the data that came
back — the only layer that can catch a **guard bug**: a predicate on the wrong
alias, a scope the traversal missed, a `sqlglot` upgrade that changes a node
shape.

```python
def _assert_no_foreign_tenant(self, result, context):
    if not context.is_bound or config.TENANT_COLUMN not in result.columns:
        return                                   # nothing to check (e.g. SELECT COUNT(*))
    index = result.columns.index(config.TENANT_COLUMN)
    foreign = {row[index] for row in result.rows if row[index] != context.tenant_id}
    if foreign:
        raise TenantIsolationError(...)          # RAISED — never returned, never logged-and-continued
```

`TenantIsolationError` is a defect report, not a user error. If it fires, the
guard has a hole and the only safe action is to fail the request loudly.

The executor also enforces the runtime budget and makes unguarded execution
inexpressible:

- `execute_approved` takes a `GuardResult`, not a `str` — "run some SQL" is not a
  callable operation without a verdict in hand.
- A `sqlite3` progress handler aborts the statement if it outlives
  `config.QUERY_TIMEOUT_SECONDS` (5.0) → `QueryTimeoutError`.
- `fetchmany(max_rows + 1)` detects truncation without pulling a runaway result
  set into memory.

| Catches | Blind to |
|---|---|
| A guard bug, visible in real returned rows | Leaks in aggregates (no `tenant_id` column to inspect); leaks that stay under the row cap |

**Known ceiling:** layer 3 is a *detector*, not a guarantee — it is pinned by
`test_the_row_assertion_is_a_detector_not_a_guarantee`.
[open-questions.md](../project/open-questions.md) Q-010 argues against adding a fourth
layer to close the gap.

---

## 9. The three layers together

| Layer | Enforced by | Runs | Catches | Blind to |
|---|---|---|---|---|
| 1 · read-only connection | SQLite (`mode=ro`, `query_only`) | on connect | any write, even one the guard missed | cross-tenant reads |
| 2 · AST guard | our code (`sqlglot`) | before execution | scope, table access, forbidden statements, missing predicates | its own bugs |
| 3 · row assertion | our code | after execution | a guard bug, in real rows | aggregates; leaks under the row cap |

Each layer assumes the others may fail. The `sqlglot`-30 incident is the proof
that this is not paranoia: layer 2 failed completely and silently, and only
layers 1 and 3 stood between that and a leak. See
[architecture-decisions.md §2](../explanation/architecture-decisions.md) and D-004.

---

## 10. JSON-side isolation

The dispatch DB is not the only source. `customers.json`, `tickets.json`,
`call_transcripts.json` are filtered in exactly one place:
`Repository._index_by_tenant` (`src/data/repository.py`). Agent code never opens a
file, never writes its own tenant filter, and never learns that call transcripts
are keyed by tenant *name* (resolved to an id at load time via the same
`TenantResolver`). One place to audit for the JSON half; `src/db/guard.py` is the
SQL half.

---

## 11. Deploying as a service

**Not implemented in this repository** — the agent ships as two CLI transports.
This section states how the guarantee maps onto FastAPI, because the isolation
core (`TenantContext` → `SqlGuard` → `QueryExecutor`) is transport-agnostic by
design and would be reused unchanged. The worked version, with the three
vulnerabilities it closes, is in [security-review.md](../../SECURITY.md).

```python
from fastapi import Depends, FastAPI, HTTPException

@app.post("/api/query")
async def query_dispatch(
    body: QueryRequest,                                   # Pydantic; `question` required, typed
    principal: Principal = Depends(require_authenticated_user),
) -> QueryResponse:
    # The tenant comes from the verified session — NOT from the request body.
    # There is no tenant_id field on QueryRequest, so "ask about someone else"
    # is not an expressible request.
    context = TenantContext.for_tenant(principal.tenant_id)

    answer = SqlAgent(LLMClient()).answer(body.question, context)
    if answer.refused:
        raise HTTPException(status_code=400, detail=list(answer.refusal_reasons))
    return QueryResponse(
        answer=answer.answer,
        row_count=answer.row_count,
        rows=answer.as_dicts(),
        sql=answer.sql if principal.is_internal else None,   # executed SQL is schema disclosure to a tenant user
        window_end=answer.date_anchor,
    )
```

Rules for the service boundary:

| Rule | Reason |
|---|---|
| The tenant is a **dependency-injected** value derived from the verified session/JWT, never a request field | A caller-supplied `tenant_id` is horizontal privilege escalation (security-review.md V1) |
| `QueryRequest` has **no** `tenant_id` field | The parameter an attacker would tamper with should not exist |
| The `PLATFORM` scope is reachable only for `principal.is_internal`, never from an end-customer or tenant path | `PLATFORM` disables predicate injection entirely |
| `SessionScope` is currently self-asserted by the caller of `Conversation` | Acceptable for a CLI; a service must derive it from the principal (F1 in security-review.md, open by design) |
| Refusal reasons are safe to return; the executed SQL is not (except to internal operators) | Reasons describe policy; SQL describes schema |
| One `TenantContext` per request, constructed at the boundary and passed down | Matches the CLI: the frozen object is the contract, not the transport |

---

## 12. Test coverage

| File | Asserts |
|---|---|
| `tests/test_tenant_isolation.py` (15) | A tenant-A query never returns tenant-B rows; predicates injected into subqueries, derived tables, CTE bodies; the **deliberately-bypassed-guard** test that proves layers 1 and 3 still hold; layer 3 is a detector, not a guarantee |
| `tests/test_security.py` (18) | The three [security-review.md](../../SECURITY.md) fixes; guard rejections — multi-statement, non-SELECT, `sqlite_*`, off-allowlist, `ATTACH`/`PRAGMA` via `Command`, cross-database |
| `tests/test_entity_resolution.py` (10) | The cascade; fuzzy gate on candidate *count* not score; refusal on ambiguity; the nearest-suggestion floor |
| `tests/test_conversation.py` (14) | Scope transitions; the confirmation gate binds only on an affirmative and cancels on anything else |
