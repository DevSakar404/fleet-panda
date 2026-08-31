"""The eight graded questions.

Two halves, both parametrized over the same question set:

  1. `test_reference_sql_*` -- hand-written reference SQL, run through the real
     guard and executor, asserted against numbers computed during Step 0 recon.
     These pass now. They are the oracle: in Step 4 the agent's generated SQL is
     compared against these answers, so the correctness target exists before the
     agent that has to hit it.

  2. `test_agent_answers_*` -- the end-to-end path through the LLM. Skipped until
     Step 4 builds it. Each carries the ambiguity that has to be resolved before
     its answer can be called correct.

Splitting them this way means a Step 4 failure is immediately diagnosable: if the
reference test passes and the agent test fails, the SQL generation is wrong; if
both fail, something under them broke.
"""

from __future__ import annotations

import os

import pytest

from src import config
from src.agent.session import TenantContext
from tests.conftest import FakeLLM, sql_reply

# Anchored on the data rather than on `date('now')`: the dataset ends 2026-05-29,
# 91 days before this was written, so `date('now')` returns zero rows for four of
# these eight questions. See decisions-log.md D-001.
ANCHOR = "(SELECT MAX(delivery_date) FROM delivery_orders)"


@pytest.fixture(scope="module")
def run(executor):
    """Execute reference SQL through the guard and return the rows."""

    def _run(sql: str, context: TenantContext):
        verdict, result = executor.run(sql, context)
        assert verdict.allowed, verdict.reasons
        return result

    return _run


# --- 1. reference SQL, verified against recon --------------------------------

def test_q1_completed_deliveries_last_7_days(run):
    """Q1. Cross-tenant, so it runs in a PLATFORM session.

    Ambiguity resolved: 'last 7 days' is anchored on MAX(delivery_date), and
    'deliveries' means orders with status='completed' rather than all orders.
    """
    result = run(
        f"SELECT COUNT(*) AS deliveries FROM delivery_orders "
        f"WHERE status = 'completed' AND delivery_date >= date({ANCHOR}, '-7 day')",
        TenantContext.platform(),
    )
    assert result.rows[0][0] == 604


def test_q2_top_diesel_tenant_last_month(run):
    """Q2. Cross-tenant. 'Last month' is the last complete calendar month in the
    data (2026-04), not a rolling 30 days.

    Aggregates delivery_orders alone. Joining tank_readings here would inflate the
    total 9x (recon.md section 5).
    """
    result = run(
        f"SELECT tenant_id, SUM(gallons_delivered) AS gallons FROM delivery_orders "
        f"WHERE product_type = 'diesel' AND status = 'completed' "
        f"AND strftime('%Y-%m', delivery_date) = "
        f"strftime('%Y-%m', date({ANCHOR}, 'start of month', '-1 month')) "
        f"GROUP BY tenant_id ORDER BY gallons DESC",
        TenantContext.platform(),
    )
    assert result.rows[0][0] == 3
    assert round(result.rows[0][1], 1) == 85816.6


def test_q3_top_5_drivers_for_tenant_3(run):
    """Q3. Tenant-scoped. The guard injects a predicate on BOTH delivery_orders
    and drivers; the join is 1:1 so it cannot inflate the count."""
    result = run(
        "SELECT d.name, COUNT(*) AS deliveries FROM delivery_orders o "
        "JOIN drivers d ON o.driver_id = d.driver_id "
        "WHERE o.status = 'completed' "
        "GROUP BY d.driver_id ORDER BY deliveries DESC LIMIT 5",
        TenantContext.for_tenant(3),
    )
    assert result.row_count == 5
    assert result.rows[0] == ("Daryl Williams", 91)


def test_q4_average_gallons_per_propane_delivery(run):
    """Q4. The trap is `status = 'completed'`.

    Without it the average is taken over gallons_ordered-shaped rows where
    gallons_delivered is NULL for 30% of them; SQLite's AVG skips NULLs silently,
    so the number changes meaning rather than erroring. 1467.7 is deliveries;
    1564.92 would be orders. The question says 'per delivery'.
    """
    result = run(
        "SELECT AVG(gallons_delivered) AS avg_gallons FROM delivery_orders "
        "WHERE product_type = 'propane' AND status = 'completed'",
        TenantContext.platform(),
    )
    assert round(result.rows[0][0], 1) == 1467.7


def test_q5_emergency_orders_for_tenant_4(run):
    """Q5. Tenant-scoped, and the tenant predicate is injected rather than
    written -- note the SQL below names no tenant at all."""
    result = run(
        f"SELECT COUNT(*) AS emergency_orders FROM delivery_orders "
        f"WHERE priority = 'emergency' AND order_date >= date({ANCHOR}, '-30 day')",
        TenantContext.for_tenant(4),
    )
    assert result.rows[0][0] == 17


def test_q6_trucks_in_maintenance(run):
    """Q6. The only date-free question, and the one where a tenant-scoped and a
    platform answer differ legitimately: 6 across the platform, fewer per tenant."""
    platform = run(
        "SELECT COUNT(*) AS n FROM trucks WHERE status = 'maintenance'",
        TenantContext.platform(),
    )
    assert platform.rows[0][0] == 6

    scoped = run(
        "SELECT COUNT(*) AS n FROM trucks WHERE status = 'maintenance'",
        TenantContext.for_tenant(3),
    )
    assert scoped.rows[0][0] == 2


def test_q7_fill_rate_by_tenant(run):
    """Q7. Cross-tenant.

    SUM(delivered)/SUM(ordered), not AVG(delivered/ordered) -- the latter weights
    a 10-gallon order the same as a 3000-gallon one. Recon found no overages, no
    nulls among completed orders and no zero denominators, so the only real trap
    is the missing status filter.
    """
    result = run(
        "SELECT tenant_id, SUM(gallons_delivered) / SUM(gallons_ordered) AS fill_rate "
        "FROM delivery_orders WHERE status = 'completed' "
        "GROUP BY tenant_id ORDER BY fill_rate DESC",
        TenantContext.platform(),
    )
    assert result.row_count == 12
    assert result.rows[0][0] == 3
    assert round(result.rows[0][1], 4) == 0.9268
    assert all(0.90 < row[1] < 0.95 for row in result.rows)


def test_q8_tenants_with_declining_volume(run):
    """Q8. Cross-tenant. Two 30-day windows anchored on the data.

    Seven of twelve tenants are technically negative; the materiality threshold
    (config.DECLINE_THRESHOLD_PCT, provisionally -10%) is what makes the answer
    useful rather than exhaustive. See open-questions.md Q-005.
    """
    result = run(
        f"WITH windows AS ("
        f"  SELECT tenant_id,"
        f"    SUM(CASE WHEN delivery_date > date({ANCHOR}, '-30 day') THEN 1 ELSE 0 END) AS recent,"
        f"    SUM(CASE WHEN delivery_date > date({ANCHOR}, '-60 day')"
        f"              AND delivery_date <= date({ANCHOR}, '-30 day') THEN 1 ELSE 0 END) AS prior"
        f"  FROM delivery_orders WHERE status = 'completed' GROUP BY tenant_id)"
        f" SELECT tenant_id, recent, prior,"
        f"        100.0 * (recent - prior) / prior AS pct_change"
        f" FROM windows WHERE prior > 0 ORDER BY pct_change",
        TenantContext.platform(),
    )
    steepest = result.rows[0]
    assert steepest[0] == 4
    assert round(steepest[3], 1) == -16.3

    from src import config

    material = [row[0] for row in result.rows if row[3] < config.DECLINE_THRESHOLD_PCT]
    assert material == [4, 8, 9, 12]


def test_the_date_anchor_matters(run):
    """The finding behind D-001, asserted rather than described.

    If this ever fails because `date('now')` starts returning rows, the dataset
    has been refreshed and the anchoring decision should be revisited.
    """
    now_anchored = run(
        "SELECT COUNT(*) AS n FROM delivery_orders "
        "WHERE status = 'completed' AND delivery_date >= date('now', '-7 day')",
        TenantContext.platform(),
    )
    assert now_anchored.rows[0][0] == 0, "dataset appears refreshed -- revisit D-001"


# --- 2. the same eight questions, through the agent --------------------------
#
# Driven by FakeLLM primed with the reference SQL above, so these assert the
# agent's PLUMBING end to end -- generation is parsed, the guard rewrites it, the
# executor runs it, the anchor reaches synthesis, cross-tenant questions are
# refused when scoped. They do NOT assert that a real model writes this SQL; that
# needs an API key and is tracked as open-questions.md Q-012.
#
# When a key is available, the same expectations ARE the acceptance target. Set
# FLEETPANDA_EVAL_LLM=1 and this file runs against the real model instead, with
# every assertion unchanged -- see `_agent`. That is the evaluation harness: there
# is no second set of questions, no second set of expected answers, and no report
# format to keep in sync, because `pytest -v` already prints one line per question.

REFERENCE_SQL = {
    1: f"SELECT COUNT(*) AS deliveries FROM delivery_orders "
       f"WHERE status = 'completed' AND delivery_date >= date({ANCHOR}, '-7 day')",
    2: f"SELECT tenant_id, SUM(gallons_delivered) AS gallons FROM delivery_orders "
       f"WHERE product_type = 'diesel' AND status = 'completed' "
       f"AND strftime('%Y-%m', delivery_date) = "
       f"strftime('%Y-%m', date({ANCHOR}, 'start of month', '-1 month')) "
       f"GROUP BY tenant_id ORDER BY gallons DESC",
    3: "SELECT d.name, COUNT(*) AS deliveries FROM delivery_orders o "
       "JOIN drivers d ON o.driver_id = d.driver_id WHERE o.status = 'completed' "
       "GROUP BY d.driver_id ORDER BY deliveries DESC LIMIT 5",
    4: "SELECT AVG(gallons_delivered) AS avg_gallons FROM delivery_orders "
       "WHERE product_type = 'propane' AND status = 'completed'",
    5: f"SELECT COUNT(*) AS emergency_orders FROM delivery_orders "
       f"WHERE priority = 'emergency' AND order_date >= date({ANCHOR}, '-30 day')",
    6: "SELECT COUNT(*) AS n FROM trucks WHERE status = 'maintenance'",
    7: "SELECT tenant_id, SUM(gallons_delivered) / SUM(gallons_ordered) AS fill_rate "
       "FROM delivery_orders WHERE status = 'completed' "
       "GROUP BY tenant_id ORDER BY fill_rate DESC",
    8: f"WITH windows AS ("
       f"  SELECT tenant_id,"
       f"    SUM(CASE WHEN delivery_date > date({ANCHOR}, '-30 day') THEN 1 ELSE 0 END) AS recent,"
       f"    SUM(CASE WHEN delivery_date > date({ANCHOR}, '-60 day')"
       f"              AND delivery_date <= date({ANCHOR}, '-30 day') THEN 1 ELSE 0 END) AS prior"
       f"  FROM delivery_orders WHERE status = 'completed' GROUP BY tenant_id)"
       f" SELECT tenant_id, recent, prior, 100.0 * (recent - prior) / prior AS pct_change"
       f" FROM windows WHERE prior > 0"
       f" AND 100.0 * (recent - prior) / prior < {config.DECLINE_THRESHOLD_PCT}"
       f" ORDER BY pct_change",
}

QUESTIONS = {
    1: "How many deliveries were completed in the last 7 days across all tenants?",
    2: "Which tenant delivered the most gallons of diesel last month?",
    3: "Show me the top 5 drivers by total deliveries for tenant 3",
    4: "What is the average gallons per delivery for propane orders?",
    5: "How many emergency orders did tenant 4 have in the past 30 days?",
    6: "Which trucks are currently in maintenance status?",
    7: "What is the fill rate (gallons delivered / gallons ordered) for completed orders by tenant?",
    8: "List tenants with declining delivery volume (compare last 30 days vs previous 30 days)",
}

# The four that range over every tenant by construction. CLAUDE.md section 9
# lists only {1, 7}; see open-questions.md Q-001.
CROSS_TENANT = {1, 2, 7, 8}


def _agent(number: int, answer_text: str = "Answer."):
    """An agent primed to produce the reference SQL for one question.

    With FLEETPANDA_EVAL_LLM set, the real client is used instead and nothing is
    primed -- the model has to write the SQL itself. The assertions below do not
    change, which is the point: if a real model writes correct SQL it produces the
    same numbers, so "did it get Q4's status filter right?" is answered by the
    existing test rather than by a separate scoring rubric. Costs ~2 calls per
    question. Tracked as open-questions.md Q-012.
    """
    from src.agent.sql_agent import SqlAgent

    if os.environ.get("FLEETPANDA_EVAL_LLM"):
        from src.llm.client import LLMClient

        return SqlAgent(LLMClient())

    return SqlAgent(
        FakeLLM(
            sql_reply(REFERENCE_SQL[number], is_cross_tenant=number in CROSS_TENANT),
            answer_text,
        )
    )


@pytest.mark.parametrize(
    "number, first_row",
    [
        (1, (604,)),
        (2, (3, 85816.6)),
        (3, ("Daryl Williams", 91)),
        (4, (1467.7,)),
        (5, (17,)),
    ],
    ids=lambda v: f"q{v}" if isinstance(v, int) else "",
)
def test_agent_answers_the_question(number, first_row):
    """Question -> generation -> guard -> execution -> synthesis, asserting the
    same numbers the reference SQL produces.

    Questions 3 and 5 name a tenant and run scoped; the rest run platform-scoped.
    Ambiguities each question must resolve are documented on the reference test
    of the same number above.
    """
    context = (
        TenantContext.for_tenant(3) if number == 3
        else TenantContext.for_tenant(4) if number == 5
        else TenantContext.platform()
    )
    answer = _agent(number).answer(QUESTIONS[number], context)

    assert not answer.refused, answer.refusal_reasons

    # Element-wise rather than tuple equality: these rows mix driver names with
    # gallons, and the float columns differ in the decimal place that matters
    # (0.9268 for a ratio, 85816.6 for a volume). approx on the floats and exact
    # on everything else avoids picking one rounding rule for both.
    assert len(answer.rows[0]) == len(first_row)
    for actual, expected in zip(answer.rows[0], first_row):
        if isinstance(expected, float):
            assert actual == pytest.approx(expected, rel=1e-4)
        else:
            assert actual == expected

    assert answer.date_anchor == "2026-05-29"


# Q6, Q7 and Q8 are asserted on their own because the correct answer is not a
# fixed first row, and pinning one was pinning our reference SQL's presentation
# rather than the answer. The first live run made that concrete: the model listed
# the six trucks where the reference counted them, and returned the twelve fill
# rates unordered where the reference sorted them. Both are defensible readings of
# the question -- arguably better ones -- so the assertions now describe what a
# correct answer contains and stay quiet about how it is shaped.


def test_agent_answers_q6_with_the_six_trucks_in_maintenance():
    """Q6. "Which trucks" admits a list or a count; six is six either way."""
    answer = _agent(6).answer(QUESTIONS[6], TenantContext.platform())
    assert not answer.refused, answer.refusal_reasons

    if answer.row_count == 1 and len(answer.rows[0]) == 1:
        assert answer.rows[0][0] == 6          # counted
    else:
        assert answer.row_count == 6           # listed


def test_agent_answers_q7_with_every_tenants_fill_rate():
    """Q7. The question asks for a fill rate per tenant and does not ask for a
    ranking, so ordering is not part of being right. Tenant 3 is still the
    highest, whoever happens to be printed first."""
    answer = _agent(7).answer(QUESTIONS[7], TenantContext.platform())
    assert not answer.refused, answer.refusal_reasons
    assert answer.row_count == 12

    t_idx = answer.columns.index("tenant_id") if "tenant_id" in answer.columns else 0
    r_idx = next(
        (i for i, c in enumerate(answer.columns) if i != t_idx and any(k in c.lower() for k in ("rate", "fill", "gallons", "sum"))),
        1 if t_idx == 0 else 0,
    )
    rates = {row[t_idx]: row[r_idx] for row in answer.rows}
    assert all(0.90 < rate < 0.95 for rate in rates.values())
    best = max(rates, key=rates.get)
    assert best == 3
    assert rates[best] == pytest.approx(0.9268, rel=1e-4)


def test_agent_answers_q8_with_a_materiality_threshold():
    """Q8. The answer is which tenants are declining, not the column layout that
    carries them. The threshold reaches the model through the prompt (D-023);
    before it did, every tenant with any decline came back -- eleven of twelve."""
    answer = _agent(8).answer(QUESTIONS[8], TenantContext.platform())
    assert not answer.refused, answer.refusal_reasons
    # Depending on window boundary edge inclusion (strictly > vs >= on the -30 day boundary),
    # tenant 12 hovers right at the -10% cusp (-11.25% with > vs. -9.5% with >=). Both are valid.
    assert {row[0] for row in answer.rows} in ({4, 8, 9, 12}, {4, 8, 9})


@pytest.mark.parametrize("number", sorted(CROSS_TENANT))
def test_cross_tenant_questions_are_refused_when_scoped(number):
    """The refusal path, graded as heavily as the answers.

    Answering these in a scoped session would return one tenant's rows presented
    as a platform-wide ranking.
    """
    answer = _agent(number).answer(QUESTIONS[number], TenantContext.for_tenant(4))

    assert answer.refused
    assert answer.rows == ()
    assert any("tenant" in reason.lower() for reason in answer.refusal_reasons)


@pytest.mark.parametrize("number", [3, 5, 6])
def test_single_tenant_questions_are_allowed_when_scoped(number):
    """The other side of the same coin -- scoping must not refuse everything."""
    answer = _agent(number).answer(QUESTIONS[number], TenantContext.for_tenant(4))
    assert not answer.refused
