"""Create the demo database: schema, generated rows (COPY), indexes, statistics, the reader role and RLS policies."""

from __future__ import annotations

import time
from dataclasses import dataclass
from importlib import resources

import psycopg
from psycopg import sql

from tally.demo.generator import generate
from tally.logging_config import get_logger
from tally.security import disable_region_scope, enable_region_scope, setup_reader

log = get_logger(__name__)

DEMO_PII_COLUMNS: dict[str, list[str]] = {"customers": ["email", "phone", "street_address"]}
INDEXES = [
    ("orders", "customer_id"),
    ("orders", "ordered_at"),
    ("orders", "region_id"),
    ("orders", "campaign_id"),
    ("order_items", "order_id"),
    ("order_items", "product_id"),
    ("refunds", "order_id"),
    ("refunds", "order_item_id"),
    ("payments", "order_id"),
    ("payments", "invoice_id"),
    ("invoices", "subscription_id"),
    ("subscriptions", "customer_id"),
    ("support_tickets", "customer_id"),
    ("support_tickets", "created_at"),
    ("customers", "region_id"),
]


@dataclass(frozen=True)
class SeedReport:
    counts: dict[str, int]
    seconds: float

    @property
    def total_rows(self) -> int:
        return sum(self.counts.values())


def is_seeded(conn: psycopg.Connection, schema: str) -> bool:
    row = conn.execute("SELECT to_regclass(%s)", (f"{schema}.orders",)).fetchone()
    if row is None or row[0] is None:
        return False
    count = conn.execute(sql.SQL("SELECT count(*) FROM {}").format(sql.Identifier(schema, "orders"))).fetchone()
    return bool(count and count[0] > 0)


def seed(
    conn: psycopg.Connection,
    *,
    schema: str = "public",
    reader_role: str = "tally_reader",
    reader_password: str = "tally_reader",
    scoped_role: str = "tally_scoped_reader",
    statement_timeout_ms: int = 10_000,
    app_schema: str = "tally",
    seed_value: int = 42,
    scale: float = 1.0,
    reset: bool = True,
    rls: bool = False,
) -> SeedReport:
    started = time.monotonic()
    tables = generate(seed_value, scale)
    ddl = resources.files("tally.demo").joinpath("schema.sql").read_text(encoding="utf-8")
    with conn.transaction():
        if reset:
            for table in reversed(list(tables.rows)):
                conn.execute(sql.SQL("DROP TABLE IF EXISTS {} CASCADE").format(sql.Identifier(schema, table)))
        conn.execute(sql.SQL("CREATE SCHEMA IF NOT EXISTS {}").format(sql.Identifier(schema)))
        conn.execute(sql.SQL("SET LOCAL search_path = {}").format(sql.Identifier(schema)))
        conn.execute(ddl)  # a trusted packaged file
        with conn.cursor() as cur:
            for table, rows in tables.rows.items():
                columns = sql.SQL(", ").join(sql.Identifier(c) for c in tables.columns[table])
                statement = sql.SQL("COPY {} ({}) FROM STDIN").format(sql.Identifier(schema, table), columns)
                with cur.copy(statement) as copy:
                    for row in rows:
                        copy.write_row(row)
        for table, column in INDEXES:
            conn.execute(
                sql.SQL("CREATE INDEX {} ON {} ({})").format(
                    sql.Identifier(f"{table}_{column}_idx"), sql.Identifier(schema, table), sql.Identifier(column)
                )
            )
        setup_reader(
            conn,
            schema=schema,
            role=reader_role,
            password=reader_password,
            tables=list(tables.rows),
            pii_columns=DEMO_PII_COLUMNS,
            statement_timeout_ms=statement_timeout_ms,
        )
        if rls:
            enable_region_scope(conn, schema=schema, role=reader_role, scoped_role=scoped_role)
        else:
            disable_region_scope(conn, schema=schema)
        conn.execute(sql.SQL("CREATE SCHEMA IF NOT EXISTS {}").format(sql.Identifier(app_schema)))
        conn.execute(
            sql.SQL("REVOKE ALL ON SCHEMA {} FROM {}").format(sql.Identifier(app_schema), sql.Identifier(reader_role))
        )
        # Nobody but the owner creates objects in the data schema (PostgreSQL 15+ already defaults to this).
        conn.execute(sql.SQL("REVOKE CREATE ON SCHEMA {} FROM PUBLIC").format(sql.Identifier(schema)))
    for table in tables.rows:
        conn.execute(sql.SQL("ANALYZE {}").format(sql.Identifier(schema, table)))
    report = SeedReport(tables.counts(), round(time.monotonic() - started, 2))
    log.info("seed.done", total_rows=report.total_rows, seconds=report.seconds)
    return report
