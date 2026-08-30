"""Runs guard-approved SQL under a row cap, a time budget, and a final tenant check.

Owned by: the db layer. Called by the SQL agent. Calls: `connection.py`,
`guard.py`, `config`, and `src.agent.session`.

This is layer 3 of the three isolation layers. Layers 1 and 2 (read-only
connection, AST rewrite) act *before* execution and reason about the query. This
one acts *after* and reasons about the data that actually came back, which makes
it the only layer that can catch a guard bug -- a predicate injected onto the
wrong alias, a scope the traversal missed, a sqlglot upgrade that changes an
argument name. It should never fire. If it does, that is a defect report, not a
user error, and it is raised rather than returned.
"""

from __future__ import annotations

import sqlite3
import time
from dataclasses import dataclass
from typing import Any, Sequence

from src import config
from src.agent.session import TenantContext
from src.db.connection import read_only_connection
from src.db.guard import GuardResult, SqlGuard


class TenantIsolationError(RuntimeError):
    """Rows for a tenant other than the bound one came back from the database.

    Raised, never returned and never logged-and-continued: if this fires, the
    guard has a hole and the only safe action is to fail the request loudly.
    """


class QueryTimeoutError(RuntimeError):
    """The query exceeded its wall-clock budget and was aborted mid-execution."""


class QueryRejectedError(RuntimeError):
    """Execution was attempted on SQL the guard did not approve."""


class QueryExecutionError(RuntimeError):
    """The guard approved the SQL and SQLite still could not run it.

    A generated query can be a well-formed, allowlisted SELECT and still name a
    column that does not exist -- the guard validates statement shape and table
    access, not column names, because it has no schema to check them against.
    The first live run produced exactly that: `d.tenant_id` against an alias with
    no such column.

    Typed separately from `QueryRejectedError` because the caller does something
    different with it. A rejection means the query was refused before running; this
    means it ran and failed, and SQLite's message is a better correction to hand
    back to the model than anything we could infer.
    """


@dataclass(frozen=True, slots=True)
class QueryResult:
    """Rows plus everything needed to explain how they were obtained."""

    columns: tuple[str, ...]
    rows: tuple[tuple[Any, ...], ...]
    executed_sql: str
    row_count: int
    truncated: bool
    elapsed_seconds: float

    def as_dicts(self) -> tuple[dict[str, Any], ...]:
        """Row tuples as dicts, for handing to the LLM for synthesis."""
        return tuple(dict(zip(self.columns, row)) for row in self.rows)


class QueryExecutor:
    """Executes validated SQL against the read-only dispatch database."""

    def __init__(
        self,
        guard: SqlGuard | None = None,
        max_rows: int = config.MAX_RESULT_ROWS,
        timeout_seconds: float = config.QUERY_TIMEOUT_SECONDS,
    ) -> None:
        self._guard = guard or SqlGuard()
        self._max_rows = max_rows
        self._timeout = timeout_seconds

    def run(self, sql: str, context: TenantContext) -> tuple[GuardResult, QueryResult | None]:
        """Guard, then execute. Returns the verdict and, if allowed, the rows.

        The guard result comes back either way so the caller can explain a refusal
        without having to re-run the check.
        """
        verdict = self._guard.check(sql, context)
        if not verdict.allowed:
            return verdict, None
        return verdict, self.execute_approved(verdict, context)

    def execute_approved(self, verdict: GuardResult, context: TenantContext) -> QueryResult:
        """Execute SQL the guard has already approved.

        Separate from `run` so that nothing can execute a string that has not been
        through the guard -- this method takes a `GuardResult`, not a `str`, so
        "run some SQL" is not an expressible operation without a verdict in hand.
        """
        if not verdict.allowed or verdict.rewritten_sql is None:
            raise QueryRejectedError("Refusing to execute SQL that the guard did not approve.")

        started = time.monotonic()
        with read_only_connection() as connection:
            self._install_timeout(connection, started)
            try:
                cursor = connection.execute(verdict.rewritten_sql)
                columns = tuple(description[0] for description in cursor.description or ())
                # fetchmany(cap + 1) rather than fetchall: one extra row is enough
                # to know the result was truncated, without pulling a runaway
                # result set into memory to find out.
                fetched = cursor.fetchmany(self._max_rows + 1)
            except sqlite3.OperationalError as exc:
                if "interrupted" in str(exc).lower():
                    raise QueryTimeoutError(
                        f"Query exceeded the {self._timeout:.0f}s budget and was aborted."
                    ) from exc
                # Anything else is SQLite refusing the statement itself -- an
                # unknown column, an ambiguous reference. Wrapped rather than
                # re-raised bare so the SQL agent can turn it into a retry and
                # then a refusal, instead of a stack trace reaching the user.
                raise QueryExecutionError(str(exc)) from exc
            finally:
                connection.set_progress_handler(None, 0)

        truncated = len(fetched) > self._max_rows
        rows = tuple(tuple(row) for row in fetched[: self._max_rows])
        result = QueryResult(
            columns=columns,
            rows=rows,
            executed_sql=verdict.rewritten_sql,
            row_count=len(rows),
            truncated=truncated,
            elapsed_seconds=time.monotonic() - started,
        )
        self._assert_no_foreign_tenant(result, context)
        return result

    def _install_timeout(self, connection: sqlite3.Connection, started: float) -> None:
        """Abort the query if it outlives its budget.

        SQLite calls the handler every N virtual-machine instructions and aborts
        the statement if it returns a truthy value. 10,000 instructions is roughly
        a millisecond of work -- frequent enough that the deadline is honoured
        promptly, rare enough that the clock check is not itself the bottleneck.
        This is the only way to bound a *single* statement's runtime; the
        connection timeout parameter bounds lock waiting, not execution.
        """
        deadline = started + self._timeout

        def _abort_if_expired() -> int:
            return 1 if time.monotonic() > deadline else 0

        connection.set_progress_handler(_abort_if_expired, 10_000)

    def _assert_no_foreign_tenant(self, result: QueryResult, context: TenantContext) -> None:
        """Final check: no row carries a tenant_id other than the bound one.

        Only meaningful when the session is bound and the result actually projects
        a tenant column -- an aggregate like `SELECT COUNT(*)` has no tenant_id to
        inspect, which is precisely why this layer supplements the guard rather
        than replacing it.
        """
        if not context.is_bound or config.TENANT_COLUMN not in result.columns:
            return

        index = result.columns.index(config.TENANT_COLUMN)
        foreign = {row[index] for row in result.rows if row[index] != context.tenant_id}
        if foreign:
            raise TenantIsolationError(
                f"Query for tenant {context.tenant_id} returned rows for {sorted(foreign)}. "
                f"The AST guard failed to isolate this query. SQL: {result.executed_sql}"
            )
