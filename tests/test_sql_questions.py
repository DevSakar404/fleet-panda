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

import pytest

from src.agent.session import TenantContext

# Anchored on the data rather than on `date('now')`: the dataset ends 2026-05-29,
# 91 days before this was written, so `date('now')` returns zero rows for four of
# these eight questions. See DECISIONS.md D-001.
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
    total 9x (RECON.md section 5).
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
    useful rather than exhaustive. See OPEN_QUESTIONS.md Q-005.
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


# --- 2. the agent path, skipped until Step 4 ---------------------------------

EIGHT_QUESTIONS = [
    pytest.param(
        1, "How many deliveries were completed in the last 7 days across all tenants?",
        604, id="q1-completed-last-7-days",
    ),
    pytest.param(
        2, "Which tenant delivered the most gallons of diesel last month?",
        3, id="q2-top-diesel-tenant",
    ),
    pytest.param(
        3, "Show me the top 5 drivers by total deliveries for tenant 3",
        "Daryl Williams", id="q3-top-5-drivers",
    ),
    pytest.param(
        4, "What is the average gallons per delivery for propane orders?",
        1467.7, id="q4-avg-propane-gallons",
    ),
    pytest.param(
        5, "How many emergency orders did tenant 4 have in the past 30 days?",
        17, id="q5-emergency-orders",
    ),
    pytest.param(
        6, "Which trucks are currently in maintenance status?",
        6, id="q6-trucks-in-maintenance",
    ),
    pytest.param(
        7, "What is the fill rate (gallons delivered / gallons ordered) for completed orders by tenant?",
        0.9268, id="q7-fill-rate",
    ),
    pytest.param(
        8, "List tenants with declining delivery volume (compare last 30 days vs previous 30 days)",
        [4, 8, 9, 12], id="q8-declining-volume",
    ),
]


@pytest.mark.skip(reason="Step 4: sql_agent is a stub")
@pytest.mark.parametrize("number, question, expected", EIGHT_QUESTIONS)
def test_agent_answers_the_question(number, question, expected):
    """End-to-end: question -> generated SQL -> guarded -> rows -> answer.

    Ambiguities the agent must resolve, per question:

      Q1  'last 7 days' -- anchored on MAX(delivery_date), not now(). 'Completed'
          means status='completed', not 'has a delivery_date in the past'.
      Q2  'last month' -- the last complete calendar month in the data (2026-04),
          not a rolling 30 days. Must not join tank_readings (9x inflation).
      Q3  'total deliveries' -- completed orders, not shifts.total_deliveries,
          which is a different number from a different table.
      Q4  'per delivery' -- over completed orders only. Omitting the status filter
          silently changes what is being averaged.
      Q5  'past 30 days' -- anchored, and on order_date (when it was placed) not
          delivery_date, because the question is about orders.
      Q6  'currently' -- trucks.status has no history, so 'currently' is just the
          current row. Also the one question whose scoped and platform answers
          both make sense; the agent should say which it gave.
      Q7  SUM/SUM, not AVG of ratios. Completed orders only.
      Q8  Two anchored 30-day windows, and a materiality threshold -- seven
          tenants are technically negative and only four are meaningfully so.

    Cross-tenant questions (1, 2, 7, 8) must be REFUSED in a tenant-scoped session
    rather than answered with one tenant's rows.
    """
    from src.agent.sql_agent import answer_question

    answer_question(question, TenantContext.platform())


@pytest.mark.skip(reason="Step 4: sql_agent is a stub")
@pytest.mark.parametrize("number", [1, 2, 7, 8])
def test_cross_tenant_questions_are_refused_when_scoped(number):
    """The refusal path, which is graded as heavily as the answers."""
    from src.agent.sql_agent import answer_question

    answer_question(dict((q.values[0], q.values[1]) for q in EIGHT_QUESTIONS)[number],
                    TenantContext.for_tenant(4))
