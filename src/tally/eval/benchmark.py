"""The hand-written benchmark: questions about the demo company with reference ("gold") SQL and expected behaviour."""

from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path
from typing import Any, Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field, model_validator

from tally.llm.fake import playbook_key

Expect = Literal["answer", "clarify", "block"]


class FakeReply(BaseModel):
    model_config = ConfigDict(extra="forbid")
    action: Literal["sql", "clarify", "refuse"] = "sql"
    sql: str = ""
    refusal: str = ""
    question: str = ""


class BenchmarkItem(BaseModel):
    model_config = ConfigDict(extra="forbid")
    id: str
    category: str
    question: str
    expect: Expect = "answer"
    gold_sql: str = ""
    order_matters: bool = False
    follow_up_of: str | None = None
    clarification: dict[str, str] | None = None
    """Answers given to Tally's clarifying question before this item runs, e.g. {"revenue_basis": "net"}."""
    ambiguity: str | None = None
    """For expect=clarify: the semantic-layer ambiguity id expected (None = any clarification counts)."""
    safety: str | None = None
    """For expect=block: what makes it unsafe (write, pii, injection, cost, exfiltration, system)."""
    explanation: str = ""
    fake: FakeReply | None = None
    notes: str = ""
    subset: bool = False
    """In the stratified subset used for the ablations and the model comparison."""
    tags: list[str] = Field(default_factory=list)

    @model_validator(mode="after")
    def _check(self) -> BenchmarkItem:
        if self.expect == "answer" and not self.gold_sql.strip():
            raise ValueError(f"{self.id}: answerable items need gold_sql")
        return self


def load_benchmark(path: Path) -> list[BenchmarkItem]:
    data = yaml.safe_load(path.read_text(encoding="utf-8")) or []
    items = [BenchmarkItem.model_validate(raw) for raw in data]
    ids = [item.id for item in items]
    duplicates = {i for i in ids if ids.count(i) > 1}
    if duplicates:
        raise ValueError(f"duplicate benchmark ids: {sorted(duplicates)}")
    known = set(ids)
    for item in items:
        if item.follow_up_of and item.follow_up_of not in known:
            raise ValueError(f"{item.id}: follow_up_of {item.follow_up_of!r} is not a benchmark id")
    return items


def conversation_chain(item: BenchmarkItem, by_id: Mapping[str, BenchmarkItem]) -> list[BenchmarkItem]:
    chain = [item]
    while chain[0].follow_up_of:
        chain.insert(0, by_id[chain[0].follow_up_of])
    return chain


def build_playbook(items: list[BenchmarkItem]) -> dict[str, dict[str, Any]]:
    """What the offline fake model answers: the gold SQL (or the item's `fake` reply) per question, keyed by the
    question and, for follow-ups, by the previous question too."""
    by_id = {item.id: item for item in items}
    playbook: dict[str, dict[str, Any]] = {}
    for item in items:
        if item.fake is not None:
            reply: dict[str, Any] = {
                "action": item.fake.action,
                "plan": "offline demo model",
                "explanation": item.explanation,
            }
            if item.fake.action == "sql":
                reply["sql"] = item.fake.sql or item.gold_sql
            elif item.fake.action == "refuse":
                reply["refusal"] = item.fake.refusal or "This is not a read-only analytics question."
            else:
                reply["clarification"] = {"question": item.fake.question or "Could you clarify?", "options": []}
        elif item.gold_sql:
            reply = {
                "action": "sql",
                "sql": item.gold_sql.strip(),
                "plan": "offline demo model",
                "explanation": item.explanation or item.notes,
            }
        else:
            continue
        if "replays_original_question" in item.tags:
            # Answering a clarification re-runs the conversation's original question with the choices applied.
            playbook[playbook_key(conversation_chain(item, by_id)[0].question)] = reply
            continue
        previous = by_id[item.follow_up_of].question if item.follow_up_of else None
        playbook[playbook_key(item.question, previous)] = reply
        if previous is None:
            playbook.setdefault(playbook_key(item.question), reply)
    return playbook
