"""The database side of the safety layers: a read-only reader role, column-level grants without PII, and an optional
row-level security scope. Applied by `tally seed` for the demo and by `tally setup-reader` for your own database.

* The reader is a separate LOGIN role, never a superuser, with ``default_transaction_read_only = on`` and a role-level
  ``statement_timeout``. It gets ``SELECT`` on the allow-listed tables only; on tables with PII columns it gets
  column-level ``SELECT`` on the other columns, so ``SELECT email FROM customers`` fails in PostgreSQL itself even
  if it got past the validator.
* Row-level security (opt-in with TALLY_RLS=true, for a tenant scope): every scoped table has RLS enabled. The reader
  gets a permissive ``USING (true)`` policy. When TALLY_REGION_SCOPE names a region,
  the executor runs each query after ``SET LOCAL ROLE tally_scoped_reader``, a NOLOGIN role with the same grants
  whose policy only admits rows of the region in ``tally.region_scope``; an unset scope admits nothing (fail closed).
  Generated SQL cannot change the role back: SET is not a SELECT and the validator refuses set_config().

  It is opt-in because it is not free: a role subject to RLS - even through a ``USING (true)`` policy - makes the
  planner ignore column statistics for operators that are not leakproof (e.g. ``timestamptz >= date``), and on the
  demo a typical revenue query went from 55 ms to 5.8 s on a nested-loop plan. Turn it on when a deployment really
  needs per-tenant isolation inside one database.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping

import psycopg
from psycopg import sql

# Every scoped table carries region_id (the tenant-column pattern), so each policy is a cheap column check.
REGION_SCOPED_TABLES = (
    "customers",
    "orders",
    "order_items",
    "refunds",
    "payments",
    "subscriptions",
    "invoices",
    "support_tickets",
)
SCOPE_SETTING = "tally.region_scope"


def table_columns(conn: psycopg.Connection, schema: str) -> dict[str, list[str]]:
    rows = conn.execute(
        """
        SELECT c.table_name, c.column_name
        FROM information_schema.columns c
        JOIN information_schema.tables t ON t.table_schema = c.table_schema AND t.table_name = c.table_name
        WHERE c.table_schema = %s AND t.table_type IN ('BASE TABLE', 'VIEW')
        ORDER BY c.table_name, c.ordinal_position
        """,
        (schema,),
    ).fetchall()
    columns: dict[str, list[str]] = {}
    for table, column in rows:
        columns.setdefault(table, []).append(column)
    return columns


def setup_reader(
    conn: psycopg.Connection,
    *,
    schema: str,
    role: str,
    password: str,
    tables: Iterable[str] | None = None,
    pii_columns: Mapping[str, Iterable[str]] | None = None,
    statement_timeout_ms: int = 10_000,
    app_schema: str | None = None,
) -> dict[str, list[str]]:
    """Create or update the reader role and its grants. Returns the granted columns per table."""
    pii = {table: set(cols) for table, cols in (pii_columns or {}).items()}
    available = table_columns(conn, schema)
    selected = list(tables) if tables is not None else list(available)
    exists = conn.execute("SELECT 1 FROM pg_roles WHERE rolname = %s", (role,)).fetchone()
    ident = sql.Identifier(role)
    if not exists:
        conn.execute(sql.SQL("CREATE ROLE {} LOGIN").format(ident))
    conn.execute(
        sql.SQL(
            "ALTER ROLE {} WITH LOGIN PASSWORD {} NOSUPERUSER NOCREATEDB NOCREATEROLE NOREPLICATION NOBYPASSRLS"
        ).format(ident, sql.Literal(password))
    )
    conn.execute(sql.SQL("ALTER ROLE {} SET default_transaction_read_only = on").format(ident))
    conn.execute(
        sql.SQL("ALTER ROLE {} SET statement_timeout = {}").format(ident, sql.Literal(f"{statement_timeout_ms}ms"))
    )
    schema_ident = sql.Identifier(schema)
    conn.execute(sql.SQL("REVOKE ALL ON ALL TABLES IN SCHEMA {} FROM {}").format(schema_ident, ident))
    conn.execute(sql.SQL("GRANT USAGE ON SCHEMA {} TO {}").format(schema_ident, ident))
    if app_schema:
        conn.execute(sql.SQL("REVOKE ALL ON SCHEMA {} FROM {}").format(sql.Identifier(app_schema), ident))
    granted: dict[str, list[str]] = {}
    for table in selected:
        if table not in available:
            raise ValueError(f"table {schema}.{table} does not exist")
        allowed = [c for c in available[table] if c not in pii.get(table, set())]
        table_ident = sql.Identifier(schema, table)
        if len(allowed) == len(available[table]):
            conn.execute(sql.SQL("GRANT SELECT ON {} TO {}").format(table_ident, ident))
        else:
            conn.execute(
                sql.SQL("GRANT SELECT ({}) ON {} TO {}").format(
                    sql.SQL(", ").join(sql.Identifier(c) for c in allowed), table_ident, ident
                )
            )
        granted[table] = allowed
    return granted


def enable_region_scope(conn: psycopg.Connection, *, schema: str, role: str, scoped_role: str) -> list[str]:
    """Row-level security: everything for the reader, one region for the scoped role (see the module docstring)."""
    scope = sql.SQL("current_setting({}, true)").format(sql.Literal(SCOPE_SETTING))
    available = table_columns(conn, schema)
    scoped = sql.Identifier(scoped_role)
    if not conn.execute("SELECT 1 FROM pg_roles WHERE rolname = %s", (scoped_role,)).fetchone():
        conn.execute(sql.SQL("CREATE ROLE {} NOLOGIN").format(scoped))
    conn.execute(sql.SQL("ALTER ROLE {} NOSUPERUSER NOBYPASSRLS").format(scoped))
    conn.execute(sql.SQL("GRANT USAGE ON SCHEMA {} TO {}").format(sql.Identifier(schema), scoped))
    conn.execute(sql.SQL("GRANT {} TO {}").format(scoped, sql.Identifier(role)))
    # The scoped role gets exactly the reader's grants (table-level or column-level).
    grants = conn.execute(
        """
        SELECT table_name, column_name FROM information_schema.column_privileges
        WHERE table_schema = %s AND grantee = %s AND privilege_type = 'SELECT'
        """,
        (schema, role),
    ).fetchall()
    per_table: dict[str, list[str]] = {}
    for table, column in grants:
        per_table.setdefault(table, []).append(column)
    conn.execute(sql.SQL("REVOKE ALL ON ALL TABLES IN SCHEMA {} FROM {}").format(sql.Identifier(schema), scoped))
    for table, columns in per_table.items():
        conn.execute(
            sql.SQL("GRANT SELECT ({}) ON {} TO {}").format(
                sql.SQL(", ").join(sql.Identifier(c) for c in columns), sql.Identifier(schema, table), scoped
            )
        )
    enabled: list[str] = []
    for table in REGION_SCOPED_TABLES:
        if table not in available or "region_id" not in available[table]:
            continue
        table_ident = sql.Identifier(schema, table)
        condition = sql.SQL("region_id IN (SELECT r.id FROM {}.regions r WHERE r.code = {})").format(
            sql.Identifier(schema), scope
        )
        conn.execute(sql.SQL("ALTER TABLE {} ENABLE ROW LEVEL SECURITY").format(table_ident))
        for policy in ("tally_reader_all", "tally_region_scope"):
            conn.execute(sql.SQL("DROP POLICY IF EXISTS {} ON {}").format(sql.Identifier(policy), table_ident))
        conn.execute(
            sql.SQL("CREATE POLICY tally_reader_all ON {} FOR SELECT TO {} USING (true)").format(
                table_ident, sql.Identifier(role)
            )
        )
        conn.execute(
            sql.SQL("CREATE POLICY tally_region_scope ON {} FOR SELECT TO {} USING ({})").format(
                table_ident, scoped, condition
            )
        )
        enabled.append(table)
    return enabled


def disable_region_scope(conn: psycopg.Connection, *, schema: str) -> None:
    available = table_columns(conn, schema)
    for table in REGION_SCOPED_TABLES:
        if table not in available:
            continue
        table_ident = sql.Identifier(schema, table)
        for policy in ("tally_reader_all", "tally_region_scope"):
            conn.execute(sql.SQL("DROP POLICY IF EXISTS {} ON {}").format(sql.Identifier(policy), table_ident))
        conn.execute(sql.SQL("ALTER TABLE {} DISABLE ROW LEVEL SECURITY").format(table_ident))


def region_scope_enabled(conn: psycopg.Connection, *, schema: str) -> bool:
    row = conn.execute(
        "SELECT bool_and(c.relrowsecurity) FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace "
        "WHERE n.nspname = %s AND c.relname = ANY(%s)",
        (schema, list(REGION_SCOPED_TABLES)),
    ).fetchone()
    return bool(row and row[0])
