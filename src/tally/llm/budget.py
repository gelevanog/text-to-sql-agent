"""Budget-safe wrapper for real cloud APIs: disk cache, throttle, retries with backoff, hard call budget, ledger.

Every real request, retries included, is appended to a JSONL ledger, so "how many API calls did this cost" is read
from a file, not remembered. The ledger stores model ids, status, latency and token counts; never prompts.
The offline fake model is passed through untouched (no throttle, no ledger, no cache).
"""

from __future__ import annotations

import hashlib
import json
import random
import threading
import time
from collections.abc import Sequence
from datetime import UTC, datetime
from pathlib import Path

from tally.llm.base import (
    BudgetExceededError,
    ChatModel,
    Completion,
    LLMError,
    Message,
    PolicyViolationError,
    RetryableLLMError,
)
from tally.logging_config import get_logger

log = get_logger(__name__)


class DiskCache:
    def __init__(self, directory: Path) -> None:
        self.directory = directory

    @staticmethod
    def key(label: str, messages: Sequence[Message], max_tokens: int, temperature: float) -> str:
        material = json.dumps(
            {"model": label, "messages": list(messages), "max_tokens": max_tokens, "temperature": temperature},
            sort_keys=True,
            ensure_ascii=False,
        )
        return hashlib.sha256(material.encode("utf-8")).hexdigest()

    def _path(self, key: str) -> Path:
        return self.directory / key[:2] / f"{key}.json"

    def get(self, key: str) -> Completion | None:
        path = self._path(key)
        if not path.exists():
            return None
        data = json.loads(path.read_text(encoding="utf-8"))
        return Completion(
            text=data["text"],
            model=data["model"],
            input_tokens=int(data.get("input_tokens", 0)),
            output_tokens=int(data.get("output_tokens", 0)),
            cached=True,
        )

    def put(self, key: str, completion: Completion) -> None:
        path = self._path(key)
        path.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "text": completion.text,
            "model": completion.model,
            "input_tokens": completion.input_tokens,
            "output_tokens": completion.output_tokens,
        }
        path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")


class Throttle:
    def __init__(self, min_interval: float) -> None:
        self.min_interval = min_interval
        self._next = 0.0
        self._lock = threading.Lock()

    def wait(self) -> None:
        if self.min_interval <= 0:
            return
        with self._lock:
            now = time.monotonic()
            start = max(now, self._next)
            self._next = start + self.min_interval
        if start > now:
            time.sleep(start - now)


class CallLedger:
    def __init__(self, path: Path | None, max_calls: int) -> None:
        self.path = path
        self.max_calls = max_calls
        self.calls = 0
        self._lock = threading.Lock()
        if path is not None and path.exists():
            with path.open(encoding="utf-8") as handle:
                self.calls = sum(1 for line in handle if line.strip())

    def reserve(self) -> None:
        with self._lock:
            if self.calls >= self.max_calls:
                raise BudgetExceededError(f"call budget of {self.max_calls} real requests reached ({self.path})")
            self.calls += 1

    def record(self, **entry: object) -> None:
        if self.path is None:
            return
        self.path.parent.mkdir(parents=True, exist_ok=True)
        row = {"ts": datetime.now(UTC).isoformat(timespec="seconds"), **entry}
        with self._lock, self.path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(row, ensure_ascii=False) + "\n")


class BudgetedModel:
    def __init__(
        self,
        inner: ChatModel,
        *,
        ledger: CallLedger,
        cache: DiskCache | None,
        throttle: Throttle | None,
        max_retries: int = 4,
        retry_base_seconds: float = 5.0,
        tag: str = "",
    ) -> None:
        self.inner = inner
        self.ledger = ledger
        self.cache = cache
        self.throttle = throttle
        self.max_retries = max_retries
        self.retry_base_seconds = retry_base_seconds
        self.tag = tag
        self.last_served: str | None = None
        """The model that produced the most recent answer (OpenRouter may route to a fallback)."""

    @property
    def label(self) -> str:
        return self.inner.label

    @property
    def is_local(self) -> bool:
        return self.inner.is_local

    def with_tag(self, tag: str) -> BudgetedModel:
        return BudgetedModel(
            self.inner,
            ledger=self.ledger,
            cache=self.cache,
            throttle=self.throttle,
            max_retries=self.max_retries,
            retry_base_seconds=self.retry_base_seconds,
            tag=tag,
        )

    def complete(self, messages: Sequence[Message], *, max_tokens: int, temperature: float = 0.0) -> Completion:
        if self.inner.is_local:
            return self.inner.complete(messages, max_tokens=max_tokens, temperature=temperature)
        key = DiskCache.key(self.inner.label, messages, max_tokens, temperature)
        if self.cache is not None and (hit := self.cache.get(key)) is not None:
            self.last_served = hit.model
            return hit
        last: LLMError | None = None
        for attempt in range(self.max_retries + 1):
            self.ledger.reserve()
            if self.throttle is not None:
                self.throttle.wait()
            started = time.monotonic()
            try:
                completion = self.inner.complete(messages, max_tokens=max_tokens, temperature=temperature)
            except RetryableLLMError as exc:
                last = exc
                self._record("retryable_error", started, error=str(exc))
            except (PolicyViolationError, LLMError) as exc:
                self._record("error", started, error=str(exc))
                raise
            else:
                self._record("ok", started, completion=completion)
                self.last_served = completion.model
                if self.cache is not None:
                    self.cache.put(key, completion)
                return completion
            if attempt < self.max_retries:
                delay = self._backoff(attempt, last)
                log.warning(
                    "llm.retry", tag=self.tag, attempt=attempt + 1, delay=round(delay, 1), error=str(last)[:160]
                )
                time.sleep(delay)
        raise last or LLMError("request failed")

    def _backoff(self, attempt: int, error: LLMError | None) -> float:
        retry_after = error.retry_after if isinstance(error, RetryableLLMError) else None
        if retry_after:
            return min(retry_after, 60.0)
        return float(min(self.retry_base_seconds * 2.0**attempt, 60.0) * (0.75 + random.random() / 2))

    def _record(
        self, status: str, started: float, *, error: str | None = None, completion: Completion | None = None
    ) -> None:
        self.ledger.record(
            tag=self.tag,
            provider=self.inner.label,
            requested_model=self.inner.label.split("/", 1)[-1],
            fallback_models=list(getattr(self.inner, "fallback_models", []) or []) or None,
            served_model=completion.model if completion else None,
            status=status,
            latency_s=round(time.monotonic() - started, 2),
            input_tokens=completion.input_tokens if completion else 0,
            output_tokens=completion.output_tokens if completion else 0,
            error=error[:300] if error else None,
        )
