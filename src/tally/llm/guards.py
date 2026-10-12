"""The free-only guard (on by default): refuses any OpenRouter model id that does not end in ``:free``, checks the
fallback list too, and rejects an answer that OpenRouter served from a non-free model. A client who wants paid
models turns it off deliberately with TALLY_REQUIRE_FREE_MODELS=false. The metered `gateway` provider (any
OpenAI-compatible endpoint that bills per token) is refused unless TALLY_ALLOW_PAID_MODELS=true."""

from __future__ import annotations

from collections.abc import Iterable

from tally.llm.base import PolicyViolationError


def ensure_free_models(model_ids: Iterable[str]) -> None:
    paid = [model for model in model_ids if not str(model).endswith(":free")]
    if paid:
        raise PolicyViolationError(
            f"free-only guard: refusing non-free OpenRouter model id(s): {', '.join(paid)} "
            "(set TALLY_REQUIRE_FREE_MODELS=false to allow paid models)"
        )


def ensure_served_free(served_model: str | None) -> None:
    if served_model and not served_model.endswith(":free"):
        raise PolicyViolationError(
            f"free-only guard: OpenRouter served non-free model {served_model!r}; answer rejected"
        )


def ensure_paid_allowed(provider: str, model: str, *, allowed: bool) -> None:
    if not allowed:
        raise PolicyViolationError(
            f"paid-model guard: the {provider} provider calls a metered endpoint (model {model!r}); "
            "set TALLY_ALLOW_PAID_MODELS=true to opt in"
        )
