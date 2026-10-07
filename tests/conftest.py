"""Shared fixtures. Database tests need PostgreSQL at TEST_DATABASE_URL (a superuser login, e.g. the CI service
container); they are skipped without it unless REQUIRE_TEST_DB=1. No test needs an API key."""

from __future__ import annotations

import json
import os
from collections.abc import Callable, Iterator, Sequence
from pathlib import Path
from typing import Any

import psycopg
import pytest

from tally.agent.store import InMemoryStore
from tally.config import Settings
from tally.llm.base import Completion, Message
from tally.services import Services, build_services

ROOT = Path(__file__).resolve().parents[1]
TEST_DATABASE_URL = os.environ.get("TEST_DATABASE_URL", "")


def pytest_collection_modifyitems(config: pytest.Config, items: list[pytest.Item]) -> None:
    if TEST_DATABASE_URL:
        return
    if os.environ.get("REQUIRE_TEST_DB") == "1":
        raise pytest.UsageError("REQUIRE_TEST_DB=1 but TEST_DATABASE_URL is not set")
    skip = pytest.mark.skip(reason="needs PostgreSQL (TEST_DATABASE_URL)")
    for item in items:
        if "db" in item.keywords:
            item.add_marker(skip)


def make_settings(**overrides: Any) -> Settings:
    base: dict[str, Any] = {
        "database_url": TEST_DATABASE_URL or "postgresql://tally:tally@localhost:5432/tally_test",
        "llm_provider": "fake",
        "llm_ledger": None,
        "llm_cache": False,
        "semantic_layer_file": ROOT / "configs" / "semantic_layer.yaml",
        "benchmark_file": ROOT / "data" / "benchmark" / "questions.yaml",
        "results_dir": ROOT / "results",
        "seed_demo": True,
        "log_level": "WARNING",
    }
    base.update(overrides)
    return Settings(**base)


@pytest.fixture(scope="session")
def settings() -> Settings:
    return make_settings()


@pytest.fixture(scope="session")
def seeded(settings: Settings) -> Settings:
    """A freshly generated demo database (once per test session, ~10 s)."""
    from tally.demo.seed import seed

    with psycopg.connect(settings.database_url, autocommit=True) as conn:
        seed(
            conn,
            schema=settings.data_schema,
            reader_role=settings.reader_role,
            reader_password=settings.reader_password,
            scoped_role=settings.scoped_reader_role,
            app_schema=settings.app_schema,
        )
    return settings


@pytest.fixture(scope="session")
def services(seeded: Settings) -> Iterator[Services]:
    svc = build_services(seeded, seed_demo=False)
    yield svc
    svc.close()


@pytest.fixture
def owner(seeded: Settings) -> Iterator[psycopg.Connection]:
    with psycopg.connect(seeded.database_url, autocommit=True) as conn:
        yield conn


@pytest.fixture
def reader(seeded: Settings) -> Iterator[psycopg.Connection]:
    with psycopg.connect(seeded.resolved_reader_url, autocommit=True) as conn:
        yield conn


class ScriptedModel:
    """A test model that returns scripted replies in order (dicts are sent as JSON) and records every prompt."""

    def __init__(self, replies: Sequence[Any], *, label: str = "scripted/test") -> None:
        self.replies = list(replies)
        self.prompts: list[list[Message]] = []
        self._label = label

    @property
    def label(self) -> str:
        return self._label

    @property
    def is_local(self) -> bool:
        return True

    def complete(self, messages: Sequence[Message], *, max_tokens: int, temperature: float = 0.0) -> Completion:
        self.prompts.append(list(messages))
        if not self.replies:
            raise AssertionError("ScriptedModel ran out of replies")
        reply = self.replies.pop(0)
        if callable(reply):
            reply = reply(messages)
        text = reply if isinstance(reply, str) else json.dumps(reply)
        return Completion(text=text, model=self._label)


def sql_reply(sql: str, **extra: Any) -> dict[str, Any]:
    return {"action": "sql", "sql": sql, "plan": "test plan", "explanation": "test explanation", **extra}


@pytest.fixture
def agent_factory(services: Services) -> Callable[..., Any]:
    from tally.services import make_agent

    def build(replies: Sequence[Any], **overrides: Any) -> tuple[Any, ScriptedModel]:
        model = ScriptedModel(replies)
        return make_agent(services, llm=model, store=InMemoryStore(), **overrides), model

    return build
