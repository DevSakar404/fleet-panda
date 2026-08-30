"""Tenant isolation: a query for tenant A must never return tenant B's data.

Three layers are tested separately, because each is supposed to hold on its own:

  guard    -- the rewritten SQL carries a predicate for every scoped table
  executor -- the rows that came back all belong to the bound tenant
  assertion-- when the guard is bypassed, the post-execution check still fires

The last group is the important one. It is the only test here that would survive
a bug in the guard, and it exists because the guard already had exactly such a
bug during Step 2: a sqlglot argument rename made `_direct_sources` return
nothing, so predicates were silently not injected and every query was
cross-tenant. The SQL still parsed, still ran, and still looked correct.
"""

from __future__ import annotations

import pytest

from src import config
from src.agent.session import SessionScope, TenantContext
from src.db.executor import QueryExecutor, TenantIsolationError
from src.db.guard import GuardResult, SqlGuard


# --- the guard rewrites correctly --------------------------------------------

@pytest.mark.parametrize("table", sorted(config.TENANT_SCOPED_TABLES))
def test_every_scoped_table_gets_a_predicate(guard, table):
    """The base case, run once per table so a new table cannot be forgotten."""
    result = guard.check(f"SELECT * FROM {table}", TenantContext.for_tenant(3))
    assert result.allowed
    assert result.injected_predicates == 1
    assert f"{table}.{config.TENANT_COLUMN} = 3" in result.rewritten_sql


@pytest.mark.parametrize(
    "sql, expected_predicates",
    [
        # one table, one predicate
        ("SELECT COUNT(*) FROM delivery_orders", 1),
        # a join must filter BOTH sides -- filtering only the driven table lets
        # the joined table widen the result back out
        ("SELECT * FROM delivery_orders o JOIN drivers d ON o.driver_id = d.driver_id", 2),
        ("SELECT * FROM delivery_orders o JOIN drivers d ON o.driver_id = d.driver_id "
         "JOIN trucks t ON o.truck_id = t.truck_id", 3),
        # a subquery is its own scope and gets its own predicate
        ("SELECT * FROM delivery_orders WHERE customer_id IN "
         "(SELECT customer_id FROM customers)", 2),
        # a CTE body is a scope; the reference to the CTE by name is not a table
        ("WITH recent AS (SELECT * FROM delivery_orders) SELECT * FROM recent", 1),
        # a derived table in FROM is its own scope
        ("SELECT * FROM (SELECT * FROM shifts) AS x", 1),
        # two CTEs, two bodies
        ("WITH a AS (SELECT * FROM drivers), b AS (SELECT * FROM trucks) "
         "SELECT * FROM a JOIN b ON 1=1", 2),
    ],
)
def test_predicate_reaches_every_scope(guard, sql, expected_predicates):
    result = guard.check(sql, TenantContext.for_tenant(3))
    assert result.allowed, result.reasons
    assert result.injected_predicates == expected_predicates
    assert result.rewritten_sql.count(f"{config.TENANT_COLUMN} = 3") >= expected_predicates


def test_predicate_uses_the_alias_when_one_exists(guard):
    """`FROM delivery_orders o` must produce `o.tenant_id`, not `delivery_orders.tenant_id`.

    An unqualified or wrongly-qualified column is a SQL error the moment a second
    scoped table joins in, so this is correctness as well as isolation.
    """
    result = guard.check(
        "SELECT * FROM delivery_orders o JOIN drivers d ON o.driver_id = d.driver_id",
        TenantContext.for_tenant(9),
    )
    assert "o.tenant_id = 9" in result.rewritten_sql
    assert "d.tenant_id = 9" in result.rewritten_sql


def test_existing_where_clause_is_extended_not_replaced(guard):
    result = guard.check(
        "SELECT * FROM delivery_orders WHERE status = 'completed'",
        TenantContext.for_tenant(2),
    )
    assert "status = 'completed'" in result.rewritten_sql
    assert "tenant_id = 2" in result.rewritten_sql


def test_asking_for_another_tenant_yields_a_contradiction(guard, executor):
    """A tenant-4 session asking for tenant 7 gets `tenant_id = 7 AND tenant_id = 4`.

    The guard does not rewrite the user's own predicate away -- it ANDs its own
    onto it, so the query becomes unsatisfiable and returns nothing. Failing
    closed with zero rows is the desired outcome; silently rewriting 7 into 4
    would answer a question the user did not ask.
    """
    context = TenantContext.for_tenant(4)
    verdict, result = executor.run(
        "SELECT tenant_id, COUNT(*) AS n FROM delivery_orders WHERE tenant_id = 7 GROUP BY tenant_id",
        context,
    )
    assert verdict.allowed
    assert result.row_count == 0


def test_platform_scope_injects_nothing(guard):
    """Cross-tenant questions are refused at the router, not neutered at the guard."""
    result = guard.check(
        "SELECT tenant_id, SUM(gallons_delivered) FROM delivery_orders GROUP BY tenant_id",
        TenantContext.platform(),
    )
    assert result.allowed
    assert result.injected_predicates == 0
    assert "tenant_id = " not in result.rewritten_sql


# --- executed queries return only the bound tenant's rows --------------------

def test_no_query_returns_another_tenants_rows(executor, all_tenant_ids):
    """The end-to-end property, checked for all twelve tenants.

    Runs an unfiltered `SELECT *` -- the query a careless model would write -- and
    asserts the rows that come back are the bound tenant's and nobody else's.
    """
    for tenant_id in all_tenant_ids:
        _, result = executor.run(
            "SELECT tenant_id, order_id FROM delivery_orders", TenantContext.for_tenant(tenant_id)
        )
        returned = {row[0] for row in result.rows}
        assert returned <= {tenant_id}, f"tenant {tenant_id} saw {returned - {tenant_id}}"


def test_row_counts_partition_across_tenants(executor, all_tenant_ids):
    """Per-tenant counts must sum to the platform total.

    Catches the failure the other direction: a predicate so aggressive it drops
    rows that legitimately belong to the tenant. Isolation that returns nothing
    is not isolation, it is an outage.
    """
    per_tenant = 0
    for tenant_id in all_tenant_ids:
        _, result = executor.run(
            "SELECT COUNT(*) AS n FROM delivery_orders", TenantContext.for_tenant(tenant_id)
        )
        per_tenant += result.rows[0][0]

    _, total = executor.run("SELECT COUNT(*) AS n FROM delivery_orders", TenantContext.platform())
    assert per_tenant == total.rows[0][0] == 9769


def test_joined_query_stays_within_the_tenant(executor):
    _, result = executor.run(
        "SELECT o.tenant_id, d.tenant_id FROM delivery_orders o "
        "JOIN drivers d ON o.driver_id = d.driver_id",
        TenantContext.for_tenant(8),
    )
    for row in result.rows:
        assert row[0] == 8 and row[1] == 8


# --- layer 3 fires when the guard is bypassed --------------------------------

def test_post_execution_assertion_catches_a_bypassed_guard(executor):
    """Hand the executor an approved-looking verdict with no predicate in it.

    This simulates the exact Step 2 bug: a guard that returns `allowed=True` and
    unfiltered SQL. Layers 1 and 2 are useless here -- the statement is a valid
    read-only SELECT. Only the row-level assertion can catch it, and it must.
    """
    forged = GuardResult(
        allowed=True,
        rewritten_sql=(
            "SELECT tenant_id, COUNT(*) AS n FROM delivery_orders GROUP BY tenant_id"
        ),
        tables=frozenset({"delivery_orders"}),
        injected_predicates=0,
    )
    with pytest.raises(TenantIsolationError, match="failed to isolate"):
        executor.execute_approved(forged, TenantContext.for_tenant(1))


def test_the_row_assertion_is_a_detector_not_a_guarantee(executor):
    """Documents the known ceiling of layer 3, so nobody mistakes it for the fix.

    The assertion can only inspect rows that came back. An unfiltered query whose
    first `MAX_RESULT_ROWS` rows happen to belong to the bound tenant passes it
    while still being a leaking query -- which is exactly what happened when this
    test was first written against `LIMIT 50` (the first 50 orders in the table
    are all tenant 1's, so nothing fired).

    This is asserted rather than merely commented so the limitation is visible in
    the suite: layer 3 catches the *class* of bug reliably in aggregate queries
    and unreliably in row-level ones, which is why the AST guard, not this, is the
    primary control.
    """
    leaking_but_undetectable = GuardResult(
        allowed=True,
        rewritten_sql="SELECT tenant_id, order_id FROM delivery_orders LIMIT 50",
        tables=frozenset({"delivery_orders"}),
        injected_predicates=0,
    )
    result = executor.execute_approved(leaking_but_undetectable, TenantContext.for_tenant(1))
    assert {row[0] for row in result.rows} == {1}, (
        "If this fails the fixture changed and the test above is now the only "
        "one needed -- delete this one rather than 'fixing' it."
    )


def test_assertion_is_silent_when_the_guard_worked(executor):
    guard_result = SqlGuard().check(
        "SELECT tenant_id, order_id FROM delivery_orders", TenantContext.for_tenant(1)
    )
    result = executor.execute_approved(guard_result, TenantContext.for_tenant(1))
    assert result.row_count > 0


# --- session scope ------------------------------------------------------------

def test_cross_tenant_questions_are_refused_in_a_tenant_session():
    """Q1, Q2, Q7 and Q8 range over every tenant and must be refused when scoped.

    CLAUDE.md section 9 lists only {1, 7}; Q2 ('which tenant delivered the most')
    and Q8 ('list tenants with declining volume') are equally cross-tenant. See
    open-questions.md Q-001.
    """
    scoped = TenantContext.for_tenant(5)
    assert {q for q in range(1, 9) if not scoped.allows_question(q)} == {1, 2, 7, 8}
    platform = TenantContext.platform()
    assert all(platform.allows_question(q) for q in range(1, 9))


def test_a_tenant_session_cannot_be_built_without_a_tenant():
    with pytest.raises(ValueError):
        TenantContext(SessionScope.TENANT)


def test_a_platform_session_cannot_smuggle_a_tenant_id():
    with pytest.raises(ValueError):
        TenantContext(SessionScope.PLATFORM, tenant_id=3)
