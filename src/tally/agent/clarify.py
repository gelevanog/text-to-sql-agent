"""Clarification policy: ask instead of guessing when a question uses a term the semantic layer marks as ambiguous.

The check is deterministic and runs before any model call: an ambiguity is pending when one of its trigger patterns
matches the question, none of its resolving patterns matches the question or an earlier question of the conversation,
and the user has not already picked an option. With TALLY_CLARIFY_POLICY=assume, the semantic layer's default is
used instead and stated as an assumption. The model can also ask on its own (action "clarify") for ambiguity the
layer does not list.
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Mapping
from dataclasses import dataclass

from tally.schema.semantic import Ambiguity, AmbiguityOption, SemanticLayer


@dataclass(frozen=True)
class PendingClarification:
    ambiguity: Ambiguity

    def to_dict(self) -> dict[str, object]:
        return {
            "id": self.ambiguity.id,
            "question": self.ambiguity.question,
            "options": [{"value": o.value, "label": o.label} for o in self.ambiguity.options],
            "source": "semantic_layer",
        }


def pending_ambiguities(
    question: str,
    layer: SemanticLayer,
    *,
    history: Iterable[str] = (),
    resolved: Mapping[str, str] | None = None,
) -> list[Ambiguity]:
    resolved = resolved or {}
    earlier = list(history)
    pending: list[Ambiguity] = []
    for ambiguity in layer.ambiguities:
        if ambiguity.id in resolved:
            continue
        if not ambiguity.triggered_by(question) or ambiguity.resolved_in(question):
            continue
        if any(ambiguity.resolved_in(text) for text in earlier):
            continue
        pending.append(ambiguity)
    return pending


def match_option(reply: str, ambiguity: Ambiguity) -> AmbiguityOption | None:
    """Map a free-text reply ("net", "fiscal please", "by units") to one of the options, if exactly one fits."""
    lowered = reply.lower().strip()
    vocab = {
        option.value: {option.value.lower(), *re.findall(r"[a-z0-9]+", option.label.lower())} - _FILLER
        for option in ambiguity.options
    }
    hits: list[AmbiguityOption] = []
    for option in ambiguity.options:
        others = set().union(*(words for value, words in vocab.items() if value != option.value))
        distinctive = vocab[option.value] - others
        if any(re.search(rf"\b{re.escape(w)}\b", lowered) for w in distinctive):
            hits.append(option)
    return hits[0] if len(hits) == 1 else None


_FILLER = frozenset({"by", "the", "in", "of", "a", "an", "has", "starts", "feb", "fy"})


def clarified_texts(layer: SemanticLayer, resolved: Mapping[str, str]) -> list[str]:
    texts: list[str] = []
    for ambiguity_id, value in resolved.items():
        ambiguity = layer.ambiguity(ambiguity_id)
        option = ambiguity.option(value) if ambiguity else None
        if ambiguity and option:
            texts.append(f"{ambiguity.description} -> {option.clarifies}")
    return texts


def assumed_defaults(pending: Iterable[Ambiguity]) -> tuple[dict[str, str], list[str]]:
    chosen: dict[str, str] = {}
    notes: list[str] = []
    for ambiguity in pending:
        option = ambiguity.option(ambiguity.default)
        if option:
            chosen[ambiguity.id] = option.value
            notes.append(f"Assumed {option.clarifies} ({ambiguity.description.rstrip('.')}).")
    return chosen, notes
