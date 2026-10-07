"""Claude via the official Anthropic SDK (optional; not used for the README's measured results)."""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any

import anthropic

from tally.llm.base import Completion, LLMError, LLMUnavailableError, Message, RetryableLLMError


class AnthropicModel:
    def __init__(
        self,
        *,
        model: str = "claude-sonnet-5",
        api_key: str | None = None,
        effort: str = "low",
        timeout_seconds: float = 120.0,
        client: anthropic.Anthropic | None = None,
    ) -> None:
        self.model = model
        self.effort = effort
        # The SDK resolves credentials itself (ANTHROPIC_API_KEY, ANTHROPIC_AUTH_TOKEN or an `ant auth login` profile).
        self._client = client or anthropic.Anthropic(api_key=api_key, timeout=timeout_seconds, max_retries=2)

    @property
    def label(self) -> str:
        return f"anthropic/{self.model}"

    @property
    def is_local(self) -> bool:
        return False

    def _params(self, messages: Sequence[Message], max_tokens: int) -> dict[str, Any]:
        system = "\n\n".join(m["content"] for m in messages if m["role"] == "system")
        turns = [{"role": m["role"], "content": m["content"]} for m in messages if m["role"] != "system"]
        # SQL generation from a short schema context needs little reasoning: low effort keeps latency and cost down.
        # Thinking tokens count toward max_tokens, so leave headroom above the visible answer length.
        params: dict[str, Any] = {
            "model": self.model,
            "max_tokens": max(max_tokens, 8000),
            "messages": turns,
            "output_config": {"effort": self.effort},
        }
        if system:
            params["system"] = system
        return params

    def complete(self, messages: Sequence[Message], *, max_tokens: int, temperature: float = 0.0) -> Completion:
        try:
            response = self._client.messages.create(**self._params(messages, max_tokens))
        except anthropic.RateLimitError as exc:
            raise RetryableLLMError(f"Anthropic rate limit: {exc.message}") from exc
        except anthropic.APIConnectionError as exc:
            raise LLMUnavailableError(f"cannot reach the Anthropic API: {exc}") from exc
        except anthropic.APIStatusError as exc:
            if exc.status_code >= 500:
                raise RetryableLLMError(f"Anthropic {exc.status_code}: {exc.message}") from exc
            raise LLMError(f"Anthropic {exc.status_code}: {exc.message}") from exc
        if response.stop_reason == "refusal":
            raise LLMError("the model declined to answer (stop_reason=refusal)")
        text = "".join(block.text for block in response.content if block.type == "text")
        if not text.strip():
            raise RetryableLLMError(f"empty answer (stop_reason={response.stop_reason})")
        return Completion(
            text=text,
            model=response.model,
            input_tokens=response.usage.input_tokens,
            output_tokens=response.usage.output_tokens,
        )
