"""Introspects dispatch.db into a compact schema card for the SQL prompt.

Owned by: the db layer. Called by `src/llm/prompts.py` when building the
text-to-SQL prompt, and by `guard.py` indirectly via the table allowlist. Calls:
`connection.py` and `config`.

Generated rather than pasted from `data/SCHEMA.md` because recon found the
documentation and the data disagree: `SCHEMA.md` advertises three values for
`shifts.status` where the data has one, and two for `customers.status` where the
data has one (RECON.md section 3, DECISIONS.md DQ-3). A prompt that advertises a
literal which never occurs invites a filter that correctly returns zero rows --
indistinguishable from a bug at the UI, and impossible for the model to know
about.

So the card carries observed distinct values for low-cardinality text columns, not
documented ones, plus the two facts that recon showed a model cannot infer and
will otherwise get wrong: which column to use for dates, and where "now" is.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path

from src import config
from src.db.connection import read_only_connection

# A text column with at most this many distinct values is treated as an enum and
# its literals are listed in the card. Above it, listing them would bloat the
# prompt without helping -- 114 driver names are not a useful hint.
ENUM_MAX_DISTINCT = 12


@dataclass(frozen=True, slots=True)
class Column:
    name: str
    data_type: str
    nullable: bool
    observed_values: tuple[str, ...] = ()
    null_fraction: float = 0.0


@dataclass(frozen=True, slots=True)
class Table:
    name: str
    row_count: int
    columns: tuple[Column, ...]


@dataclass(frozen=True, slots=True)
class SchemaCard:
    """Everything the SQL prompt needs to know about the database."""

    tables: tuple[Table, ...]
    date_anchor: str | None

    @property
    def table_names(self) -> frozenset[str]:
        return frozenset(t.name for t in self.tables)

    def render(self) -> str:
        """The card as prompt text. Compact on purpose -- it is sent every call."""
        lines: list[str] = ["## dispatch.db schema (introspected, with observed values)", ""]
        for table in self.tables:
            lines.append(f"### {table.name}  ({table.row_count:,} rows)")
            for column in table.columns:
                parts = [f"  {column.name} {column.data_type}"]
                if column.observed_values:
                    parts.append("one of: " + ", ".join(repr(v) for v in column.observed_values))
                if column.null_fraction > 0:
                    parts.append(f"{column.null_fraction:.0%} NULL")
                lines.append(" | ".join(parts))
            lines.append("")

        lines.extend([
            "## Facts that are not visible in the schema",
            "",
            "- `customers` holds the tenants' END-customers (fuel recipients).",
            "  It is NOT the list of FleetPanda tenants. A question about "
            "'customers' of a tenant means rows in this table.",
            "- `delivery_orders.created_at` is a single constant for every row "
            "(the fixture's generation timestamp). Never use it for date "
            "arithmetic. Use `order_date` or `delivery_date`.",
            "- `tank_readings` has many rows per customer. Joining it before "
            "aggregating inflates SUM and COUNT roughly 9x. Aggregate "
            "`delivery_orders` alone, then join only to label the result.",
            "- `gallons_delivered` is NULL for every order that is not "
            "'completed'. Any fill-rate or gallons aggregate must filter "
            "`status = 'completed'` or the numerator silently loses 30% of rows "
            "while the denominator keeps them.",
            # Added after the first live run: the model answered Q5 with
            # delivery_date and got 18 where the correct answer is 17. Both
            # columns are always populated, so nothing in the data hints at which
            # one a question means -- only the wording does.
            "- `order_date` is when an order was PLACED. `delivery_date` is the "
            "SCHEDULED delivery, 0-3 days later (1.5 on average) -- it is filled "
            "in on every row including the 948 cancelled ones, which were never "
            "delivered at all. It is only evidence of a real delivery when "
            "`status = 'completed'`, so counting deliveries needs that filter as "
            "well as the date. A question about ORDERS ('how many emergency "
            "orders...') filters `order_date`; a question about DELIVERIES "
            "('deliveries completed...') filters `delivery_date` AND "
            "`status = 'completed'`. Picking the wrong column shifts a relative "
            "window by a day or two and returns a plausible wrong number with no "
            "error.",
            # Added after the first live run: the model answered Q3 by summing
            # this column, which ranks drivers correctly but reports six times the
            # real delivery count.
            "- `shifts.total_deliveries` is a per-shift counter that does NOT "
            "reconcile with `delivery_orders`: it sums to 40,911 platform-wide "
            "against 6,851 completed orders, roughly 6x. Never use it to count "
            "deliveries. To rank drivers, COUNT rows in `delivery_orders` grouped "
            "by `driver_id`.",
            "- A question naming people or vehicles wants their labels, not their "
            "keys. Join `drivers` for `drivers.name` and `trucks` for "
            "`trucks.label` rather than returning a bare `driver_id` or "
            "`truck_id`.",
        ])

        if self.date_anchor:
            lines.extend([
                f"- The newest {config.DATE_ANCHOR_COLUMN} in the data is "
                f"{self.date_anchor}. Treat that date as 'today'. For relative "
                "windows use e.g. "
                f"`date((SELECT MAX({config.DATE_ANCHOR_COLUMN}) FROM "
                f"{config.DATE_ANCHOR_TABLE}), '-7 day')` rather than "
                "`date('now')`, which returns zero rows against this dataset.",
                # The anchor column is pinned, not merely suggested. A live run
                # anchored on MAX(order_date) instead, which is a day earlier than
                # MAX(delivery_date) for two of the twelve tenants -- so the window
                # gained a day and the answer moved. Both columns look equally
                # reasonable to a reader of the schema alone.
                f"- ALWAYS anchor on `MAX({config.DATE_ANCHOR_COLUMN})`, never on "
                f"`MAX(order_date)`. They differ: `order_date` runs a day behind "
                f"`{config.DATE_ANCHOR_COLUMN}` for some tenants, which silently "
                "widens a relative window by a day. Use `order_date` for FILTERING "
                "orders, but never as the anchor a window is measured from.",
            ])
        return "\n".join(lines)


def _observed_values(connection: sqlite3.Connection, table: str, column: str) -> tuple[str, ...]:
    """Distinct values for a text column, if there are few enough to be an enum."""
    rows = connection.execute(
        f"SELECT DISTINCT {column} FROM {table} "
        f"WHERE {column} IS NOT NULL LIMIT {ENUM_MAX_DISTINCT + 1}"
    ).fetchall()
    if len(rows) > ENUM_MAX_DISTINCT:
        return ()
    return tuple(sorted(str(row[0]) for row in rows))


@lru_cache(maxsize=1)
def introspect(db_path: Path | None = None) -> SchemaCard:
    """Read the live schema. Cached -- the database is read-only."""
    tables: list[Table] = []
    with read_only_connection(db_path) as connection:
        names = [
            row[0]
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type='table' "
                "AND name NOT LIKE 'sqlite_%' ORDER BY name"
            )
        ]
        for name in names:
            row_count = connection.execute(f"SELECT COUNT(*) FROM {name}").fetchone()[0]
            columns: list[Column] = []
            for info in connection.execute(f"PRAGMA table_info({name})"):
                data_type = (info["type"] or "").upper()
                nulls = connection.execute(
                    f"SELECT SUM(CASE WHEN {info['name']} IS NULL THEN 1 ELSE 0 END) FROM {name}"
                ).fetchone()[0] or 0
                columns.append(
                    Column(
                        name=info["name"],
                        data_type=data_type,
                        nullable=not info["notnull"],
                        observed_values=(
                            _observed_values(connection, name, info["name"])
                            if data_type == "TEXT" else ()
                        ),
                        null_fraction=(nulls / row_count) if row_count else 0.0,
                    )
                )
            tables.append(Table(name=name, row_count=row_count, columns=tuple(columns)))

        anchor = connection.execute(
            f"SELECT MAX({config.DATE_ANCHOR_COLUMN}) FROM {config.DATE_ANCHOR_TABLE}"
        ).fetchone()[0]

    return SchemaCard(tables=tuple(tables), date_anchor=anchor)
