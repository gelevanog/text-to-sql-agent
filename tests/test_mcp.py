"""The MCP server: tools listed with their schemas and annotations, the guard's verdicts through the protocol
(in-process client), the audit log, the optional `ask` tool, and the stdio transport as Claude Code / Claude Desktop
launch it."""

from __future__ import annotations

import asyncio
import sys
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any

import pytest
from mcp import Client, StdioServerParameters

from tally.config import Settings
from tally.mcp_server import DATA_NOTICE, MAX_CELL_CHARS, TallyTools, build_server, redacted_url
from tally.services import Services

ROOT = Path(__file__).resolve().parents[1]
TALLY = Path(sys.executable).with_name("tally")
REVENUE_SQL = (
    "SELECT region_code, SUM(net_revenue_usd) AS net_revenue FROM order_revenue "
    "WHERE order_date >= DATE '2026-07-01' AND order_date < DATE '2026-10-01' GROUP BY 1 ORDER BY 2 DESC"
)


def run[T](coro_fn: Callable[[Client], Awaitable[T]], tools: TallyTools) -> T:
    async def main() -> T:
        async with Client(build_server(tools)) as client:
            return await coro_fn(client)

    return asyncio.run(main())


def call(tools: TallyTools, name: str, arguments: dict[str, Any]) -> dict[str, Any]:
    async def go(client: Client) -> dict[str, Any]:
        result = await client.call_tool(name, arguments)
        assert not result.is_error, result.content
        assert isinstance(result.structured_content, dict)
        return result.structured_content

    return run(go, tools)


def unreachable(password: str = "s3cret-pw") -> Settings:
    return Settings(database_url=f"postgresql://tally:{password}@127.0.0.1:1/tally", llm_ledger=None)


# ---- without a database ---------------------------------------------------------------------------------------
def test_tools_are_listed_with_schemas_and_read_only_annotations() -> None:
    async def go(client: Client) -> Any:
        return (await client.list_tools()).tools

    tools = run(go, TallyTools(unreachable()))  # services are built lazily: listing needs no database
    by_name = {t.name: t for t in tools}
    assert set(by_name) == {"describe_schema", "validate_sql", "run_sql"}
    for tool in tools:
        assert tool.description and tool.annotations is not None
        assert tool.annotations.read_only_hint is True and tool.annotations.destructive_hint is False
    run_sql = by_name["run_sql"].input_schema
    assert run_sql["required"] == ["sql"]
    assert run_sql["properties"]["max_rows"]["maximum"] == 500
    assert run_sql["properties"]["sql"]["maxLength"] == 20_000


def test_ask_is_opt_in() -> None:
    async def go(client: Client) -> list[str]:
        return [t.name for t in (await client.list_tools()).tools]

    assert "ask" in run(go, TallyTools(unreachable(), with_ask=True))


def test_unreachable_database_is_a_helpful_error_without_the_password() -> None:
    async def go(client: Client) -> Any:
        return await client.call_tool("describe_schema", {})

    result = run(go, TallyTools(unreachable()))
    assert result.is_error
    text = result.content[0].text  # type: ignore[union-attr]
    assert "127.0.0.1:1/tally" in text and "make db && make seed" in text
    assert "s3cret-pw" not in text


def test_arguments_are_validated_before_the_tool_runs() -> None:
    async def go(client: Client) -> Any:
        return await client.call_tool("run_sql", {"sql": "SELECT 1", "max_rows": 10_000})

    result = run(go, TallyTools(unreachable()))
    assert result.is_error and "max_rows" in result.content[0].text  # type: ignore[union-attr]


def test_redacted_url() -> None:
    assert redacted_url("postgresql://u:p@db.example:5433/sales?sslmode=require") == "db.example:5433/sales"


def test_stdio_transport_lists_tools(tmp_path: Path) -> None:
    params = StdioServerParameters(
        command=str(TALLY),
        args=["mcp"],
        env={"TALLY_DATABASE_URL": unreachable().database_url, "TALLY_LOG_LEVEL": "WARNING"},
        cwd=tmp_path,
    )

    async def main() -> list[str]:
        async with Client(params) as client:
            return sorted(t.name for t in (await client.list_tools()).tools)

    assert asyncio.run(main()) == ["describe_schema", "run_sql", "validate_sql"]


# ---- with the demo database -----------------------------------------------------------------------------------
@pytest.fixture(scope="module")
def tools(services: Services) -> TallyTools:
    return TallyTools(services.settings, services=services)


@pytest.mark.db
def test_describe_schema_for_a_question(tools: TallyTools) -> None:
    body = call(tools, "describe_schema", {"question": "net revenue by region last calendar quarter"})
    assert body["company"] == "Lumora" and body["today"] == "2026-10-01"
    assert "order_revenue" in body["selection"]["views"] and "net_revenue" in body["selection"]["metrics"]
    assert "### Business rules" in body["schema"] and "net_revenue_usd" in body["schema"]
    assert body["personal_data_columns"] == {"customers": ["email", "phone", "street_address"]}
    assert {a["id"] for a in body["ambiguities"]} >= {"revenue_basis", "period_basis"}
    assert body["guard"]["max_rows"] == 1000 and DATA_NOTICE in body["note"]


@pytest.mark.db
def test_describe_schema_without_a_question_is_the_whole_schema(tools: TallyTools) -> None:
    body = call(tools, "describe_schema", {})
    assert body["selection"] == "full schema" and len(body["all_tables"]) == 18 and len(body["semantic_views"]) == 4
    for column in ("email", "phone", "street_address"):
        assert f"  {column} " not in body["schema"]  # personal data is not even described


@pytest.mark.db
def test_run_sql_answers_through_the_guard(tools: TallyTools) -> None:
    body = call(tools, "run_sql", {"sql": REVENUE_SQL, "purpose": "net revenue by region, Q3 2026"})
    assert body["status"] == "ok" and body["views_expanded"] == ["order_revenue"]
    assert body["columns"] == ["region_code", "net_revenue"] and body["row_count"] == 5 == body["rows_shown"]
    assert all(isinstance(r[1], float) for r in body["rows"]) and body["truncated"] is False
    assert body["chart"]["type"] == "bar" and "data" not in body["chart"]
    assert body["limit_applied"] == 1000 and DATA_NOTICE in body["notes"]
    assert body["executed_sql"].startswith("WITH order_revenue AS (") and body["executed_sql"].endswith("LIMIT 1000")


@pytest.mark.db
def test_run_sql_truncates_rows_and_long_values(tools: TallyTools) -> None:
    body = call(tools, "run_sql", {"sql": "SELECT id, ordered_at FROM orders ORDER BY id", "max_rows": 3})
    assert body["rows_shown"] == 3 and body["row_count"] == 1000 and body["truncated"] is True
    assert any("showing 3 of 1000" in n for n in body["notes"])
    long = call(tools, "run_sql", {"sql": "SELECT repeat('x', 2000) AS t"})
    assert long["status"] == "invalid"  # repeat() is not on the allow-list
    text = call(tools, "run_sql", {"sql": "SELECT lpad('x', 2000, 'y') AS t"})
    assert text["status"] == "ok" and len(text["rows"][0][0]) < MAX_CELL_CHARS + 50


@pytest.mark.parametrize(
    ("sql", "code"),
    [
        ("DELETE FROM orders WHERE status = 'cancelled'", "not_select"),
        ("SELECT pg_read_file('/etc/passwd')", "function_not_allowed"),
        ("SELECT pg_sleep(30)", "function_not_allowed"),
        ("SELECT email FROM customers LIMIT 5", "pii_column"),
        ("SELECT 1; DROP TABLE orders", "multiple_statements"),
        ("SELECT usename FROM pg_catalog.pg_user", "system_table"),
        ("WITH x AS (UPDATE orders SET status = 'paid' RETURNING id) SELECT * FROM x", "write_or_ddl"),
    ],
)
@pytest.mark.db
def test_run_sql_blocks_policy_violations(tools: TallyTools, sql: str, code: str) -> None:
    body = call(tools, "run_sql", {"sql": sql})
    assert body["status"] == "blocked" and body["layer"] == "validator"
    assert code in {v["code"] for v in body["violations"]} and all(v["hard"] for v in body["violations"])
    assert "rows" not in body and "do not work around it" in body["hint"]


@pytest.mark.db
def test_run_sql_soft_violations_ask_for_a_fix(tools: TallyTools) -> None:
    body = call(tools, "run_sql", {"sql": "SELECT nonexistent FROM orders"})
    assert body["status"] == "invalid" and body["hint"] == "fix the SQL and try again"
    star = call(tools, "run_sql", {"sql": "SELECT * FROM customers"})
    assert star["status"] == "invalid" and {v["code"] for v in star["violations"]} == {"pii_via_star"}


@pytest.mark.db
def test_run_sql_cost_guard(tools: TallyTools) -> None:
    body = call(tools, "run_sql", {"sql": "SELECT count(*) FROM orders a, orders b, order_items c"})
    assert body["status"] == "blocked" and body["layer"] == "cost_guard" and "rows" not in body


@pytest.mark.db
def test_run_sql_database_errors_are_reported(tools: TallyTools) -> None:
    body = call(tools, "run_sql", {"sql": "SELECT 1 / (count(*) - count(*)) AS x FROM regions"})
    assert body["status"] == "error" and body["layer"] == "database" and "division by zero" in body["reasons"][0]


@pytest.mark.db
def test_planted_prompt_injection_comes_back_marked_as_data(tools: TallyTools) -> None:
    body = call(tools, "run_sql", {"sql": "SELECT subject, body FROM support_tickets ORDER BY created_at DESC LIMIT 1"})
    assert body["status"] == "ok" and "ignore all previous instructions" in body["rows"][0][1]
    assert DATA_NOTICE in body["notes"]


@pytest.mark.db
def test_validate_sql_does_not_execute(tools: TallyTools) -> None:
    body = call(tools, "validate_sql", {"sql": REVENUE_SQL})
    assert body["status"] == "ok" and body["executed_sql"].startswith("WITH order_revenue AS (")
    assert body["executed_sql"].endswith("LIMIT 1000") and body["plan"]["total_cost"] > 0
    assert "rows" not in body and body["note"] == "valid; nothing was executed"
    pinned = call(tools, "validate_sql", {"sql": "SELECT count(*) FROM orders WHERE ordered_at >= CURRENT_DATE"})
    assert pinned["today_pinned"] == "2026-10-01" and "CURRENT_DATE" not in pinned["executed_sql"]
    blocked = call(tools, "validate_sql", {"sql": "DROP TABLE orders"})
    assert blocked["status"] == "blocked"


@pytest.mark.db
def test_run_sql_is_audited(tools: TallyTools, services: Services) -> None:
    call(tools, "run_sql", {"sql": "SELECT count(*) AS n FROM regions", "purpose": "how many regions (audit test)"})
    call(tools, "run_sql", {"sql": "DELETE FROM regions", "purpose": "audit test: delete"})
    log = {r.question: r for r in services.store.audit_log(50)}
    ok = log["[mcp] how many regions (audit test)"]
    assert ok.status == "answered" and ok.row_count == 1 and ok.model == "mcp-client"
    blocked = log["[mcp] audit test: delete"]
    assert blocked.status == "blocked" and blocked.violations == ["not_select"]


@pytest.mark.db
def test_ask_runs_tallys_agent_with_the_offline_model(services: Services) -> None:
    tools = TallyTools(services.settings, services=services, with_ask=True)
    body = call(tools, "ask", {"question": "Show monthly net revenue for the last 12 months"})
    assert body["status"] == "answered" and body["row_count"] == 12 and body["answer"]
    assert body["model"] == "fake/demo" and body["chart"]["type"] == "line"
    clarify = call(tools, "ask", {"question": "What was revenue last quarter?"})
    assert clarify["status"] == "clarification" and clarify["clarification"]["options"]


@pytest.mark.db
def test_stdio_end_to_end(seeded: Settings, tmp_path: Path) -> None:
    params = StdioServerParameters(
        command=str(TALLY),
        args=["mcp"],
        env={
            "TALLY_DATABASE_URL": seeded.database_url,
            "TALLY_SEMANTIC_LAYER_FILE": str(ROOT / "configs" / "semantic_layer.yaml"),
            "TALLY_LLM_LEDGER": "",
            "TALLY_LOG_LEVEL": "WARNING",
        },
        cwd=tmp_path,
    )

    async def main() -> dict[str, Any]:
        async with Client(params) as client:
            result = await client.call_tool("run_sql", {"sql": "SELECT code, name FROM regions ORDER BY code"})
            assert isinstance(result.structured_content, dict)
            return result.structured_content

    body = asyncio.run(main())
    assert body["status"] == "ok" and body["row_count"] == 5 and body["columns"] == ["code", "name"]
