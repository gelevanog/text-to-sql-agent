"""Model providers without network: the free-only guard, request shapes (OpenRouter, Qwen Cloud), error mapping,
the budget wrapper, the Anthropic provider with a mocked client, the fake model and reply parsing."""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import httpx
import pytest

from tally.agent.prompts import ReplyParseError, parse_plan
from tally.config import DEFAULT_QWEN_BASE_URL, Settings
from tally.llm.anthropic_provider import AnthropicModel
from tally.llm.base import BudgetExceededError, LLMError, Message, PolicyViolationError, RetryableLLMError
from tally.llm.budget import BudgetedModel, CallLedger, DiskCache, Throttle
from tally.llm.embeddings import OpenRouterEmbedder
from tally.llm.factory import budgeted, build_chat_model
from tally.llm.fake import FakeModel, playbook_key
from tally.llm.guards import ensure_free_models, ensure_served_free
from tally.llm.openai_compat import OpenAICompatibleModel

MESSAGES: list[Message] = [{"role": "system", "content": "s"}, {"role": "user", "content": "u"}]
# Placeholder credentials, assembled at runtime so no file contains a key-shaped literal.
FAKE_KEY = "-".join(["test", "key", "not", "real"])


def ok_response(model: str, content: str = '{"action": "sql", "sql": "SELECT 1"}') -> dict[str, Any]:
    return {
        "model": model,
        "choices": [{"message": {"content": content}, "finish_reason": "stop"}],
        "usage": {"prompt_tokens": 10, "completion_tokens": 5},
    }


class Recorder:
    def __init__(self, responses: list[httpx.Response]) -> None:
        self.responses = responses
        self.requests: list[httpx.Request] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        return self.responses.pop(0)

    @property
    def transport(self) -> httpx.MockTransport:
        return httpx.MockTransport(self)


# ---- free-only guard --------------------------------------------------------------------------------------------
def test_free_only_guard_refuses_paid_ids_and_paid_fallbacks() -> None:
    ensure_free_models(["nvidia/nemotron-3-super-120b-a12b:free", "google/gemma-4-31b-it:free"])
    with pytest.raises(PolicyViolationError):
        ensure_free_models(["openai/gpt-5"])
    with pytest.raises(PolicyViolationError):
        OpenAICompatibleModel(
            kind="openrouter",
            base_url="https://openrouter.ai/api/v1",
            api_key=FAKE_KEY,
            model="x/free-model:free",
            fallback_models=["anthropic/claude-sonnet-5"],
        )


def test_free_only_guard_rejects_an_answer_served_by_a_paid_model() -> None:
    ensure_served_free("vendor/model:free")
    ensure_served_free(None)
    recorder = Recorder([httpx.Response(200, json=ok_response("vendor/paid-model"))])
    model = OpenAICompatibleModel(
        kind="openrouter",
        base_url="https://openrouter.ai/api/v1",
        api_key=FAKE_KEY,
        model="vendor/model:free",
        transport=recorder.transport,
    )
    with pytest.raises(PolicyViolationError):
        model.complete(MESSAGES, max_tokens=100)


def test_factory_applies_the_guard_before_building_anything() -> None:
    settings = Settings(llm_provider="openrouter", llm_model="openai/gpt-5", openrouter_api_key=None)
    with pytest.raises(PolicyViolationError):
        build_chat_model(settings)  # refused before the missing key is even noticed
    relaxed = settings.model_copy(update={"require_free_models": False, "openrouter_api_key": FAKE_KEY})
    assert build_chat_model(relaxed).label == "openrouter/openai/gpt-5"


def test_free_only_guard_does_not_apply_to_other_providers() -> None:
    model = OpenAICompatibleModel(
        kind="qwen", base_url=DEFAULT_QWEN_BASE_URL, api_key=FAKE_KEY, model="qwen3-coder-next"
    )
    assert not model.require_free


# ---- request shapes --------------------------------------------------------------------------------------------
def test_openrouter_request_shape() -> None:
    recorder = Recorder([httpx.Response(200, json=ok_response("vendor/model:free"))])
    model = OpenAICompatibleModel(
        kind="openrouter",
        base_url="https://openrouter.ai/api/v1/",
        api_key=FAKE_KEY,
        model="vendor/model:free",
        fallback_models=["other/model:free"],
        reasoning_effort="low",
        transport=recorder.transport,
    )
    completion = model.complete(MESSAGES, max_tokens=321, temperature=0.0)
    request = recorder.requests[0]
    body = json.loads(request.content)
    assert str(request.url) == "https://openrouter.ai/api/v1/chat/completions"
    assert request.headers["authorization"] == f"Bearer {FAKE_KEY}"
    assert request.headers["x-title"] == "Tally"
    assert body["model"] == "vendor/model:free"
    assert body["models"] == ["vendor/model:free", "other/model:free"]
    assert body["reasoning"] == {"effort": "low"}
    assert body["max_tokens"] == 321 and body["messages"] == MESSAGES
    assert completion.model == "vendor/model:free" and completion.input_tokens == 10


def test_qwen_provider_request_shape() -> None:
    recorder = Recorder([httpx.Response(200, json=ok_response("qwen3-coder-next"))])
    settings = Settings(llm_provider="qwen", qwen_api_key=FAKE_KEY, qwen_enable_thinking=False)
    model = build_chat_model(settings)
    assert isinstance(model, OpenAICompatibleModel)
    model._transport = recorder.transport
    model.complete(MESSAGES, max_tokens=200)
    request = recorder.requests[0]
    body = json.loads(request.content)
    assert str(request.url) == f"{DEFAULT_QWEN_BASE_URL}/chat/completions"
    assert request.headers["authorization"] == f"Bearer {FAKE_KEY}"
    assert body["model"] == "qwen3-coder-next"
    assert body["enable_thinking"] is False
    assert "models" not in body and "reasoning" not in body and "x-title" not in request.headers


def test_qwen_settings_accept_the_dashscope_variable_and_a_custom_endpoint(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DASHSCOPE_API_KEY", FAKE_KEY)
    monkeypatch.setenv("TALLY_QWEN_BASE_URL", "https://dashscope-intl.aliyuncs.com/compatible-mode/v1")
    monkeypatch.setenv("TALLY_LLM_MODEL", "qwen3.8-max")
    settings = Settings(llm_provider="qwen")
    model = build_chat_model(settings)
    assert model.label == "qwen/qwen3.8-max"
    assert model._url == "https://dashscope-intl.aliyuncs.com/compatible-mode/v1/chat/completions"  # type: ignore[attr-defined]


def test_missing_keys_fail_clearly() -> None:
    with pytest.raises(LLMError, match="QWEN_API_KEY"):
        OpenAICompatibleModel(kind="qwen", base_url=DEFAULT_QWEN_BASE_URL, model="qwen3-coder-next")
    with pytest.raises(LLMError, match="TALLY_LLM_BASE_URL"):
        build_chat_model(Settings(llm_provider="openai_compatible", llm_model="m"))


@pytest.mark.parametrize(
    ("status", "payload", "error"),
    [
        (429, {"error": {"message": "rate-limited upstream"}}, RetryableLLMError),
        (503, {"error": {"message": "overloaded"}}, RetryableLLMError),
        (200, {"error": {"code": 502, "message": "upstream failed"}}, RetryableLLMError),
        (400, {"error": {"message": "bad request"}}, LLMError),
        (200, {"model": "m", "choices": [{"message": {"content": ""}, "finish_reason": "length"}]}, LLMError),
        (200, {"model": "m", "choices": [{"message": {"content": "In APAC"}, "finish_reason": "length"}]}, LLMError),
    ],
)
def test_error_mapping(status: int, payload: dict[str, Any], error: type[Exception]) -> None:
    recorder = Recorder([httpx.Response(status, json=payload, headers={"retry-after": "2"})])
    model = OpenAICompatibleModel(
        kind="openai", base_url="https://api.example.test/v1", api_key=FAKE_KEY, model="m", transport=recorder.transport
    )
    with pytest.raises(error) as info:
        model.complete(MESSAGES, max_tokens=10)
    if status == 429:
        assert info.value.retry_after == 2.0  # type: ignore[attr-defined]


# ---- budget wrapper ----------------------------------------------------------------------------------------------
def test_budget_wrapper_retries_records_and_caches(tmp_path: Path) -> None:
    recorder = Recorder(
        [
            httpx.Response(429, json={"error": {"message": "rate-limited upstream"}}),
            httpx.Response(200, json=ok_response("vendor/model:free")),
        ]
    )
    inner = OpenAICompatibleModel(
        kind="openrouter",
        base_url="https://openrouter.ai/api/v1",
        api_key=FAKE_KEY,
        model="vendor/model:free",
        transport=recorder.transport,
    )
    ledger_path = tmp_path / "calls.jsonl"
    model = BudgetedModel(
        inner,
        ledger=CallLedger(ledger_path, 10),
        cache=DiskCache(tmp_path / "cache"),
        throttle=Throttle(0),
        retry_base_seconds=0.0,
        tag="test",
    )
    prompt: list[Message] = [{"role": "user", "content": "prompt-text-that-must-not-be-logged"}]
    first = model.complete(prompt, max_tokens=50)
    again = model.complete(prompt, max_tokens=50)
    rows = [json.loads(line) for line in ledger_path.read_text().splitlines()]
    assert [r["status"] for r in rows] == ["retryable_error", "ok"]
    assert all(r["requested_model"] == "vendor/model:free" for r in rows)
    assert "prompt-text-that-must-not-be-logged" not in ledger_path.read_text()  # never prompts
    assert again.cached and again.text == first.text
    assert len(recorder.requests) == 2  # the cached call made no request


def test_budget_wrapper_stops_at_the_hard_budget(tmp_path: Path) -> None:
    recorder = Recorder([httpx.Response(200, json=ok_response("m"))])
    inner = OpenAICompatibleModel(
        kind="openai", base_url="https://api.example.test/v1", api_key=FAKE_KEY, model="m", transport=recorder.transport
    )
    ledger_path = tmp_path / "calls.jsonl"
    ledger_path.write_text('{"status": "ok"}\n' * 3)
    model = BudgetedModel(inner, ledger=CallLedger(ledger_path, 3), cache=None, throttle=None)
    with pytest.raises(BudgetExceededError):
        model.complete(MESSAGES, max_tokens=10)
    assert recorder.requests == []


def test_fake_model_is_not_wrapped() -> None:
    fake = FakeModel({})
    assert budgeted(fake, Settings(), tag="x") is fake


# ---- Anthropic ----------------------------------------------------------------------------------------------------
class FakeMessages:
    def __init__(self) -> None:
        self.params: dict[str, Any] = {}

    def create(self, **params: Any) -> Any:
        self.params = params
        return SimpleNamespace(
            stop_reason="end_turn",
            model="claude-sonnet-5",
            content=[SimpleNamespace(type="text", text='{"action": "refuse", "refusal": "no"}')],
            usage=SimpleNamespace(input_tokens=7, output_tokens=3),
        )


def test_anthropic_provider_request_shape() -> None:
    messages = FakeMessages()
    client = SimpleNamespace(messages=messages)
    model = AnthropicModel(model="claude-sonnet-5", client=client)  # type: ignore[arg-type]
    completion = model.complete(MESSAGES, max_tokens=100)
    assert messages.params["system"] == "s"
    assert messages.params["messages"] == [{"role": "user", "content": "u"}]
    assert messages.params["output_config"] == {"effort": "low"}
    assert messages.params["max_tokens"] >= 100
    assert completion.text.startswith('{"action"') and completion.model == "claude-sonnet-5"
    assert model.label == "anthropic/claude-sonnet-5"


# ---- embeddings ---------------------------------------------------------------------------------------------------
def test_embeddings_guarded_cached_and_recorded(tmp_path: Path) -> None:
    with pytest.raises(PolicyViolationError):
        OpenRouterEmbedder(model="vendor/paid-embedder", api_key=FAKE_KEY)
    recorder = Recorder(
        [
            httpx.Response(
                200, json={"data": [{"index": 1, "embedding": [0.0, 1.0]}, {"index": 0, "embedding": [1.0, 0.0]}]}
            )
        ]
    )
    ledger = CallLedger(tmp_path / "calls.jsonl", 5)
    embed = OpenRouterEmbedder(
        model="liquid/lfm-2.5-embedding-350m:free",
        api_key=FAKE_KEY,
        cache_dir=tmp_path,
        ledger=ledger,
        transport=recorder.transport,
    )
    assert embed(["a", "b"]) == [[1.0, 0.0], [0.0, 1.0]]
    assert embed(["b"]) == [[0.0, 1.0]]  # from the cache
    assert len(recorder.requests) == 1
    assert json.loads((tmp_path / "calls.jsonl").read_text())["tag"] == "embeddings"


# ---- fake model and parsing --------------------------------------------------------------------------------------
def test_fake_model_follows_the_playbook_with_follow_ups() -> None:
    playbook = {
        playbook_key("Show orders"): {"action": "sql", "sql": "SELECT 1"},
        playbook_key("Only EU", "Show orders"): {"action": "sql", "sql": "SELECT 2"},
    }
    fake = FakeModel(playbook)
    first = fake.complete(
        [{"role": "system", "content": "x"}, {"role": "user", "content": "QUESTION: Show orders?"}], max_tokens=10
    )
    assert json.loads(first.text)["sql"] == "SELECT 1"
    follow = "[1] Previous question: Show orders\nQUESTION: only EU"
    second = fake.complete([{"role": "system", "content": "x"}, {"role": "user", "content": follow}], max_tokens=10)
    assert json.loads(second.text)["sql"] == "SELECT 2"
    unknown = fake.complete(
        [{"role": "system", "content": "x"}, {"role": "user", "content": "QUESTION: ?"}], max_tokens=10
    )
    assert json.loads(unknown.text)["action"] == "refuse"


@pytest.mark.parametrize(
    "text",
    [
        '{"action": "sql", "sql": "SELECT 1", "plan": "p"}',
        'Sure! ```json\n{"action": "sql", "sql": "SELECT 1"}\n``` hope that helps',
        '<think>{"draft": 1}</think>{"action": "sql", "sql": "SELECT 1"}',
        "```sql\nSELECT 1\n```",
        '{"sql": "SELECT 1"}',
    ],
)
def test_reply_parsing_is_robust(text: str) -> None:
    plan = parse_plan(text)
    assert plan.action == "sql" and plan.sql == "SELECT 1"


def test_reply_parsing_rejects_garbage_and_keeps_clarifications() -> None:
    with pytest.raises(ReplyParseError):
        parse_plan("I think you should look at revenue.")
    with pytest.raises(ReplyParseError):
        parse_plan('{"action": "sql", "sql": ""}')
    plan = parse_plan('{"action": "clarify", "clarification": {"question": "Which?", "options": ["a", "b"]}}')
    assert plan.action == "clarify" and plan.clarification_options == ["a", "b"]


def test_a_provider_403_is_retried_once_on_the_next_free_model(tmp_path: Path) -> None:
    denied = Recorder([httpx.Response(403, json={"error": {"message": "Access denied by security policy."}})])
    backup = Recorder([httpx.Response(200, json=ok_response("other/model:free"))])
    ledger = CallLedger(tmp_path / "calls.jsonl", 10)
    primary = OpenAICompatibleModel(
        kind="openrouter",
        base_url="https://openrouter.ai/api/v1",
        api_key=FAKE_KEY,
        model="vendor/model:free",
        fallback_models=["other/model:free"],
        transport=denied.transport,
    )
    alternative = OpenAICompatibleModel(
        kind="openrouter",
        base_url="https://openrouter.ai/api/v1",
        api_key=FAKE_KEY,
        model="other/model:free",
        transport=backup.transport,
    )
    model = BudgetedModel(
        primary,
        ledger=ledger,
        cache=None,
        throttle=None,
        policy_fallback=BudgetedModel(alternative, ledger=ledger, cache=None, throttle=None),
    )
    assert model.complete(MESSAGES, max_tokens=10).model == "other/model:free"
    rows = [json.loads(line) for line in (tmp_path / "calls.jsonl").read_text().splitlines()]
    assert [(r["requested_model"], r["status"]) for r in rows] == [
        ("vendor/model:free", "error"),
        ("other/model:free", "ok"),
    ]


def test_factory_wires_the_policy_fallback() -> None:
    settings = Settings(
        llm_provider="openrouter",
        openrouter_api_key=FAKE_KEY,
        llm_model="a/b:free",
        llm_fallback_models=["c/d:free", "e/f:free"],
        llm_ledger=None,
    )
    model = budgeted(build_chat_model(settings), settings, tag="t")
    assert isinstance(model, BudgetedModel) and model.policy_fallback is not None
    assert model.policy_fallback.label == "openrouter/c/d:free"
