"""The database layers: read-only reader role, column grants without PII, statement timeout, row cap, single
statements only, the cost guard and the opt-in row-level security scope."""

from __future__ import annotations

from collections.abc import Iterator

import psycopg
import pytest
from psycopg import errors

from tally.config import Settings
from tally.db import Database
from tally.security import disable_region_scope, enable_region_scope, region_scope_enabled
from tally.sql.executor import QueryError, ReadOnlyExecutor

pytestmark = pytest.mark.db


@pytest.fixture
def executor(seeded: Settings) -> Iterator[ReadOnlyExecutor]:
    db = Database(seeded.database_url, seeded.resolved_reader_url)
    yield ReadOnlyExecutor(db, statement_timeout_ms=1000, max_rows=50)
    db.close()


def test_reader_cannot_write_even_with_a_raw_connection(reader: psycopg.Connection) -> None:
    for statement in (
        "DELETE FROM orders",
        "UPDATE products SET list_price_usd = 0",
        "INSERT INTO regions (id, code, name, timezone) VALUES (9, 'X', 'X', 'UTC')",
        "CREATE TABLE leak (x int)",
        "DROP TABLE orders",
    ):
        with pytest.raises(psycopg.Error):
            reader.execute(statement)


def test_reader_sessions_default_to_read_only(reader: psycopg.Connection) -> None:
    row = reader.execute("SHOW default_transaction_read_only").fetchone()
    assert row is not None and row[0] == "on"
    with pytest.raises(errors.ReadOnlySqlTransaction):
        reader.execute("CREATE TEMP TABLE t (x int)")


def test_personal_data_columns_are_not_granted(reader: psycopg.Connection) -> None:
    with pytest.raises(errors.InsufficientPrivilege):
        reader.execute("SELECT email FROM customers LIMIT 1")
    with pytest.raises(errors.InsufficientPrivilege):
        reader.execute("SELECT * FROM customers LIMIT 1")
    row = reader.execute("SELECT name, city FROM customers LIMIT 1").fetchone()
    assert row is not None


def test_reader_cannot_see_the_app_schema(reader: psycopg.Connection) -> None:
    with pytest.raises(errors.InsufficientPrivilege):
        reader.execute("SELECT count(*) FROM tally.audit_log")


def test_executor_enforces_the_row_cap(executor: ReadOnlyExecutor) -> None:
    result = executor.execute("SELECT id FROM orders ORDER BY id")
    assert result.row_count == 50 and result.truncated
    small = executor.execute("SELECT id FROM orders ORDER BY id LIMIT 3")
    assert small.row_count == 3 and not small.truncated
    assert [c.name for c in small.columns] == ["id"]


def test_executor_statement_timeout(executor: ReadOnlyExecutor) -> None:
    with pytest.raises(QueryError) as info:
        executor.execute("SELECT pg_sleep(3)")  # the validator would refuse this; the database stops it anyway
    assert info.value.timeout


def test_executor_refuses_several_statements_in_one_call(executor: ReadOnlyExecutor, owner: psycopg.Connection) -> None:
    with pytest.raises(QueryError):
        executor.execute("SELECT 1; DELETE FROM orders")
    row = owner.execute("SELECT count(*) FROM orders").fetchone()
    assert row is not None and row[0] > 0


def test_executor_transaction_is_read_only_and_rolled_back(executor: ReadOnlyExecutor) -> None:
    with pytest.raises(QueryError):
        executor.execute("WITH x AS (DELETE FROM refunds RETURNING id) SELECT count(*) FROM x")
    result = executor.execute("SELECT current_setting('transaction_read_only'), current_setting('TimeZone')")
    assert result.rows[0] == ("on", "UTC")


def test_permission_errors_are_flagged(executor: ReadOnlyExecutor) -> None:
    with pytest.raises(QueryError) as info:
        executor.execute("SELECT phone FROM customers")
    assert info.value.permission


def test_cost_guard_sees_cross_joins_under_count_and_limit(executor: ReadOnlyExecutor) -> None:
    normal = executor.explain("SELECT region_id, count(*) FROM orders GROUP BY 1")
    cross = executor.explain("SELECT count(*) FROM order_items a CROSS JOIN order_items b")
    limited = executor.explain("SELECT a.id FROM order_items a CROSS JOIN order_items b LIMIT 10")
    assert normal.plan_rows < 100_000
    assert cross.plan_rows > 1_000_000_000
    assert limited.plan_rows > 1_000_000_000  # the largest node, not the top node
    assert cross.total_cost > normal.total_cost * 1000


@pytest.fixture
def scoped(seeded: Settings, owner: psycopg.Connection) -> Iterator[Database]:
    enable_region_scope(
        owner, schema=seeded.data_schema, role=seeded.reader_role, scoped_role=seeded.scoped_reader_role
    )
    db = Database(seeded.database_url, seeded.resolved_reader_url)
    yield db
    db.close()
    disable_region_scope(owner, schema=seeded.data_schema)


def test_row_level_security_scope(scoped: Database, owner: psycopg.Connection, seeded: Settings) -> None:
    assert region_scope_enabled(owner, schema=seeded.data_schema)
    everything = ReadOnlyExecutor(scoped, region_scope="*")
    eu_only = ReadOnlyExecutor(scoped, region_scope="EU")
    unknown = ReadOnlyExecutor(scoped, region_scope="MARS")
    total = everything.execute("SELECT count(*) FROM orders").rows[0][0]
    eu = eu_only.execute("SELECT count(*) FROM orders").rows[0][0]
    expected = owner.execute("SELECT count(*) FROM orders WHERE region_id = 2").fetchone()
    assert expected is not None and eu == expected[0] and 0 < eu < total
    regions = eu_only.execute("SELECT DISTINCT region_id FROM order_items").rows
    assert regions == [(2,)]
    assert unknown.execute("SELECT count(*) FROM payments").rows[0][0] == 0  # fail closed
    # A scoped query cannot switch itself back to the unrestricted role: SET is refused in a read-only call chain
    # by the validator, and set_config is not on its allow-list; even run directly, RLS still applies to it.
    leaked = eu_only.execute("SELECT count(*) FROM orders WHERE region_id <> 2").rows[0][0]
    assert leaked == 0


def test_region_scope_requires_the_policies(seeded: Settings) -> None:
    from tally.services import build_services

    with pytest.raises(RuntimeError, match="TALLY_RLS"):
        build_services(seeded.model_copy(update={"region_scope": "EU"}), memory_store=True, seed_demo=False)
