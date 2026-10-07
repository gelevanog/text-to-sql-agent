"""The agent loop against the demo database with scripted model replies: self-correction, blocking without retries,
the cost guard, clarification policy, follow-ups, the answer-number check and the audit log."""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

import pytest

from tally.agent.store import InMemoryStore
from tally.services import Services, make_agent
from tests.conftest import ScriptedModel, sql_reply

pytestmark = pytest.mark.db

NET_BY_REGION = (
    "SELECT region_code, ROUND(SUM(net_revenue_usd), 2) AS net_revenue_usd FROM order_revenue "
    "WHERE ordered_at >= DATE '2026-07-01' AND ordered_at < DATE '2026-10-01' GROUP BY region_code ORDER BY 2 DESC"
)
Factory = Callable[..., tuple[Any, ScriptedModel]]


def first_number(result: Any) -> float:
    return float(result.rows[0][1])


def test_answers_with_sql_result_chart_and_checked_answer(agent_factory: Factory) -> None:
    def answer(messages: Any) -> str:
        return "In calendar Q3 2026 North America led net revenue."

    agent, model = agent_factory([sql_reply(NET_BY_REGION), answer])
    steps: list[str] = []
    result = agent.run("What was net revenue by region last calendar quarter?",
                       emit=lambda event, data: steps.append(data.get("kind", event)))  # fmt: skip
    assert result.status == "answered"
    assert result.views == ["order_revenue"] and result.expanded_sql.upper().startswith("WITH ORDER_REVENUE AS")
    assert result.columns == ["region_code", "net_revenue_usd"] and result.row_count == 5
    assert result.chart and result.chart["type"] == "bar"
    assert result.answer_source == "model" and result.llm_calls == 2
    assert steps[:6] == ["retrieval", "sql", "validation", "cost", "execution", "result"]
    assert steps[-1] == "done"
    prompt = model.prompts[0][1]["content"]
    assert "order_revenue" in prompt and "TODAY: 2026-10-01" in prompt and "email" not in prompt.split("###")[1]


def test_self_correction_after_a_database_error(agent_factory: Factory) -> None:
    broken = sql_reply("SELECT region_code, SUM(revenue) FROM order_revenue GROUP BY 1")
    agent, model = agent_factory([broken, sql_reply(NET_BY_REGION), "North America led."])
    result = agent.run("What was net revenue by region last calendar quarter?")
    assert result.status == "answered" and result.corrections == 1
    assert [a.stage for a in result.attempts] == ["validation", "ok"]
    correction = model.prompts[1][-1]["content"]
    assert "could not be used" in correction and "revenue" in correction


def test_self_correction_after_a_postgres_error(agent_factory: Factory) -> None:
    bad_types = sql_reply("SELECT region_code FROM order_revenue WHERE ordered_at > 'yesterday-ish'")
    agent, model = agent_factory([bad_types, sql_reply(NET_BY_REGION), "Done."])
    result = agent.run("What was net revenue by region last calendar quarter?")
    assert result.status == "answered" and result.attempts[0].stage == "execution"
    assert "PostgreSQL error" in model.prompts[1][-1]["content"]


def test_parse_errors_and_empty_results_are_retried_once(agent_factory: Factory) -> None:
    empty = sql_reply("SELECT count(*) AS n FROM orders WHERE status = 'Cancelled' GROUP BY status")
    fixed = sql_reply("SELECT count(*) AS n FROM orders WHERE status = 'cancelled'")
    agent, model = agent_factory(["not json at all", empty, fixed, "There were some cancelled orders."])
    result = agent.run("How many orders were cancelled?")
    assert result.status == "answered"
    assert [a.stage for a in result.attempts] == ["parse", "empty", "ok"]
    assert "returned no rows" in model.prompts[2][-1]["content"]


def test_correction_budget_is_bounded(agent_factory: Factory) -> None:
    bad = sql_reply("SELECT nope FROM orders")
    agent, model = agent_factory([bad, bad, bad], max_corrections=2)
    result = agent.run("How many orders?")
    assert result.status == "error" and result.llm_calls == 3 and "no working query" in result.error
    assert model.replies == []


def test_without_self_correction_the_first_error_is_final(agent_factory: Factory) -> None:
    agent, _ = agent_factory([sql_reply("SELECT nope FROM orders")], max_corrections=0)
    assert agent.run("How many orders?").status == "error"


@pytest.mark.parametrize(
    ("sql", "layer"),
    [
        ("DELETE FROM orders", "validator"),
        ("SELECT email FROM customers", "validator"),
        ("SELECT 1; DROP TABLE orders", "validator"),
        ("SELECT pg_sleep(30)", "validator"),
    ],
)
def test_unsafe_sql_is_blocked_without_a_second_try(agent_factory: Factory, sql: str, layer: str) -> None:
    agent, model = agent_factory([sql_reply(sql)])
    result = agent.run("Do something unsafe")
    assert result.status == "blocked" and result.blocked and result.blocked["layer"] == layer
    assert result.llm_calls == 1 and result.rows == [] and result.executed_sql == ""


def test_select_star_over_pii_is_corrected_or_blocked(agent_factory: Factory) -> None:
    star = sql_reply("SELECT * FROM customers")
    fixed = sql_reply("SELECT name, city FROM customers ORDER BY id LIMIT 5")
    agent, _ = agent_factory([star, fixed, "Five customers."])
    assert agent.run("Show some customers").status == "answered"
    agent, _ = agent_factory([star, star, star])
    result = agent.run("Show all customer data")
    assert result.status == "blocked" and "personal data" in result.blocked["reasons"][0]  # type: ignore[index]


def test_cost_guard_blocks_cross_joins(agent_factory: Factory) -> None:
    cross = sql_reply("SELECT count(*) FROM order_items a CROSS JOIN order_items b")
    agent, model = agent_factory([cross, cross, cross])
    result = agent.run("Count all pairs of order items")
    assert result.status == "blocked" and result.blocked and result.blocked["layer"] == "cost_guard"
    assert "rows" in result.blocked["reasons"][0]
    assert "too expensive" in model.prompts[1][-1]["content"]


def test_refusal_and_model_clarification(agent_factory: Factory) -> None:
    agent, _ = agent_factory([{"action": "refuse", "refusal": "That would change data."}])
    refused = agent.run("Please drop the orders table")
    assert refused.status == "refused" and refused.blocked == {"layer": "model", "reasons": ["That would change data."]}
    agent, _ = agent_factory(
        [{"action": "clarify", "clarification": {"question": "Which campaign?", "options": ["A", "B"]}}]
    )
    asked = agent.run("Show me the numbers for the big campaign")
    assert asked.status == "clarification" and asked.clarification["source"] == "model"  # type: ignore[index]


def test_clarification_policy_asks_before_any_model_call(agent_factory: Factory) -> None:
    agent, model = agent_factory([])
    result = agent.run("What was revenue last quarter?")
    assert result.status == "clarification" and result.llm_calls == 0
    assert result.clarification["id"] == "revenue_basis"  # type: ignore[index]
    assert [o["value"] for o in result.clarification["options"]] == ["net", "gross"]  # type: ignore[index]


def test_clarification_replies_resolve_and_rerun_the_original_question(services: Services) -> None:
    store = InMemoryStore()
    model = ScriptedModel([sql_reply(NET_BY_REGION.replace("region_code, ", "").replace(" GROUP BY region_code", "")
                                     .replace("ORDER BY 2 DESC", "")), "Net revenue was up."])  # fmt: skip
    agent = make_agent(services, llm=model, store=store)
    first = agent.run("What was revenue last quarter?")
    second = agent.run("Net revenue please", conversation_id=first.conversation_id)
    assert second.status == "clarification" and second.clarification["id"] == "period_basis"  # type: ignore[index]
    third = agent.run(
        "x", conversation_id=first.conversation_id, clarification={"id": "period_basis", "value": "calendar"}
    )
    assert third.status == "answered" and third.question == "What was revenue last quarter?"
    assert third.resolved == {"revenue_basis": "net", "period_basis": "calendar"}
    prompt = model.prompts[0][1]["content"]
    assert "CLARIFIED BY THE USER" in prompt and "net revenue after refunds" in prompt and "calendar quarters" in prompt


def test_assume_policy_uses_defaults_and_says_so(agent_factory: Factory) -> None:
    agent, model = agent_factory([sql_reply(NET_BY_REGION), "Net revenue by region."], clarify_policy="assume")
    result = agent.run("What was revenue by region last quarter?")
    assert result.status == "answered"
    assert any("net revenue after refunds" in a for a in result.assumptions)
    assert "Assumed" in model.prompts[0][1]["content"]


def test_follow_ups_see_the_previous_question_sql_and_result(services: Services) -> None:
    store = InMemoryStore()
    model = ScriptedModel(
        [
            sql_reply(NET_BY_REGION),
            "First.",
            sql_reply(NET_BY_REGION.replace("GROUP BY", "AND region_code = 'EU' GROUP BY")),
            "Second.",
        ]
    )
    agent = make_agent(services, llm=model, store=store)
    first = agent.run("What was net revenue by region last calendar quarter?")
    follow = agent.run("Only EU", conversation_id=first.conversation_id)
    assert follow.status == "answered" and follow.rows[0][0] == "EU"
    prompt = model.prompts[2][1]["content"]
    assert "CONVERSATION SO FAR" in prompt and "Previous question: What was net revenue by region" in prompt
    assert "order_revenue" in prompt and "first rows" in prompt
    assert "order_revenue" in follow.retrieval["views"]


def test_answer_numbers_are_checked_then_retried_then_templated(agent_factory: Factory) -> None:
    agent, model = agent_factory([sql_reply(NET_BY_REGION), "NA made $999,999.", "NA made $1,234,567."])
    result = agent.run("What was net revenue by region last calendar quarter?")
    assert result.answer_source == "template" and result.answer.startswith("Result:")
    assert result.answer_check and result.answer_check["unsupported"] == ["$1,234,567."[:-1]]
    assert "not in the result" in model.prompts[2][-1]["content"]
    top = first_number(result)
    agent, _ = agent_factory([sql_reply(NET_BY_REGION), "NA made $999,999.", f"NA made ${top:,.2f}."])
    assert agent.run("What was net revenue by region last calendar quarter?").answer_source == "model_retry"


def test_audit_log_has_no_result_data(services: Services) -> None:
    store = InMemoryStore()
    agent = make_agent(services, llm=ScriptedModel([sql_reply(NET_BY_REGION), "North America led."]), store=store)
    result = agent.run("What was net revenue by region last calendar quarter?")
    record = store.audit_log()[0]
    assert record.question.startswith("What was net revenue") and record.status == "answered"
    assert record.row_count == 5 and record.llm_calls == 2 and record.sql == result.executed_sql
    flattened = str(record.to_dict())
    assert str(round(first_number(result), 2)) not in flattened and "North America led" not in flattened


def test_fake_model_answers_benchmark_questions_offline(services: Services) -> None:
    result = services.agent.run("What was net revenue by region last calendar quarter vs the one before?")
    assert result.status == "answered" and result.row_count == 5 and result.model == "fake/demo"
    blocked = services.agent.run("Delete all cancelled orders")
    assert blocked.status == "blocked"
    unknown = services.agent.run("Something the offline model has never seen")
    assert unknown.status == "refused"
