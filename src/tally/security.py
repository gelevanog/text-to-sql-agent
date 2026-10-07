"""The database side of the safety layers: a read-only reader role, column-level grants without PII, and an optional
row-level security scope. Applied by `tally seed` for the demo and by `tally setup-reader` for your own database.

* The reader is a separate LOGIN role, never a superuser, with ``default_transaction_read_only = on`` and a role-level
  ``statement_timeout``. It gets ``SELECT`` on the allow-listed tables only; on tables with PII columns it gets
  column-level ``SELECT`` on the other columns, so ``SELECT email FROM customers`` fails in PostgreSQL itself even
  if it got past the validator.
* Row-level security (optional): every policy reads ``tally.region_scope`` (set per transaction by the executor).
  ``'*'`` allows all rows, a region code allows that region's rows, and an unset scope allows nothing (fail closed).
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping

import psycopg
from psycopg import sql

# Tables with a region_id column get a direct policy; child tables are scoped through their parent.
REGION_POLICIES: dict[str, str] = {
    "customers": "region_id IN (SELECT r.id FROM {schema}.regions r WHERE r.code = {scope})",
    "orders": "region_id IN (SELECT r.id FROM {schema}.regions r WHERE r.code = {scope})",
    "subscriptions": "region_id IN (SELECT r.id FROM {schema}.regions r WHERE r.code = {scope})",
    "support_tickets": "region_id IN (SELECT r.id FROM {schema}.regions r WHERE r.code = {scope})",
    "order_items": "EXISTS (SELECT 1 FROM {schema}.orders o WHERE o.id = order_id)",
    "refunds": "EXISTS (SELECT 1 FROM {schema}.orders o WHERE o.id = order_id)",
    "invoices": "EXISTS (SELECT 1 FROM {schema}.subscriptions s WHERE s.id = subscription_id)",
    "payments": (
        "EXISTS (SELECT 1 FROM {schema}.orders o WHERE o.id = order_id) "
        "OR EXISTS (SELECT 1 FROM {schema}.invoices i WHERE i.id = invoice_id)"
    ),
}
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
        conn.execute(
            sql.SQL("REVOKE ALL ON SCHEMA {} FROM {}").format(sql.Identifier(app_schema), ident)
        )
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


def enable_region_scope(conn: psycopg.Connection, *, schema: str, role: str) -> list[str]:
    """Row-level security for the demo schema: one SELECT policy per table for the reader role."""
    scope = sql.SQL("current_setting({}, true)").format(sql.Literal(SCOPE_SETTING))
    available = table_columns(conn, schema)
    enabled: list[str] = []
    for table, template in REGION_POLICIES.items():
        if table not in available:
            continue
        table_ident = sql.Identifier(schema, table)
        condition = sql.SQL(template).format(schema=sql.Identifier(schema), scope=scope)
        conn.execute(sql.SQL("ALTER TABLE {} ENABLE ROW LEVEL SECURITY").format(table_ident))
        conn.execute(sql.SQL("DROP POLICY IF EXISTS tally_region_scope ON {}").format(table_ident))
        conn.execute(
            sql.SQL("CREATE POLICY tally_region_scope ON {} FOR SELECT TO {} USING ({} = '*' OR {})").format(
                table_ident, sql.Identifier(role), scope, condition
            )
        )
        enabled.append(table)
    return enabled
