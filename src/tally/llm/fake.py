"""The deterministic offline model: it maps the demo and benchmark questions to their reference SQL, and writes
answers from the result with a template. It makes CI, the tests and the zero-key Docker demo work without any API
key, and it plays a *gullible* model for the adversarial benchmark items (it writes the DELETE or the PII query a
careless model might), so the validator and the database guards are exercised offline too."""

from __future__ import annotations

import json
import re
from collections.abc import Mapping, Sequence
from typing import Any

from tally.agent.answer_check import template_answer
from tally.agent.prompts import ANSWER_SYSTEM, PREVIOUS_HEADING, QUESTION_HEADING, RESULT_HEADING
from tally.llm.base import Completion, Message

UNKNOWN_REPLY = {
    "action": "refuse",
    "plan": "",
    "refusal": (
        "The offline demo model only knows the demo and benchmark questions. Pick a saved question, or configure a "
        "real model (TALLY_LLM_PROVIDER=openrouter with a free model)."
    ),
}


def normalize(text: str) -> str:
    return re.sub(r"\s+", " ", re.sub(r"[?.!\s]+$", "", text.strip().lower()))


def playbook_key(question: str, previous: str | None = None) -> str:
    return f"{normalize(previous)} || {normalize(question)}" if previous else normalize(question)


class FakeModel:
    def __init__(self, playbook: Mapping[str, Mapping[str, Any]]) -> None:
        self.playbook = dict(playbook)
        self.calls = 0

    @property
    def label(self) -> str:
        return "fake/demo"

    @property
    def is_local(self) -> bool:
        return True

    def complete(self, messages: Sequence[Message], *, max_tokens: int, temperature: float = 0.0) -> Completion:
        self.calls += 1
        if messages and messages[0]["content"] == ANSWER_SYSTEM:
            return Completion(text=self._answer(messages), model="fake/demo")
        prompt = next(m["content"] for m in messages if m["role"] == "user")
        question = ""
        previous: str | None = None
        for line in prompt.splitlines():
            if line.startswith(QUESTION_HEADING):
                question = line[len(QUESTION_HEADING) :].strip()
            elif PREVIOUS_HEADING in line:
                previous = line.split(PREVIOUS_HEADING, 1)[1].strip()
        reply = self.playbook.get(playbook_key(question, previous)) or self.playbook.get(playbook_key(question))
        return Completion(text=json.dumps(dict(reply) if reply else UNKNOWN_REPLY), model="fake/demo")

    @staticmethod
    def _answer(messages: Sequence[Message]) -> str:
        prompt = messages[1]["content"]
        payload: dict[str, Any] = {}
        for line in prompt.splitlines():
            if line.startswith(RESULT_HEADING):
                payload = json.loads(line[line.index("{") :])
        return template_answer(
            payload.get("columns", []),
            payload.get("rows", []),
            int(payload.get("row_count", 0)),
            bool(payload.get("truncated")),
        )
