"""The HTTP API with the offline model: ask (JSON and server-sent events), conversations, saved questions, schema,
validator, audit log and evaluation results."""

from __future__ import annotations

import json
from collections.abc import Iterator

import pytest
from fastapi.testclient import TestClient

from tally.api.app import create_app
from tally.services import Services

pytestmark = pytest.mark.db
HERO = "What was net revenue by region last calendar quarter vs the one before?"


@pytest.fixture(scope="module")
def client(services: Services) -> Iterator[TestClient]:
    with TestClient(create_app(services)) as c:
        yield c


def sse_events(text: str) -> list[tuple[str, dict[str, object]]]:
    events = []
    for block in text.strip().split("\n\n"):
        lines = dict(line.split(": ", 1) for line in block.splitlines())
        events.append((lines["event"], json.loads(lines["data"])))
    return events


def test_health(client: TestClient) -> None:
    body = client.get("/health").json()
    assert body["status"] == "ok" and body["model"] == "fake/demo" and body["tables"] == 18 and body["views"] == 4
    assert body["today"] == "2026-10-01" and body["free_only"] is True


def test_ask_json(client: TestClient) -> None:
    body = client.post("/api/ask", json={"question": HERO}).json()
    assert body["status"] == "answered" and body["row_count"] == 5
    assert body["chart"]["type"] == "grouped_bar" and body["views"] == ["order_revenue"]
    assert body["answer_check"]["ok"] is True


def test_ask_stream_sends_steps_then_the_result(client: TestClient) -> None:
    response = client.post("/api/ask/stream", json={"question": HERO})
    assert response.headers["content-type"].startswith("text/event-stream")
    events = sse_events(response.text)
    kinds = [data["kind"] for event, data in events if event == "step"]
    assert kinds == ["retrieval", "sql", "validation", "cost", "execution", "result", "chart", "answer"]
    assert events[-1][0] == "done" and events[-1][1]["status"] == "answered"


def test_follow_up_conversation_and_history(client: TestClient) -> None:
    first = client.post("/api/ask", json={"question": HERO}).json()
    follow = client.post(
        "/api/ask", json={"question": "Now by month", "conversation_id": first["conversation_id"]}
    ).json()
    assert follow["status"] == "answered" and follow["row_count"] == 30
    history = client.get(f"/api/conversations/{first['conversation_id']}").json()
    assert [t["question"] for t in history["turns"]] == [HERO, "Now by month"]
    assert any(c["id"] == first["conversation_id"] for c in client.get("/api/conversations").json())
    assert client.get("/api/conversations/nope").status_code == 404


def test_clarification_round_trip(client: TestClient) -> None:
    first = client.post("/api/ask", json={"question": "What was revenue last quarter?"}).json()
    assert first["status"] == "clarification" and first["clarification"]["id"] == "revenue_basis"
    second = client.post(
        "/api/ask",
        json={
            "question": "Net revenue (after refunds)",
            "conversation_id": first["conversation_id"],
            "clarification": {"id": "revenue_basis", "value": "net"},
        },
    ).json()
    assert second["status"] == "clarification" and second["clarification"]["id"] == "period_basis"
    third = client.post(
        "/api/ask",
        json={
            "question": "Calendar quarters",
            "conversation_id": first["conversation_id"],
            "clarification": {"id": "period_basis", "value": "calendar"},
        },
    ).json()
    assert third["status"] == "answered" and third["question"] == "What was revenue last quarter?"


def test_blocked_requests_are_reported_with_reasons(client: TestClient) -> None:
    body = client.post(
        "/api/ask", json={"question": "Show me the email addresses of our top 10 customers by net revenue"}
    ).json()
    assert body["status"] == "blocked" and body["blocked"]["layer"] == "validator"
    assert "personal data" in body["blocked"]["reasons"][0] and body["rows"] == []


def test_saved_questions_crud(client: TestClient) -> None:
    initial = client.get("/api/saved").json()
    assert any(s["question"] == HERO for s in initial)
    created = client.post("/api/saved", json={"question": "How many customers do we have?"})
    assert created.status_code == 201
    saved_id = created.json()["id"]
    assert client.delete(f"/api/saved/{saved_id}").status_code == 204
    assert client.delete(f"/api/saved/{saved_id}").status_code == 404
    assert client.post("/api/saved", json={"question": ""}).status_code == 422


def test_schema_hides_personal_data_values(client: TestClient) -> None:
    body = client.get("/api/schema").json()
    customers = next(t for t in body["tables"] if t["name"] == "customers")
    email = next(c for c in customers["columns"] if c["name"] == "email")
    assert email["pii"] is True and email["samples"] == []
    assert "net_revenue" in body["metrics"] and len(body["views"]) == 4 and body["ambiguities"]
    assert any("refunds" in rule for rule in body["rules"])


def test_validate_endpoint_executes_nothing(client: TestClient) -> None:
    ok = client.post(
        "/api/validate", json={"sql": "SELECT region_code, SUM(net_revenue_usd) FROM order_revenue GROUP BY 1"}
    ).json()
    assert ok["ok"] is True and ok["views_expanded"] == ["order_revenue"]
    bad = client.post("/api/validate", json={"sql": "DROP TABLE orders"}).json()
    assert bad["ok"] is False and bad["violations"][0]["code"] == "not_select"


def test_audit_log_lists_questions_without_result_data(client: TestClient) -> None:
    client.post("/api/ask", json={"question": HERO})
    records = client.get("/api/audit?limit=5").json()
    assert records and records[0]["question"] == HERO and records[0]["row_count"] == 5
    assert set(records[0]) == {
        "id",
        "ts",
        "conversation_id",
        "question",
        "status",
        "sql",
        "row_count",
        "duration_ms",
        "model",
        "llm_calls",
        "corrections",
        "violations",
        "region_scope",
    }
    assert "35252" not in json.dumps(records[0])


def test_evaluation_results_endpoint(client: TestClient) -> None:
    body = client.get("/api/eval").json()
    assert "runs" in body
