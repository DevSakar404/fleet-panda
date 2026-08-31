"""Static validation and tenant-predicate injection for generated SQL.

Owned by: the db layer. Called by `executor.py` (which refuses to run anything the
guard has not approved) and by the SQL agent. Calls: `sqlglot`, `config`, and
`src.agent.session` for the bound TenantContext.

This is the file the whole multi-tenant claim rests on, and it is deliberately the
most heavily commented one in the repo.

Why an AST and not a regex or a prompt instruction: a prompt is a request, not a
control -- the model can ignore it, and nothing downstream would know. A regex
over SQL text cannot see scope, so it cannot tell `FROM delivery_orders` in an
outer query from the same string inside a subquery that is already filtered, and
it cannot find the WHERE clause it is supposed to extend. Parsing to a tree and
rewriting nodes is the only approach where "every tenant-scoped table reference
carries a tenant predicate" is a statement we can actually verify.

The guard is layer 2 of 3. Layer 1 is the read-only connection (a write cannot
execute even if the guard misses it, `src/db/connection.py`); layer 3 is the
post-execution row assertion (`src/db/executor.py`). Each layer assumes the others
may fail.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Iterable, Iterator

import sqlglot
from sqlglot import exp

from src import config
from src.agent.session import TenantContext

# Statement kinds that must never appear, anywhere in the tree. sqlglot parses
# PRAGMA, ATTACH, VACUUM and similar bare verbs into `exp.Command`, which is why
# that catch-all is in the list -- it is the node type that would otherwise let
# `PRAGMA query_only = OFF` or `ATTACH DATABASE ...` through as "not a DML node".
#
# Most of these are top-level statements that the `isinstance(statement, exp.Select)`
# check already rejects (their root is not a Select). They are listed anyway, belt to
# that check's braces, so a nested one is caught even if the root gate is ever weakened.
# `exp.Into` earns its place for a different reason: `SELECT * INTO backup FROM t`
# stays rooted at exp.Select, so it slips PAST the root gate -- it is the one write
# verb this list catches that the Select check does not.
#
# Deliberately NOT here: `exp.Replace` is the SQLite REPLACE(str, a, b) string
# function, not `REPLACE INTO`, so forbidding it would reject legitimate SELECTs.
# `exp.Truncate` does not exist in sqlglot and referencing it breaks the import.
FORBIDDEN_NODES: tuple[type[exp.Expression], ...] = (
    exp.Insert, exp.Update, exp.Delete, exp.Drop, exp.Create, exp.Alter,
    exp.Command, exp.Transaction, exp.Commit, exp.Rollback,
    exp.Into,
)

# SQLite's internal catalogue. Reading it leaks every tenant's schema and, more
# to the point, is never needed to answer a dispatch question.
FORBIDDEN_TABLE_PREFIX = "sqlite_"


@dataclass(frozen=True, slots=True)
class GuardResult:
    """The verdict, and everything the agent needs to explain it.

    A structured result rather than an exception because a refusal is a normal
    conversational outcome -- the agent has to tell the user *why* it will not run
    their question, and `reasons` is what it says. (CLAUDE.md section 7.)
    """

    allowed: bool
    rewritten_sql: str | None
    reasons: tuple[str, ...] = ()
    tables: frozenset[str] = frozenset()
    injected_predicates: int = 0

    @classmethod
    def reject(cls, *reasons: str) -> "GuardResult":
        return cls(allowed=False, rewritten_sql=None, reasons=tuple(reasons))


class SqlGuard:
    """Validates one generated statement and rewrites it to be tenant-safe."""

    def __init__(
        self,
        allowed_tables: Iterable[str] = config.TENANT_SCOPED_TABLES,
        max_rows: int = config.MAX_RESULT_ROWS,
    ) -> None:
        # Stored lowercased because SQLite identifiers are case-insensitive:
        # `FROM DELIVERY_ORDERS` and `FROM delivery_orders` name the same table.
        # Comparing case-sensitively here would be worse than merely annoying --
        # validation would reject the uppercase form, but if it ever did not, the
        # injection pass uses the same membership test and would skip the table,
        # producing an allowed query with no tenant predicate. Both comparisons go
        # through `_canonical_table_name` so they can never disagree.
        self._allowed = frozenset(name.lower() for name in allowed_tables)
        self._max_rows = max_rows

    def check(self, sql: str, context: TenantContext) -> GuardResult:
        """Parse, validate, rewrite. Never raises on bad SQL -- returns a rejection."""
        if not sql or not sql.strip():
            return GuardResult.reject("Empty query.")

        # --- parse -----------------------------------------------------------
        # parse() rather than parse_one(): parse_one silently keeps only the first
        # statement, so `SELECT 1; DROP TABLE trucks` would validate as a clean
        # SELECT and the second statement would be invisible to us. We need the
        # count in order to reject it.
        try:
            statements = sqlglot.parse(sql, dialect="sqlite")
        except sqlglot.errors.ParseError as exc:
            return GuardResult.reject(f"Query could not be parsed as SQLite: {exc}")

        statements = [s for s in statements if s is not None]
        if len(statements) != 1:
            return GuardResult.reject(
                f"Expected exactly one statement, found {len(statements)}. "
                "Multi-statement input is rejected outright."
            )

        statement = statements[0]

        # --- validate --------------------------------------------------------
        reasons: list[str] = []

        if not isinstance(statement, exp.Select):
            reasons.append(f"Only SELECT statements are permitted, got {type(statement).__name__.upper()}.")

        for node_type in FORBIDDEN_NODES:
            if isinstance(statement, node_type) or any(True for _ in statement.find_all(node_type)):
                reasons.append(f"Statement contains a forbidden {node_type.__name__.upper()} node.")

        # CTE names look like tables in a FROM clause but are not real tables, so
        # they are exempt from the allowlist. They are collected before the table
        # sweep for exactly that reason.
        cte_names = {cte.alias_or_name for cte in statement.find_all(exp.CTE)}

        referenced: set[str] = set()
        for table in statement.find_all(exp.Table):
            name = self._canonical_table_name(table)
            if name in {c.lower() for c in cte_names}:
                continue
            referenced.add(name)

            # A schema-qualified name (`otherdb.delivery_orders`) would read from a
            # database other than the one we opened read-only. ATTACH is already
            # rejected above, so there should be no second database to reach --
            # this is the belt to that braces, and it costs one comparison.
            qualifier = table.text("db")
            if qualifier and qualifier.lower() != "main":
                reasons.append(
                    f"Cross-database reference {qualifier}.{name!r} is not permitted."
                )

            if name.startswith(FORBIDDEN_TABLE_PREFIX):
                reasons.append(f"Access to SQLite internal table {name!r} is not permitted.")
            elif name not in self._allowed:
                reasons.append(
                    f"Table {name!r} is not in the allowlist "
                    f"(permitted: {', '.join(sorted(self._allowed))})."
                )

        if not referenced and isinstance(statement, exp.Select):
            reasons.append("Query references no known table.")

        if reasons:
            return GuardResult.reject(*reasons)

        # --- rewrite ---------------------------------------------------------
        injected = self._inject_tenant_predicates(statement, context)
        self._enforce_limit(statement)

        return GuardResult(
            allowed=True,
            rewritten_sql=statement.sql(dialect="sqlite"),
            tables=frozenset(referenced),
            injected_predicates=injected,
        )

    # --- rewriting -----------------------------------------------------------

    def _inject_tenant_predicates(self, statement: exp.Expression, context: TenantContext) -> int:
        """Add `tenant_id = N` to every tenant-scoped table reference, in place.

        Returns the number of predicates added.

        The traversal is the part worth reading slowly. `find_all(exp.Select)`
        visits every SELECT in the tree -- the outer one, each CTE body, and each
        subquery -- and for each one we add a predicate to *that* SELECT's own
        WHERE clause. Because every nested SELECT is visited in its own right, a
        subquery gets filtered by its own WHERE rather than by the outer one,
        which is what makes correlated and derived-table queries safe.

        `_direct_sources` is what keeps the scopes from bleeding: it returns only
        the tables named directly in this SELECT's FROM and JOIN clauses, and
        deliberately does not descend. Using `select.find_all(exp.Table)` here
        instead would be the classic bug -- an outer SELECT would try to filter a
        table that only exists inside a subquery, producing SQL that references an
        alias not in scope.
        """
        if not context.is_bound:
            # PLATFORM scope: nothing is injected, by design. Cross-tenant
            # questions are refused earlier, at the router, via
            # TenantContext.allows_question -- not here.
            return 0

        tenant_id = context.tenant_id
        injected = 0
        cte_names = {cte.alias_or_name for cte in statement.find_all(exp.CTE)}

        lowered_ctes = {name.lower() for name in cte_names}
        for select in statement.find_all(exp.Select):
            for table in self._direct_sources(select):
                name = self._canonical_table_name(table)
                if name in lowered_ctes or name not in self._allowed:
                    continue

                # Qualify with the alias if the query gave the table one
                # (`FROM delivery_orders o` -> `o.tenant_id`), otherwise with the
                # table name. An unqualified `tenant_id = 4` would be ambiguous
                # the moment a second tenant-scoped table joins in.
                qualifier = table.alias_or_name
                predicate = exp.EQ(
                    this=exp.column(config.TENANT_COLUMN, table=qualifier),
                    expression=exp.Literal.number(tenant_id),
                )
                # append=True ANDs onto any existing WHERE rather than replacing
                # it. copy=False mutates this node in place, which is what we
                # want -- we are rewriting the tree we are about to render.
                select.where(predicate, append=True, copy=False)
                injected += 1

        return injected

    @staticmethod
    def _canonical_table_name(table: exp.Table) -> str:
        """The table's bare name, lowercased.

        The single place identifiers are normalised, so the allowlist check and
        the injection pass can never disagree about whether `DELIVERY_ORDERS` is
        the allowlisted `delivery_orders`. If they disagreed in the direction of
        allow-but-do-not-inject, the result would be an approved query with no
        tenant filter.
        """
        return table.name.lower()

    @staticmethod
    def _direct_sources(select: exp.Select) -> list[exp.Table]:
        """Tables named directly in this SELECT's FROM and JOIN clauses.

        Does not descend into subqueries: a subquery is its own `exp.Select` and
        is visited separately by the caller. A `FROM (SELECT ...) x` yields
        nothing here, which is correct -- there is no base table in this scope to
        filter.

        The loop reads this SELECT's own argument values and picks out the From
        and Join nodes, rather than indexing `select.args["from"]` by name. That
        is deliberate: sqlglot renamed this argument from "from" to "from_" in
        version 30, and the name-indexed version failed *silently* -- it returned
        no tables, injected no predicates, and produced a syntactically perfect
        query with no tenant filter at all. A guard whose failure mode is "quietly
        allows everything" is worse than no guard, so it does not depend on a key
        name it cannot verify.

        Returns a list rather than a generator because the caller mutates each
        SELECT's WHERE clause while walking these, and a lazy generator would be
        iterating `select.args` as `where()` adds a key to it.
        """
        sources: list[exp.Table] = []
        for value in tuple(select.args.values()):
            if isinstance(value, exp.From):
                if isinstance(value.this, exp.Table):
                    sources.append(value.this)
            elif isinstance(value, list):
                for item in value:
                    if isinstance(item, exp.Join) and isinstance(item.this, exp.Table):
                        sources.append(item.this)
        return sources

    def _enforce_limit(self, statement: exp.Expression) -> None:
        """Ensure the statement returns at most `max_rows` rows.

        A missing LIMIT is added. An existing LIMIT larger than the cap is lowered
        to it; a smaller one is left alone, because the model asking for the top 5
        drivers should still get 5. A non-literal LIMIT (an expression, a
        parameter) is replaced outright rather than reasoned about.
        """
        existing = statement.args.get("limit")
        if existing is None:
            statement.set("limit", exp.Limit(expression=exp.Literal.number(self._max_rows)))
            return

        value = existing.expression
        if isinstance(value, exp.Literal) and value.is_int and int(value.name) <= self._max_rows:
            return
        statement.set("limit", exp.Limit(expression=exp.Literal.number(self._max_rows)))
