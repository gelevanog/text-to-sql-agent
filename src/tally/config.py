"""Runtime settings from environment variables (and an optional .env file). See .env.example."""

from __future__ import annotations

import datetime as dt
from functools import lru_cache
from pathlib import Path
from typing import Annotated, Literal
from urllib.parse import quote, urlsplit, urlunsplit

from pydantic import AliasChoices, Field, field_validator
from pydantic_settings import BaseSettings, NoDecode, SettingsConfigDict

LLMProviderKind = Literal["fake", "openrouter", "qwen", "openai", "anthropic", "openai_compatible"]
ClarifyPolicy = Literal["ask", "assume"]
RetrievalMode = Literal["retrieval", "full"]
EmbeddingProviderKind = Literal["off", "openrouter"]

# Free OpenRouter models chosen from the smoke test before the real run (results/smoke.json).
DEFAULT_FREE_MODEL = "nvidia/nemotron-3-super-120b-a12b:free"
DEFAULT_FREE_FALLBACKS = ["nvidia/nemotron-3-ultra-550b-a55b:free"]
# Qwen Cloud (Alibaba Model Studio / DashScope), OpenAI-compatible. The Token Plan's international endpoint is the
# default; the pay-as-you-go endpoints are https://dashscope-intl.aliyuncs.com/compatible-mode/v1 (international) and
# https://dashscope.aliyuncs.com/compatible-mode/v1 (mainland China).
DEFAULT_QWEN_BASE_URL = "https://token-plan.ap-southeast-1.maas.aliyuncs.com/compatible-mode/v1"
DEFAULT_QWEN_MODEL = "qwen3-coder-next"
DEFAULT_MODELS: dict[str, str] = {
    "fake": "fake-demo",
    "openrouter": DEFAULT_FREE_MODEL,
    "qwen": DEFAULT_QWEN_MODEL,
    "openai": "gpt-5-mini",
    "anthropic": "claude-sonnet-5",
    "openai_compatible": "",
}
CLOUD_PROVIDERS = frozenset({"openrouter", "qwen", "openai", "anthropic"})


def _split(value: object) -> object:
    if isinstance(value, str):
        return [item.strip() for item in value.split(",") if item.strip()]
    return value


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_prefix="TALLY_", extra="ignore")

    # ---- database
    database_url: str = "postgresql://tally:tally@localhost:5432/tally"
    """Owner connection: creates the demo schema, the reader role and Tally's own tables (audit, conversations)."""
    reader_database_url: str = ""
    """Read-only login every generated query runs as. Empty = database_url with the reader role's credentials."""
    reader_role: str = "tally_reader"
    reader_password: str = "tally_reader"
    data_schema: str = "public"
    """Schema with the business tables the agent may query."""
    app_schema: str = "tally"
    """Schema for Tally's own tables; the reader role has no access to it."""
    seed_demo: bool = True
    """On API startup, generate the demo company database when the data schema is empty."""
    demo_seed: int = 42
    demo_scale: float = 1.0

    # ---- execution guard
    statement_timeout_ms: int = 5000
    max_rows: int = 1000
    """Row cap: LIMIT is enforced in the SQL and rows are fetched up to this many."""
    max_plan_cost: float = 2_000_000.0
    """EXPLAIN total cost above which a query is refused before it runs."""
    max_plan_rows: float = 50_000_000.0
    region_scope: str = "*"
    """Row-level security scope: '*' = all regions, or a region code (e.g. EU) to restrict every query to it."""

    # ---- agent
    clarify_policy: ClarifyPolicy = "ask"
    """ask = ask a clarifying question for ambiguous terms; assume = use the semantic layer's default and say so."""
    max_corrections: int = 2
    """Retries after a SQL error, a validation failure or a suspicious result."""
    retrieval_mode: RetrievalMode = "retrieval"
    semantic_layer: bool = True
    semantic_layer_file: Path = Path("configs/semantic_layer.yaml")
    retrieval_top_tables: int = 6
    retrieval_top_metrics: int = 3
    history_turns: int = 3
    answer_retries: int = 1
    """Re-asks for an answer whose numbers are not all in the result, before falling back to a template answer."""
    today: dt.date | None = dt.date(2026, 10, 1)
    """The date the agent treats as today (fixed for the demo so relative dates are reproducible). Empty = real date."""

    # ---- schema retrieval embeddings (optional, cloud)
    embeddings: EmbeddingProviderKind = "off"
    embedding_model: str = "liquid/lfm-2.5-embedding-350m:free"
    embedding_weight: float = 0.5

    # ---- LLM
    llm_provider: LLMProviderKind = "fake"
    llm_model: str = ""
    """Empty = the provider's default (DEFAULT_MODELS)."""
    llm_fallback_models: Annotated[list[str], NoDecode] = Field(default_factory=list)
    llm_base_url: str = ""
    """For openai_compatible (vLLM, LM Studio, any OpenAI-compatible gateway)."""
    llm_api_key: str | None = None
    llm_timeout_seconds: float = 120.0
    llm_max_tokens: int = 4000
    llm_temperature: float = 0.0
    llm_reasoning_effort: str = "low"
    """Sent to OpenRouter as reasoning.effort for reasoning models (empty = not sent)."""
    require_free_models: bool = True
    """Refuse any OpenRouter model id that does not end in ":free" (requests and the model that answered)."""

    openrouter_api_key: str | None = Field(default=None, validation_alias=AliasChoices("OPENROUTER_API_KEY"))
    openrouter_base_url: str = Field(
        default="https://openrouter.ai/api/v1", validation_alias=AliasChoices("OPENROUTER_BASE_URL")
    )
    qwen_api_key: str | None = Field(
        default=None, validation_alias=AliasChoices("QWEN_API_KEY", "DASHSCOPE_API_KEY", "TALLY_QWEN_API_KEY")
    )
    qwen_base_url: str = DEFAULT_QWEN_BASE_URL
    qwen_enable_thinking: bool | None = None
    """Sent as enable_thinking to Qwen3 hybrid-thinking models when set (false keeps latency and tokens down)."""
    openai_api_key: str | None = Field(default=None, validation_alias=AliasChoices("OPENAI_API_KEY"))
    openai_base_url: str = Field(default="https://api.openai.com/v1", validation_alias=AliasChoices("OPENAI_BASE_URL"))
    anthropic_api_key: str | None = Field(default=None, validation_alias=AliasChoices("ANTHROPIC_API_KEY"))
    anthropic_effort: str = "low"

    # ---- budget for real cloud calls
    llm_cache: bool = False
    """Disk cache of model responses keyed by the full request (the evaluation always turns it on)."""
    llm_cache_dir: Path = Path(".cache/llm")
    llm_ledger: Path | None = Path("results/calls.jsonl")
    llm_max_calls: int = 500
    llm_min_seconds_between_requests: float = 3.0
    llm_max_retries: int = 4

    # ---- API
    cors_origins: Annotated[list[str], NoDecode] = Field(default_factory=lambda: ["http://localhost:3000"])
    results_dir: Path = Path("results")
    benchmark_file: Path = Path("data/benchmark/questions.yaml")
    log_level: str = Field(default="INFO", validation_alias=AliasChoices("LOG_LEVEL", "TALLY_LOG_LEVEL"))
    log_format: Literal["console", "json"] = Field(
        default="console", validation_alias=AliasChoices("LOG_FORMAT", "TALLY_LOG_FORMAT")
    )

    @field_validator("llm_fallback_models", "cors_origins", mode="before")
    @classmethod
    def _split_lists(cls, value: object) -> object:
        return _split(value)

    @field_validator("today", mode="before")
    @classmethod
    def _empty_today(cls, value: object) -> object:
        return None if value in ("", "none", "None") else value

    @property
    def resolved_llm_model(self) -> str:
        return self.llm_model or DEFAULT_MODELS[self.llm_provider]

    @property
    def effective_today(self) -> dt.date:
        return self.today or dt.datetime.now(dt.UTC).date()

    @property
    def resolved_reader_url(self) -> str:
        """The reader role's connection string (the owner URL with the reader's user and password)."""
        if self.reader_database_url:
            return self.reader_database_url
        parts = urlsplit(self.database_url)
        host = parts.hostname or "localhost"
        netloc = f"{quote(self.reader_role)}:{quote(self.reader_password)}@{host}"
        if parts.port:
            netloc += f":{parts.port}"
        return urlunsplit((parts.scheme, netloc, parts.path, parts.query, parts.fragment))


@lru_cache
def get_settings() -> Settings:
    return Settings()
