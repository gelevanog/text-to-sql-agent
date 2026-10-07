"""Pure logic: the answer-number check, the chart recommender and the evaluation's result comparator."""

from __future__ import annotations

import datetime as dt
from decimal import Decimal

import pytest

from tally.agent.answer_check import check_answer, extract_numbers, template_answer
from tally.agent.chart import recommend_chart
from tally.eval.compare import compare_results

ROWS = [["EU", 104022.05, 123286.92, -0.1563], ["NA", 172289.8, 184829.41, -0.0678]]
COLUMNS = ["region_code", "net_revenue_q3_2026", "net_revenue_q2_2026", "pct_change"]


# ---- answer check -----------------------------------------------------------------------------------------------
@pytest.mark.parametrize(
    "answer",
    [
        "EU net revenue was $104,022.05 in Q3 2026, down from $123,286.92.",
        "EU fell to about $104K from $123.3K, a drop of 15.6%.",
        "EU dropped 16% while NA declined 6.8% to $172.3K.",
        "North America brought in $0.17M, Europe 0.1M.",
        "Both regions (2 rows) shrank; EU by 15.63 percent.",
    ],
)
def test_answers_quoting_the_result_pass(answer: str) -> None:
    check = check_answer(answer, ROWS, question="net revenue last calendar quarter vs the one before", columns=COLUMNS)
    assert check.ok, check.unsupported


@pytest.mark.parametrize(
    ("answer", "bad"),
    [
        ("EU net revenue was $110,500 in Q3.", "$110,500"),
        ("Together the regions made $276,311.85.", "$276,311.85"),  # a total the result does not contain
        ("EU is 19,264.87 below Q2.", "19,264.87"),  # a difference the result does not contain
        ("EU fell 25%.", "25%"),
    ],
)
def test_numbers_not_in_the_result_are_flagged(answer: str, bad: str) -> None:
    check = check_answer(answer, ROWS, columns=COLUMNS)
    assert not check.ok
    assert bad in check.unsupported


def test_small_counts_dates_and_question_numbers_are_allowed() -> None:
    rows = [["2026-07-01", 42]]
    assert check_answer("In July 2026 (the 3rd quarter) we had 42 orders across 5 regions.", rows).ok
    assert check_answer("The top 25 products...", [[1.5]], question="Show the top 25 products").ok


def test_number_extraction_handles_suffixes_and_signs() -> None:
    found = {n.text: n.value for n in extract_numbers("Revenue was $1.23M, costs 12.5K, change -4.2%, 2,500 units")}
    assert found["$1.23M"] == pytest.approx(1_230_000)
    assert found["12.5K"] == pytest.approx(12_500)
    assert found["-4.2%"] == pytest.approx(-4.2)
    assert found["2,500"] == 2500


def test_template_answer_uses_only_result_values() -> None:
    answer = template_answer(COLUMNS, ROWS, 2, False)
    assert check_answer(answer, ROWS, columns=COLUMNS).ok
    assert template_answer(["n"], [], 0, False) == "No rows matched the question."


# ---- chart recommender --------------------------------------------------------------------------------------------
def test_single_number_is_a_kpi() -> None:
    assert recommend_chart(["net_revenue_usd"], ["numeric"], [[1645578.17]]).type == "kpi"


def test_time_series_is_a_line() -> None:
    rows = [
        ["2026-01-01T00:00:00+00:00", 10.0],
        ["2026-02-01T00:00:00+00:00", 12.0],
        ["2026-03-01T00:00:00+00:00", 9.0],
    ]
    spec = recommend_chart(["month", "net_revenue_usd"], ["timestamptz", "numeric"], rows)
    assert spec.type == "line" and spec.x == "month" and spec.y == ["net_revenue_usd"]


def test_time_series_by_category_pivots_into_series() -> None:
    rows = [["2026-01-01", "EU", 1.0], ["2026-01-01", "NA", 2.0], ["2026-02-01", "EU", 3.0], ["2026-02-01", "NA", 4.0]]
    spec = recommend_chart(["month", "region_code", "net"], ["date", "text", "numeric"], rows)
    assert spec.type == "line" and spec.series == ["EU", "NA"]
    assert spec.data[0] == {"month": "2026-01-01", "EU": 1.0, "NA": 2.0}


def test_category_and_measure_is_a_bar_and_periods_are_grouped() -> None:
    bar = recommend_chart(["region_code", "orders"], ["text", "int8"], [["EU", 5], ["NA", 7]])
    assert bar.type == "bar" and not bar.horizontal
    grouped = recommend_chart(COLUMNS, ["text", "numeric", "numeric", "numeric"], ROWS)
    assert grouped.type == "grouped_bar"
    assert grouped.y == ["net_revenue_q3_2026", "net_revenue_q2_2026"]  # the change column is not a bar


def test_shares_of_a_whole_become_a_pie_with_a_slice_limit() -> None:
    rows = [["card", 0.6], ["paypal", 0.25], ["apple_pay", 0.15]]
    assert recommend_chart(["method", "share"], ["text", "numeric"], rows).type == "pie"
    many = [[f"m{i}", 1 / 8] for i in range(8)]
    assert recommend_chart(["method", "share"], ["text", "numeric"], many).type == "bar"


def test_two_categories_stack_and_unplottable_shapes_fall_back_to_a_table() -> None:
    rows = [["Lighting", "EU", 1.0], ["Lighting", "NA", 2.0], ["Smart Home", "EU", 3.0], ["Smart Home", "NA", 4.0]]
    assert recommend_chart(["parent", "region", "net"], ["text", "text", "numeric"], rows).type == "stacked_bar"
    assert recommend_chart(["subject", "body"], ["text", "text"], [["a", "b"]]).type == "table"
    assert recommend_chart(["n"], ["int8"], []).type == "table"
    ids = recommend_chart(["customer_id", "orders"], ["int4", "int8"], [[1, 5], [2, 4]])
    assert ids.type == "bar" and ids.x == "customer_id"


# ---- comparator -----------------------------------------------------------------------------------------------------
def test_order_insensitive_with_extra_columns_and_renames() -> None:
    gold = [("EU", Decimal("104022.0512")), ("NA", Decimal("172289.8044"))]
    pred = [("NA", "North America", Decimal("172289.80")), ("EU", "Europe", Decimal("104022.05"))]
    result = compare_results(gold, pred)
    assert result.match and result.method == "columns"


def test_order_matters_when_asked() -> None:
    gold = [("a", 3), ("b", 2), ("c", 1)]
    assert compare_results(gold, [("a", 3), ("b", 2), ("c", 1)], order_matters=True).match
    assert not compare_results(gold, [("b", 2), ("a", 3), ("c", 1)], order_matters=True).match
    assert compare_results(gold, [("b", 2), ("a", 3), ("c", 1)]).match


def test_numeric_tolerance_rounding_and_percent_scale() -> None:
    assert compare_results([(Decimal("0.156345"),)], [(Decimal("0.1563"),)]).match
    assert compare_results([(Decimal("0.156345"),)], [(Decimal("15.63"),)]).match  # percent
    assert compare_results([(Decimal("104022.4567"),)], [(104022,)]).match  # rounded to an integer
    assert not compare_results([(Decimal("104022.4567"),)], [(104030,)]).match
    assert not compare_results([(Decimal("100"),)], [(Decimal("101"),)]).match


def test_dates_timestamps_and_month_strings() -> None:
    gold = [(dt.datetime(2026, 7, 1, tzinfo=dt.UTC), 5)]
    assert compare_results(gold, [(dt.date(2026, 7, 1), 5)]).match
    assert compare_results(gold, [("2026-07", 5)]).match
    assert compare_results([(dt.date(2026, 7, 1), 5)], [("2026-Q3", 5)]).match
    assert not compare_results(gold, [(dt.date(2026, 8, 1), 5)]).match


def test_row_counts_nulls_and_missing_columns() -> None:
    assert not compare_results([(1,), (2,)], [(1,)]).match
    assert compare_results([(None, 1)], [(None, 1)]).match
    assert not compare_results([(None,)], [(0,)]).match
    assert not compare_results([("EU", 1), ("NA", 2)], [("EU",), ("NA",)]).match
    assert compare_results([], []).match


def test_columns_must_line_up_row_by_row() -> None:
    gold = [("a", 1), ("b", 2)]
    assert not compare_results(gold, [("a", 2), ("b", 1)]).match


def test_one_row_comparison_against_one_row_per_period() -> None:
    gold = [(Decimal("1271860.56"), Decimal("1645578.17"))]
    pred = [(2024, Decimal("1271860.56")), (2025, Decimal("1645578.17"))]
    result = compare_results(gold, pred)
    assert result.match and result.method == "values"
    assert not compare_results([("Paid Search", Decimal("446490.76"))], [("Paid Search", Decimal("1"))]).match
