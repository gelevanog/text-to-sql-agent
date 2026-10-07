"""Schema understanding: the generator, introspection, the semantic layer (loading, synonyms, metrics, ambiguity
patterns) and schema retrieval."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
import yaml
from pydantic import ValidationError

from tally.demo.generator import INJECTION_PRODUCT, generate
from tally.demo.seed import DEMO_PII_COLUMNS
from tally.schema.catalog import Catalog
from tally.schema.retrieval import SchemaRetriever, tokenize
from tally.schema.semantic import SemanticLayer, load_semantic_layer
from tally.services import Services

ROOT = Path(__file__).resolve().parents[1]
LAYER = load_semantic_layer(ROOT / "configs/semantic_layer.yaml")
CATALOG = Catalog.from_json(json.loads((ROOT / "tests/fixtures/catalog.json").read_text()), LAYER)


# ---- generator ------------------------------------------------------------------------------------------------
def test_generator_is_deterministic_and_sized() -> None:
    a, b = generate(42), generate(42)
    assert a.counts() == b.counts()
    assert a.rows["orders"][:50] == b.rows["orders"][:50]
    assert 150_000 <= sum(a.counts().values()) <= 300_000
    assert generate(7).rows["orders"][:20] != a.rows["orders"][:20]


def test_generator_plants_the_traps() -> None:
    tables = generate(42)
    customers = tables.rows["customers"]
    cols = tables.columns["customers"]
    assert any(row[cols.index("is_test_account")] for row in customers)
    assert any(row[cols.index("deleted_at")] is not None for row in customers)
    orders = tables.rows["orders"]
    ocols = tables.columns["orders"]
    assert any(row[ocols.index("deleted_at")] is not None for row in orders)
    assert {row[ocols.index("currency_code")] for row in orders} >= {"USD", "EUR", "JPY", "GBP", "BRL"}
    assert any(row[2] == INJECTION_PRODUCT for row in tables.rows["products"])
    assert any("AI assistant" in str(row[-1]) for row in tables.rows["support_tickets"])


def test_semantic_layer_pii_matches_the_demo_grants() -> None:
    assert {t: sorted(c) for t, c in LAYER.pii_columns.items()} == {t: sorted(c) for t, c in DEMO_PII_COLUMNS.items()}


# ---- semantic layer -------------------------------------------------------------------------------------------
def test_semantic_layer_loads_and_cross_references() -> None:
    assert set(LAYER.semantic_views) == {
        "order_revenue",
        "order_line_revenue",
        "customer_profile",
        "subscription_revenue",
    }
    assert LAYER.metrics["net_revenue"].view == "order_revenue"
    assert LAYER.reporting.fiscal_year_start_month == 2
    assert {a.id for a in LAYER.ambiguities} == {
        "revenue_basis",
        "period_basis",
        "active_customer",
        "top_products_basis",
    }


@pytest.mark.parametrize(
    "broken",
    [
        {"metrics": {"m": {"description": "x", "sql": "SUM(x)", "view": "no_such_view"}}},
        {"tables": {"orders": {"colums": {}}}},  # typo: unknown keys fail
        {
            "ambiguities": [
                {
                    "id": "a",
                    "description": "d",
                    "triggers": ["(unclosed"],
                    "question": "q",
                    "options": [{"value": "x", "label": "X", "clarifies": "x"}],
                    "default": "x",
                }
            ]
        },
        {
            "ambiguities": [
                {
                    "id": "a",
                    "description": "d",
                    "triggers": ["x"],
                    "question": "q",
                    "options": [{"value": "x", "label": "X", "clarifies": "x"}],
                    "default": "y",
                }
            ]
        },
        {"joins": [["orders", "customers.id"]]},
        {"semantic_views": {"orders": {"description": "d", "sql": "SELECT 1"}}, "tables": {"orders": {}}},
    ],
)
def test_semantic_layer_rejects_mistakes(broken: dict[str, object]) -> None:
    with pytest.raises((ValidationError, ValueError)):
        SemanticLayer.model_validate(broken)


def test_synonyms_expand_business_words() -> None:
    assert set(LAYER.expand_synonyms("What were sales by market for our clients?")) >= {
        "revenue",
        "regions",
        "customers",
    }
    assert "refunds" in LAYER.expand_synonyms("how many returns")
    assert LAYER.expand_synonyms("nothing relevant here") == []


@pytest.mark.parametrize(
    ("question", "pending"),
    [
        ("What was revenue last quarter?", {"revenue_basis", "period_basis"}),
        ("What was net revenue last quarter?", {"period_basis"}),
        ("What was net revenue last calendar quarter?", set()),
        ("Gross revenue in FY2026", set()),
        ("How many active customers?", {"active_customer"}),
        ("How many customers placed an order in the last 90 days?", set()),
        ("What are our top products?", {"top_products_basis"}),
        ("Top 5 products by units sold", set()),
        ("Subscription revenue in 2025", set()),
        ("How many orders did we get in 2025?", set()),
    ],
)
def test_ambiguity_patterns(question: str, pending: set[str]) -> None:
    found = {a.id for a in LAYER.ambiguities if a.triggered_by(question) and not a.resolved_in(question)}
    assert found == pending


def test_metric_definitions_live_in_the_layer_file() -> None:
    raw = yaml.safe_load((ROOT / "configs/semantic_layer.yaml").read_text())
    assert "refund" in raw["metrics"]["net_revenue"]["description"].lower()
    assert "is_test_account" in raw["semantic_views"]["order_revenue"]["sql"]


# ---- retrieval ------------------------------------------------------------------------------------------------
@pytest.fixture(scope="module")
def retriever() -> SchemaRetriever:
    return SchemaRetriever(CATALOG)


def test_tokenizer_stems_and_drops_stop_words() -> None:
    assert tokenize("What were the refunds for products?") == ["refund", "product"]


@pytest.mark.parametrize(
    ("question", "expected_view", "expected_tables"),
    [
        ("What was net revenue by region last calendar quarter?", "order_revenue", set()),
        ("Which 10 products had the most units sold?", "order_line_revenue", {"products"}),
        ("How many new customers did we get each month?", "customer_profile", set()),
        ("Subscription revenue by plan in 2025", "subscription_revenue", set()),
        ("What net revenue was attributed to each campaign?", "order_revenue", {"campaigns"}),
    ],
)
def test_retrieval_finds_the_right_views(
    retriever: SchemaRetriever, question: str, expected_view: str, expected_tables: set[str]
) -> None:
    context = retriever.retrieve(question)
    assert expected_view in context.views
    assert expected_tables <= set(context.tables)
    assert len(context.tables) < len(CATALOG.tables)  # never the whole schema


def test_retrieval_uses_synonyms_and_sample_values(retriever: SchemaRetriever) -> None:
    assert "support_tickets" in retriever.retrieve("How many complaints about delivery?").tables
    assert "refunds" in retriever.retrieve("damaged_in_transit reimbursements by country").tables


def test_retrieval_adds_join_paths_between_selected_tables(retriever: SchemaRetriever) -> None:
    path = retriever._path("refunds", "categories")
    assert path[0] == "refunds" and path[-1] == "categories" and "products" in path
    context = retriever.retrieve("refund reasons for product categories", extra_tables=["refunds", "categories"])
    assert {"refunds", "categories"} <= set(context.tables)
    assert (
        any(a.startswith("products.") or b.startswith("products.") for a, b in context.joins)
        or "products" in context.tables
    )


def test_rendered_context_hides_personal_data_but_names_it(retriever: SchemaRetriever) -> None:
    text = retriever.retrieve("customers by city").text
    assert "customers (" in text
    assert "  email" not in text and "  phone" not in text
    assert "Business rules" in text and "usd_per_unit" in text


def test_full_schema_mode_and_the_no_semantic_layer_ablation() -> None:
    full = SchemaRetriever(CATALOG).retrieve("anything", full=True)
    assert set(full.tables) == set(CATALOG.tables) and full.mode == "full"
    bare = CATALOG.without_semantic_layer()
    assert bare.views == {} and all(not c.description for t in bare.tables.values() for c in t.columns)
    assert bare.pii == CATALOG.pii
    text = SchemaRetriever(bare).retrieve("net revenue by region").text
    assert "Semantic views" not in text and "Business rules" not in text


@pytest.mark.db
def test_introspection_reads_grants_keys_counts_and_samples(services: Services) -> None:
    catalog = services.catalog
    customers = catalog.tables["customers"]
    assert {c.name for c in customers.columns if c.pii} == {"email", "phone", "street_address"}
    assert all(not c.samples for c in customers.columns if c.pii)
    orders = catalog.tables["orders"]
    assert orders.row_count > 30_000
    assert orders.column("customer_id").references == "customers.id"  # type: ignore[union-attr]
    assert orders.column("id").primary_key  # type: ignore[union-attr]
    assert "cancelled" in orders.column("status").samples  # type: ignore[union-attr]
    view = catalog.views["order_revenue"]
    assert view.column("net_revenue_usd").type == "numeric"  # type: ignore[union-attr]
    assert not any(INJECTION_PRODUCT in s for t in catalog.tables.values() for c in t.columns for s in c.samples)
