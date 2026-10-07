"""Builds the configured chat model, applying the free-only guard first, and wraps cloud models in the budget
wrapper (throttle, retries with backoff, hard call budget, ledger, optional disk cache)."""

from __future__ import annotations

from collections.abc import Callable, Mapping
from typing import Any

from tally.config import DEFAULT_MODELS, LLMProviderKind, Settings
from tally.llm.anthropic_provider import AnthropicModel
from tally.llm.base import ChatModel, LLMError
from tally.llm.budget import BudgetedModel, CallLedger, DiskCache, Throttle
from tally.llm.fake import FakeModel
from tally.llm.guards import ensure_free_models
from tally.llm.openai_compat import OpenAICompatibleModel

PlaybookLoader = Callable[[], Mapping[str, Mapping[str, Any]]]


def build_chat_model(
    settings: Settings,
    *,
    provider: LLMProviderKind | None = None,
    model: str | None = None,
    fallback_models: list[str] | None = None,
    playbook: PlaybookLoader | None = None,
) -> ChatModel:
    kind: LLMProviderKind = provider or settings.llm_provider
    name = model or (settings.llm_model if provider is None else "") or DEFAULT_MODELS[kind]
    fallbacks = settings.llm_fallback_models if fallback_models is None else fallback_models
    # Guards first: nothing is constructed (no client, no key read) for a refused model.
    if kind == "openrouter" and settings.require_free_models:
        ensure_free_models([name, *fallbacks])
    if kind == "fake":
        return FakeModel(playbook() if playbook else {})
    if kind == "anthropic":
        return AnthropicModel(
            model=name,
            api_key=settings.anthropic_api_key,
            effort=settings.anthropic_effort,
            timeout_seconds=settings.llm_timeout_seconds,
        )
    base_url = {
        "openrouter": settings.openrouter_base_url,
        "qwen": settings.qwen_base_url,
        "openai": settings.openai_base_url,
        "openai_compatible": settings.llm_base_url,
    }[kind]
    api_key = {
        "openrouter": settings.openrouter_api_key,
        "qwen": settings.qwen_api_key,
        "openai": settings.openai_api_key,
        "openai_compatible": settings.llm_api_key,
    }[kind]
    if kind == "openai_compatible" and not base_url:
        raise LLMError("TALLY_LLM_BASE_URL is required for the openai_compatible provider")
    return OpenAICompatibleModel(
        kind=kind,
        base_url=base_url,
        model=name,
        api_key=api_key,
        fallback_models=fallbacks if kind == "openrouter" else (),
        require_free=settings.require_free_models,
        timeout_seconds=settings.llm_timeout_seconds,
        reasoning_effort=settings.llm_reasoning_effort if kind == "openrouter" else "",
        enable_thinking=settings.qwen_enable_thinking if kind == "qwen" else None,
    )


def budgeted(model: ChatModel, settings: Settings, *, tag: str, cache: bool | None = None) -> ChatModel:
    """Cloud models get the throttle, retries, call budget and ledger; the fake model passes through."""
    if model.is_local:
        return model
    use_cache = settings.llm_cache if cache is None else cache
    ledger = CallLedger(settings.llm_ledger, settings.llm_max_calls)
    throttle = Throttle(settings.llm_min_seconds_between_requests)
    policy_fallback: ChatModel | None = None
    if isinstance(model, OpenAICompatibleModel) and model.kind == "openrouter" and model.fallback_models:
        # A provider-side 403 is not covered by OpenRouter's `models` list: ask the next free model directly.
        nxt, *rest = model.fallback_models
        alternative = OpenAICompatibleModel(
            kind="openrouter",
            base_url=settings.openrouter_base_url,
            model=nxt,
            api_key=settings.openrouter_api_key,
            fallback_models=rest,
            require_free=settings.require_free_models,
            timeout_seconds=settings.llm_timeout_seconds,
            reasoning_effort=settings.llm_reasoning_effort,
        )
        policy_fallback = BudgetedModel(alternative, ledger=ledger, cache=None, throttle=throttle,
                                        max_retries=settings.llm_max_retries, tag=f"{tag}:policy_fallback")  # fmt: skip
    return BudgetedModel(
        model,
        ledger=ledger,
        cache=DiskCache(settings.llm_cache_dir) if use_cache else None,
        throttle=throttle,
        max_retries=settings.llm_max_retries,
        tag=tag,
        policy_fallback=policy_fallback,
    )
