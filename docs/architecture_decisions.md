# Architecture Decisions — the "why"

← [README](../README.md) · Reference specs: [tenant isolation](specs/tenant_isolation_spec.md) · [ticket triage](specs/ticket_triage_agent_spec.md)

This is the **narrative digest** of the design. It explains the reasoning and the
trade-offs behind the load-bearing choices. The dated, per-decision journal —
context, options, chosen option, trade-off accepted, and the file it lives in —
is [`DECISIONS.md`](../DECISIONS.md) (entries D-001 … D-021). Decision numbers
below link to it.

---

## 1. No agent framework

**Choice:** direct provider SDK calls (`anthropic`, `openai`), no LangChain /
LlamaIndex / CrewAI / LangGraph.

The two things a reviewer needs to interrogate in this system are the *prompts*
and the *control flow* around them. A framework's value proposition is hiding
exactly those behind an abstraction. The whole LLM surface here is one 122-line
file (`src/llm/client.py`, one `complete()` method, one branch for the second
provider) and one prompt file (`src/llm/prompts.py`, every system prompt in one
place). There is no retry-orchestration layer, no tool-router object, no chain —
the control flow is plain Python `for attempt in range(...)` in `sql_agent.py`
that a reader can follow top to bottom.

**Trade-off accepted:** we hand-write things a framework provides (JSON-from-model
parsing with fence stripping, the retry loop, the two-call split). That is ~40
lines total, and each line is one we can explain and test.

---

## 2. Multi-tenant SQL isolation is an AST rewrite, not a prompt and not a regex

This is the core security decision. [D-004](../DECISIONS.md), [D-005](../DECISIONS.md).

A tenant-scoped session must never see another tenant's rows. The generated SQL
is untrusted (the model can be wrong; the model can be prompt-injected). Three
ways to enforce the constraint were on the table:

| Approach | Why it fails as a *control* |
|---|---|
| **Prompt instruction** ("only query tenant N") | A prompt is a request. The model can ignore it — through error or injection — and nothing downstream would know. It is not verifiable. |
| **Regex / string rewrite** over the SQL text | A regex cannot see scope. It cannot distinguish `FROM delivery_orders` in an outer query from the same string inside an already-filtered subquery, and it cannot reliably find the `WHERE` clause it needs to extend. It breaks on the first correlated subquery or CTE. |
| **AST parse + rewrite** (chosen) | Parsing to a tree makes "every tenant-scoped table reference carries a tenant predicate" a statement we can *walk the tree and verify*. |

**How the chosen approach works** (`src/db/guard.py`, full detail in the
[isolation spec](specs/tenant_isolation_spec.md)):

1. Parse with `sqlglot.parse(sql, dialect="sqlite")` — `parse`, not `parse_one`,
   so a smuggled second statement (`SELECT 1; DROP TABLE trucks`) is visible and
   rejected on statement count rather than silently dropped.
2. Validate against an allowlist: SELECT only; no DDL/DML nodes anywhere in the
   tree; no `sqlite_*` internal tables; no cross-database references; every table
   named must be one of six known tenant-scoped tables.
3. Rewrite: `statement.find_all(exp.Select)` visits **every** SELECT scope — the
   outer query, each CTE body, each subquery — and appends
   `<table>.tenant_id = <session tenant>` to *that scope's own* `WHERE`. Nested
   scopes are filtered by their own predicate, which is what makes derived tables
   and correlated subqueries safe.

**The incident that justifies the layered design.** During the build, `sqlglot`
30 renamed the `Select` node's `from` argument to `from_`. Code that reached the
FROM clause by indexing `select.args["from"]` by name started returning nothing —
so the injection pass found no tables, added no predicates, and emitted
**syntactically perfect, entirely unfiltered SQL**. A guard whose failure mode is
"silently allows everything" is worse than no guard. Two consequences:

- The guard now reads `select.args.values()` and picks out `From`/`Join` nodes by
  *type*, never by a key name it cannot verify (`_direct_sources`, D-005).
- The guard is **layer 2 of 3**. Layer 1 (a read-only connection) and layer 3 (a
  post-execution assertion that no returned row carries a foreign `tenant_id`)
  are positioned precisely so that a total failure of layer 2 is still contained.
  Each layer assumes the others may fail. [D-004](../DECISIONS.md).

**Trade-off accepted:** we take a hard dependency on `sqlglot`'s SQLite dialect
coverage and pin the version. A query `sqlglot` cannot parse is refused, not
executed — correct, but it means parser coverage is a functional limit. Layer 3
only inspects a `tenant_id` column when the query projects one; an aggregate like
`SELECT COUNT(*)` has nothing for it to check, so layer 3 is a *detector*, not a
guarantee, and the test suite pins that ceiling rather than papering over it.

---

## 3. The triage context pipeline is a structured join with a deterministic score — not RAG

[D-013](../DECISIONS.md), [D-010](../DECISIONS.md), [D-014](../DECISIONS.md).

"Pull the customer's full context and produce a brief" reads like a
retrieval-augmented-generation problem. It is not, for this data:

- **The knowledge base is 12 articles.** Every article and every ticket carries a
  literal `product_area` from the same controlled vocabulary. Matching an article
  to a ticket is a *join on a shared key with a tie-break*, not a semantic-search
  problem. An embedding index over 12 rows is infrastructure (a vector store, an
  embedding API call on the request path, a similarity threshold to tune) bought
  for nothing.
- **KB retrieval** (`find_kb_articles`): score = 10 for a `product_area` match,
  +4 per overlapping symptom token, +2 for a title token overlap; an article must
  clear a floor of 10 to appear at all; ties break on recency; top 3 returned.
  The floor is what makes "no article covers this" a possible, honest answer —
  before it existed, `billing` tickets (the one area with zero KB coverage) were
  served whatever article shared a word with them.
- **The other four sources are exact lookups**, not retrieval: the customer
  profile, this tenant's past tickets, this tenant's call transcripts, and a
  four-query operational snapshot from the dispatch DB. All are tenant-scoped
  through the one repository.

**The structure that matters is the fan-in, and the split inside it:**

```
ticket
  ├── gather()   five sources → TicketContext          deterministic, no LLM
  ├── score()    TicketContext → EscalationAssessment   deterministic, no LLM
  ├── narrate()  both → three prose sections            LLM — no arithmetic
  └── TicketBrief
```

Everything decidable is decided before the model is called, and the model is
handed the decision rather than asked for it. That is what makes "why was this
escalated?" answerable from `assessment.signals` — an audit trail of
(name, points, sentence) tuples — without re-running anything.

**Trade-off accepted:** keyword-plus-`product_area` retrieval will miss an
article that describes the same failure in different words. At 12 articles a CSM
can skim the whole KB; at 500 articles this would need revisiting, and
[D-013](../DECISIONS.md) says so explicitly. The escalation weights are a
first-pass calibration against a 12-tenant roster and are flagged for human
review (Q-002, Q-005, Q-014 in [OPEN_QUESTIONS.md](../OPEN_QUESTIONS.md)).

---

## 4. Escalation is additive points in Python; the LLM only explains it

[D-010](../DECISIONS.md), [D-011](../DECISIONS.md), [D-012](../DECISIONS.md).

A model asked to weigh health score against CARR against contract proximity gives
a different answer on a different day. "Why was this ticket escalated?" has to be
answerable, identically, every time. So escalation level is
`sum(signal.points)` bucketed against fixed thresholds, computed by a pure
function with no network, no database, and no clock unless one is injected. The
LLM receives the level and writes the paragraph explaining it.

Two refinements the calibration forced:

- **`today` is injected** ([D-011](../DECISIONS.md)). Contract dates are
  forward-looking facts on the real calendar; they must not drift with the
  dataset's 91-day staleness. This is the *second clock* — see §5.
- **Account-state points are capped** at `URGENT + 10` ([D-012](../DECISIONS.md)).
  Uncapped, account signals reach 95 points while ticket signals reach 35, so
  every ticket from a struggling tenant scored CRITICAL and the level stopped
  ranking anything — all twelve of one tenant's tickets were identical. The cap
  makes the rule one sentence: *a bad account is URGENT on its own; CRITICAL
  additionally requires something about this specific ticket* (a repeat filing,
  an entitlement gap, a stated urgency).

---

## 5. Two clocks, on purpose

[D-001](../DECISIONS.md), [D-011](../DECISIONS.md).

The dispatch data ends 2026-05-29. Anchoring "last 30 days" on `date('now')`
returns zero rows and reports every tenant as having stopped delivering.

- **Dispatch queries and the triage operational snapshot** anchor relative
  windows on `(SELECT MAX(delivery_date) FROM delivery_orders)`. The agent states
  the anchor in its reply ("in the 7 days to 29 May 2026, the most recent data
  available"), and `SqlAnswer` carries the window as machine-readable data so a
  downstream consumer is not left parsing English to learn the numbers are stale.
- **Escalation contract proximity** runs on the real calendar, because a renewal
  date is a real future date.

**Trade-off accepted:** a reader must know which clock a given number is on. The
mitigation is that the anchor is always stated, never implied.

---

## 6. Text-to-SQL is two LLM calls, split so the prose call cannot compute

[D-007](../DECISIONS.md), [D-008](../DECISIONS.md), [D-009](../DECISIONS.md).

Call one turns a question into SQL and is the only step allowed to be creative.
Call two turns the returned rows into two or three sentences and is given no
ability to compute anything — it receives the numbers and is told to use them
verbatim and invent none. Nothing between the two trusts either.

The cross-tenant refusal for a scoped session is checked **twice, from two
independent inputs** ([D-008](../DECISIONS.md)): once from the model's own
`is_cross_tenant` flag (which reads the *question* and catches "compare us to the
others" intent the SQL might not show), and once structurally from the generated
SQL (`GROUP BY` / `ORDER BY` on `tenant_id` means the query is shaped as a
ranking). Either firing is enough to refuse.

---

## Trade-offs, at a glance

| Decision | We gain | We accept |
|---|---|---|
| No framework | Prompts and control flow are readable and testable | ~40 lines of hand-written plumbing |
| AST guard for isolation | A verifiable tenant predicate on every SELECT scope | A hard `sqlglot` version dependency; unparseable SQL is refused |
| Three isolation layers | A layer-2 failure is still contained | Layer 3 cannot inspect aggregates — it is a detector, not a proof |
| Structured join, not RAG, for KB | No vector store; auditable ranking; honest "no match" | Misses semantically-similar articles worded differently (fine at n=12) |
| Deterministic escalation | Reproducible, explainable-from-code decisions | Weights are a first-pass calibration pending human review |
| Two clocks | Non-empty answers on stale data; correct contract math | The reader must track which clock a number is on |
| Two-call SQL split | The step that emits numbers cannot fabricate them | Two round trips per question (latency budget noted in [D-019](../DECISIONS.md)) |

For the cost model, the 150-tenant scaling analysis, and the two-layer
end-customer isolation design, see the back half of [`DECISIONS.md`](../DECISIONS.md).
