"""The semantic layer: table and column descriptions, business rules, metrics, synonyms, join paths, ambiguous terms and
semantic views, loaded from YAML and validated (unknown keys fail at load time)."""

from __future__ import annotations

import re
from functools import cached_property
from pathlib import Path
from typing import Any

import yaml
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class ColumnMeta(_Strict):
    description: str = ""
    pii: bool = False
    sample: bool = True
    """Show distinct sample values of this column to the model (never done for PII)."""


class TableMeta(_Strict):
    description: str = ""
    synonyms: list[str] = Field(default_factory=list)
    allowed: bool = True
    columns: dict[str, ColumnMeta] = Field(default_factory=dict)

    @field_validator("columns", mode="before")
    @classmethod
    def _column_shorthand(cls, value: Any) -> Any:
        if isinstance(value, dict):
            return {k: {"description": v} if isinstance(v, str) else v for k, v in value.items()}
        return value


class SemanticView(_Strict):
    description: str
    synonyms: list[str] = Field(default_factory=list)
    columns: dict[str, str] = Field(default_factory=dict)
    sql: str


class Metric(_Strict):
    description: str
    synonyms: list[str] = Field(default_factory=list)
    view: str | None = None
    sql: str


class AmbiguityOption(_Strict):
    value: str
    label: str
    clarifies: str
    """Text appended to the question when this option is chosen, e.g. "net revenue after refunds"."""


class Ambiguity(_Strict):
    id: str
    description: str
    triggers: list[str]
    resolved_by: list[str] = Field(default_factory=list)
    question: str
    options: list[AmbiguityOption]
    default: str

    @model_validator(mode="after")
    def _check(self) -> Ambiguity:
        for pattern in [*self.triggers, *self.resolved_by]:
            try:
                re.compile(pattern)
            except re.error as exc:
                raise ValueError(f"ambiguity {self.id}: invalid pattern {pattern!r}: {exc}") from exc
        if self.default not in {o.value for o in self.options}:
            raise ValueError(f"ambiguity {self.id}: default {self.default!r} is not one of its options")
        return self

    def triggered_by(self, text: str) -> bool:
        lowered = text.lower()
        return any(re.search(p, lowered) for p in self.triggers)

    def resolved_in(self, text: str) -> bool:
        lowered = text.lower()
        return any(re.search(p, lowered) for p in self.resolved_by)

    def option(self, value: str) -> AmbiguityOption | None:
        return next((o for o in self.options if o.value == value), None)


class Reporting(_Strict):
    timezone: str = "UTC"
    currency: str = "USD"
    fiscal_year_start_month: int = 1
    first_data_date: str | None = None
    last_data_date: str | None = None


class FunctionPolicy(_Strict):
    allow: list[str] = Field(default_factory=list)


class SemanticLayer(_Strict):
    version: int = 1
    company: str = ""
    description: str = ""
    dialect: str = "postgres"
    reporting: Reporting = Reporting()
    rules: list[str] = Field(default_factory=list)
    synonyms: dict[str, list[str]] = Field(default_factory=dict)
    ambiguities: list[Ambiguity] = Field(default_factory=list)
    semantic_views: dict[str, SemanticView] = Field(default_factory=dict)
    metrics: dict[str, Metric] = Field(default_factory=dict)
    tables: dict[str, TableMeta] = Field(default_factory=dict)
    joins: list[tuple[str, str]] = Field(default_factory=list)
    functions: FunctionPolicy = FunctionPolicy()

    @model_validator(mode="after")
    def _check_references(self) -> SemanticLayer:
        for name, metric in self.metrics.items():
            if metric.view and metric.view not in self.semantic_views:
                raise ValueError(f"metric {name}: unknown semantic view {metric.view!r}")
        for left, right in self.joins:
            for side in (left, right):
                if side.count(".") != 1:
                    raise ValueError(f"join {left} = {right}: expected table.column")
        overlap = set(self.semantic_views) & set(self.tables)
        if overlap:
            raise ValueError(f"semantic views shadow tables: {sorted(overlap)}")
        return self

    @cached_property
    def pii_columns(self) -> dict[str, set[str]]:
        return {
            table: {name for name, col in meta.columns.items() if col.pii}
            for table, meta in self.tables.items()
            if any(col.pii for col in meta.columns.values())
        }

    def ambiguity(self, ambiguity_id: str) -> Ambiguity | None:
        return next((a for a in self.ambiguities if a.id == ambiguity_id), None)

    def expand_synonyms(self, text: str) -> list[str]:
        """Canonical terms whose synonyms (or the term itself) appear in the text."""
        lowered = f" {text.lower()} "
        found: list[str] = []
        for canonical, words in self.synonyms.items():
            for word in [canonical, *words]:
                stem = re.escape(word.lower().removesuffix("s"))
                if re.search(rf"(?<![a-z0-9]){stem}s?(?![a-z0-9])", lowered):
                    found.append(canonical)
                    break
        return found


EMPTY_LAYER = SemanticLayer()


def load_semantic_layer(path: Path) -> SemanticLayer:
    data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    return SemanticLayer.model_validate(data)
