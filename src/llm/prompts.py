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

from typing import TYPE_CHECKING

from src import config
from src.db.schema import introspect

if TYPE_CHECKING:  # import cycle: the agent imports prompts, not the other way round
    from src.agent.escalation import EscalationAssessment
    from src.agent.triage_agent import TicketContext

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
8. Window boundaries. A SINGLE window -- "the last 7 days", "the past 30 days" --
   is INCLUSIVE of its edge: use `>= date(<anchor>, '-N day')`. Using `>` there
   silently drops a day's rows.
   When comparing TWO ADJACENT 30-day windows of delivery volume (last 30 days vs previous 30 days):
   In a CTE `WITH windows AS (...)`, select `tenant_id` and compute counts directly on `delivery_orders WHERE status = 'completed' GROUP BY tenant_id`:
     `SUM(CASE WHEN delivery_date > date(<anchor>, '-30 day') THEN 1 ELSE 0 END) AS recent`
     `SUM(CASE WHEN delivery_date > date(<anchor>, '-60 day') AND delivery_date <= date(<anchor>, '-30 day') THEN 1 ELSE 0 END) AS prior`
   In the main SELECT from `windows`, select `tenant_id` and filter:
     `WHERE prior > 0 AND 100.0 * (recent - prior) / prior < -{decline_threshold}`.

9.  "Last month" is the last COMPLETE CALENDAR month, not a rolling 30 days and
    not the anchor's own month -- the data stops part-way through the anchor month,
    so that one is partial and would undercount. It is the month BEFORE the anchor's
    month. Select it as:
      `strftime('%Y-%m', delivery_date) =
       strftime('%Y-%m', date(<anchor>, 'start of month', '-1 month'))`
10. "By tenant" means ONE ROW PER TENANT: `GROUP BY tenant_id` and nothing else.
    Adding `customer_id` to the grouping answers a different question -- there are
    114 end-customers and 12 tenants.
11. When asked specifically for "gallons" (e.g. "most gallons of diesel"), aggregate with
    `SUM(gallons_delivered)`. When asked for "delivery volume" or "deliveries", use `COUNT(*)`.
    Say which you used in `assumptions`.
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


def build_triage_payload(context: "TicketContext", assessment: "EscalationAssessment") -> dict:
    """Flatten a context pack and an escalation verdict into the triage prompt body.

    Lives here rather than in `triage_agent.py` for two reasons. It is prompt
    content -- what the model is shown is exactly as much a prompt decision as the
    system message above it, and this file is where prompt decisions are read
    during a walkthrough. And it kept `triage_agent.py` over the ~350-line ceiling
    in CLAUDE.md section 6 (D-015).

    Note what is included: the escalation level and its reasons are passed IN. The
    model explains the decision; it does not make it.
    """
    return {
        "ticket": {
            "id": context.ticket.ticket_id,
            "subject": context.ticket.subject,
            "description": context.ticket.description,
            "product_area": context.ticket.product_area,
            "priority": context.ticket.priority,
            "status": context.ticket.status,
            "created_at": str(context.ticket.created_at),
        },
        "customer": {
            "name": context.tenant.name,
            "health_score": context.tenant.health_score,
            "carr": context.tenant.carr,
            "contract_end_date": str(context.tenant.contract_end_date),
            "assigned_csm": context.tenant.assigned_csm,
            "modules_active": sorted(context.tenant.modules_active),
            "onboarding_status": context.tenant.onboarding_status,
        },
        "escalation": {
            "level": assessment.level.value,
            "score": assessment.score,
            "account_risk": assessment.account_risk,
            "ticket_risk": assessment.ticket_risk,
            "reasons": list(assessment.reasons),
            "missing_module": assessment.missing_module,
        },
        "duplicates": [
            {"id": t.ticket_id, "created_at": str(t.created_at), "status": t.status}
            for t in context.duplicates
        ],
        "past_tickets": [
            {"id": t.ticket_id, "subject": t.subject, "status": t.status,
             "resolution": t.resolution}
            for t in context.past_tickets
        ],
        "calls": [
            {"date": str(c.call_date), "topic": c.topic, "sentiment": c.sentiment,
             "competitor_mentioned": c.competitor_mentioned,
             "action_items": list(c.action_items)}
            for c in context.calls
        ],
        "kb_articles": [
            {"id": a.article_id, "title": a.title, "root_cause": a.root_cause,
             "resolution": a.resolution, "updated_at": str(a.updated_at)}
            for a in context.kb_articles
        ],
        "operations": {
            "completed_last_30": context.operations.completed_last_30,
            "completed_prior_30": context.operations.completed_prior_30,
            "volume_change_pct": context.operations.volume_change_pct,
            "emergency_last_30": context.operations.emergency_last_30,
            "open_orders": context.operations.open_orders,
            "anchor_date": context.operations.anchor_date,
        },
        "sources_with_no_data": list(context.missing_sources),
    }


def build_sql_prompt() -> str:
    """The text-to-SQL system prompt with the live schema card interpolated.

    The decline threshold is interpolated rather than written into the prompt
    text, so `config.DECLINE_THRESHOLD_PCT` stays the single place it is defined
    (CLAUDE.md 5). It reached the prompt only after the first live run: the value
    existed in config and in the tests, but nothing ever told the model about it,
    so it flagged every tenant with any decline at all -- eleven of twelve.
    """
    return SQL_SYSTEM_PROMPT.format(
        schema_card=introspect().render(),
        decline_threshold=abs(config.DECLINE_THRESHOLD_PCT),
    )
