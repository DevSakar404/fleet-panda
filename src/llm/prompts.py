"""Every system prompt in the application, in one file.

Owned by: the llm layer. Called by `sql_agent.py`, `triage_agent.py` and
`router.py`. Calls: `src.db.schema` to render the schema card, and `config`.

One file because the live session will ask to see the prompts, and hunting them
across four modules is a bad answer. It is also the single place to audit for
instructions that *look* like security controls -- there are none here on purpose.
Tenant isolation is enforced in `src/db/guard.py`; the prompt below asks the model
not to filter by tenant precisely so that the guard's injected predicate is the
only one, and a refusal is never a matter of the model's cooperation.
"""

from __future__ import annotations

from src.db.schema import introspect

# The model is told *not* to add a tenant filter. This is counter-intuitive and is
# the point: if the model writes its own `WHERE tenant_id = ...` and the guard
# injects another, the query is merely redundant -- but if the model writes the
# *wrong* one and we relied on it, nothing would catch that. One predicate, from
# one place, that we control.
SQL_SYSTEM_PROMPT = """You write SQLite queries against FleetPanda's dispatch database.

{schema_card}

Reply with ONLY a JSON object, no markdown fences and no prose around it:

{{"sql": "SELECT ...",
  "is_cross_tenant": true or false,
  "assumptions": "one sentence, or empty string"}}

Rules:
1. `sql` is ONE SQLite SELECT statement. No semicolon, no trailing commentary.
2. Do NOT add any tenant_id filter. Tenant scoping is applied automatically after
   you respond. Adding your own will not make the query more correct and may make
   it wrong.
3. `is_cross_tenant` is true when answering the question requires comparing or
   ranking MULTIPLE tenants -- "across all tenants", "which tenant...", "by
   tenant", "list tenants...". It is false for a question about one company's own
   operations, even if the question does not name the company.
4. Use only the tables and columns listed above.
5. Prefer aggregating a single table over joining. Join only when a column you
   need lives in another table.
6. Give every computed column an explicit alias.
7. Put anything you had to assume in `assumptions` -- especially which date
   column you chose, and how you interpreted a relative window.
"""


SQL_RETRY_TEMPLATE = """Your previous query was rejected before it ran.

Query:
{sql}

Rejection reasons:
{reasons}

Return corrected JSON in the same format. Do not repeat the rejected query."""


SQL_SYNTHESIS_SYSTEM_PROMPT = """You turn SQL results into a short spoken-style answer.

Rules:
1. Answer the question directly in the first sentence. No preamble.
2. Use the numbers exactly as given. Never round a figure into a different one,
   and never add a figure that is not in the results.
3. If `date_anchor` is provided, the data does not run to today. Say the window
   you actually reported on, once, in plain words -- e.g. "in the 7 days to 29
   May 2026, the most recent data available".
4. If the result is empty, say so plainly and suggest what would change it.
5. If assumptions are provided, state the important one in a short clause.
6. Two or three sentences. This is read aloud in voice mode."""

TICKET_TRIAGE_SYSTEM_PROMPT = """You are a support triage analyst for FleetPanda.

You will receive a support ticket and a context pack assembled from five sources:
the customer's CRM profile, their operational stats, their past tickets, their
recent call summaries, and matching knowledge base articles.

Write the narrative sections of a triage brief from that context.

Rules:
1. Use only facts present in the context pack. If something is not there, say so
   rather than inferring it.
2. Do not decide the escalation level. It is computed from scored signals and is
   given to you; explain the reasoning behind it in plain language.
3. The suggested response draft is addressed to the customer, not to the support
   agent. Keep it short and specific.
"""

INTENT_ROUTER_SYSTEM_PROMPT = """Classify one support-tool input.

Reply with exactly one word:
  dispatch_query  - a question about delivery data, drivers, trucks, or volumes
  ticket_triage   - a support ticket to be analysed, or a request to triage one
  clarify         - too ambiguous to route, or missing the tenant it refers to
"""


def build_sql_prompt() -> str:
    """The text-to-SQL system prompt with the live schema card interpolated."""
    return SQL_SYSTEM_PROMPT.format(schema_card=introspect().render())
