"""Optional cloud embeddings for schema retrieval (OpenRouter's /embeddings, e.g. liquid/lfm-2.5-embedding-350m:free).
Off by default: BM25 plus the semantic layer's synonyms is the default retriever. Vectors are cached on disk and every
request is recorded in the call ledger; the free-only guard applies."""

from __future__ import annotations

import hashlib
import json
import time
from pathlib import Path

import httpx

from tally.llm.base import LLMError
from tally.llm.budget import CallLedger, Throttle
from tally.llm.guards import ensure_free_models
from tally.llm.openai_compat import OPENROUTER_HEADERS, _json_or_error


class OpenRouterEmbedder:
    def __init__(
        self,
        *,
        model: str,
        api_key: str | None,
        base_url: str = "https://openrouter.ai/api/v1",
        cache_dir: Path | None = None,
        ledger: CallLedger | None = None,
        throttle: Throttle | None = None,
        require_free: bool = True,
        transport: httpx.BaseTransport | None = None,
    ) -> None:
        if not api_key:
            raise LLMError("OPENROUTER_API_KEY is not set (needed for TALLY_EMBEDDINGS=openrouter)")
        if require_free:
            ensure_free_models([model])
        self.model = model
        self._url = base_url.rstrip("/") + "/embeddings"
        self._headers = {"Authorization": f"Bearer {api_key}", **OPENROUTER_HEADERS}
        self.cache_dir = cache_dir
        self.ledger = ledger
        self.throttle = throttle
        self._transport = transport

    def _cache_path(self, text: str) -> Path | None:
        if self.cache_dir is None:
            return None
        key = hashlib.sha256(f"{self.model}\n{text}".encode()).hexdigest()
        return self.cache_dir / "embeddings" / key[:2] / f"{key}.json"

    def __call__(self, texts: list[str]) -> list[list[float]]:
        vectors: dict[int, list[float]] = {}
        missing: list[int] = []
        for i, text in enumerate(texts):
            path = self._cache_path(text)
            if path is not None and path.exists():
                vectors[i] = json.loads(path.read_text(encoding="utf-8"))
            else:
                missing.append(i)
        for start in range(0, len(missing), 64):
            batch = missing[start : start + 64]
            for i, vector in zip(batch, self._request([texts[i] for i in batch]), strict=True):
                vectors[i] = vector
                path = self._cache_path(texts[i])
                if path is not None:
                    path.parent.mkdir(parents=True, exist_ok=True)
                    path.write_text(json.dumps(vector), encoding="utf-8")
        return [vectors[i] for i in range(len(texts))]

    def _request(self, texts: list[str]) -> list[list[float]]:
        if self.ledger is not None:
            self.ledger.reserve()
        if self.throttle is not None:
            self.throttle.wait()
        started = time.monotonic()
        status, error = "ok", None
        try:
            with httpx.Client(timeout=60, transport=self._transport) as client:
                response = client.post(self._url, json={"model": self.model, "input": texts}, headers=self._headers)
            data = _json_or_error(response)
            items = sorted(data.get("data") or [], key=lambda d: d.get("index", 0))
            if len(items) != len(texts):
                raise LLMError(f"expected {len(texts)} embeddings, got {len(items)}")
            return [list(map(float, item["embedding"])) for item in items]
        except Exception as exc:
            status, error = "error", str(exc)[:300]
            raise
        finally:
            if self.ledger is not None:
                self.ledger.record(
                    tag="embeddings",
                    provider=f"openrouter/{self.model}",
                    requested_model=self.model,
                    served_model=self.model if status == "ok" else None,
                    status=status,
                    latency_s=round(time.monotonic() - started, 2),
                    inputs=len(texts),
                    error=error,
                )
