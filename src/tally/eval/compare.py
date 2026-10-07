"""Execution accuracy: does the predicted query return the same result as the gold query?

Rules (documented in the README):
* The row count must match. Rows are compared as a multiset unless the item says order matters.
* Every gold column must appear among the predicted columns (matched by values, in any position, under any name);
  extra predicted columns are allowed (e.g. a difference column the gold query does not compute).
* Numbers match when equal within 1e-6 relative, when the prediction is the gold value rounded to the prediction's
  decimals, or within 0.001% relative (rounding inside sums). A whole column may be in percent instead of a fraction.
* Timestamps at midnight compare equal to dates, and 'YYYY-MM' / 'YYYY-MM-DD' / 'YYYY-Qn' strings to the dates they
  name. Strings compare after trimming, case-insensitively.
* When the shapes differ and the gold result is a single row, the result also matches if every gold number appears
  in the prediction (one row of "2024 vs 2025" against one row per year). This is reported separately.
"""

from __future__ import annotations

import datetime as dt
import re
from collections.abc import Sequence
from dataclasses import dataclass
from decimal import Decimal
from typing import Any

_YM = re.compile(r"^(\d{4})-(\d{2})$")
_YMD = re.compile(r"^(\d{4})-(\d{2})-(\d{2})$")
_YQ = re.compile(r"^(\d{4})[- ]?Q([1-4])$", re.IGNORECASE)
_TS = re.compile(r"^\d{4}-\d{2}-\d{2}[ T]\d{2}:\d{2}")
SCALES = (1.0, 100.0, 0.01)


@dataclass(frozen=True)
class Comparison:
    match: bool
    method: str
    """exact | columns | values | none"""
    reason: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {"match": self.match, "method": self.method, "reason": self.reason}


@dataclass(frozen=True)
class Num:
    value: float
    decimals: int | None


def normalize(value: Any) -> Any:
    if value is None or isinstance(value, bool):
        return value
    if isinstance(value, Decimal):
        exponent = value.as_tuple().exponent
        places = -exponent if isinstance(exponent, int) and exponent < 0 else 0
        return Num(float(value), places)
    if isinstance(value, int):
        return Num(float(value), 0)
    if isinstance(value, float):
        text = repr(value)
        decimals = len(text.split(".")[1]) if "." in text and "e" not in text else None
        return Num(value, decimals)
    if isinstance(value, dt.datetime):
        if value.tzinfo is not None:
            value = value.astimezone(dt.UTC).replace(tzinfo=None)
        if value.time() == dt.time(0, 0):
            return value.date()
        return value
    if isinstance(value, dt.date):
        return value
    if isinstance(value, str):
        text = value.strip()
        if m := _YMD.match(text):
            return dt.date(int(m[1]), int(m[2]), int(m[3]))
        if m := _YM.match(text):
            return dt.date(int(m[1]), int(m[2]), 1)
        if m := _YQ.match(text):
            return dt.date(int(m[1]), (int(m[2]) - 1) * 3 + 1, 1)
        if _TS.match(text):
            try:
                return normalize(dt.datetime.fromisoformat(text.replace(" ", "T")))
            except ValueError:
                return text.casefold()
        return text.casefold()
    if isinstance(value, dt.timedelta):
        return Num(value.total_seconds(), None)
    return value


def numbers_equal(gold: float, pred: Num, scale: float = 1.0) -> bool:
    target = gold * scale
    diff = abs(target - pred.value)
    if diff <= 1e-6 * max(1.0, abs(target)):
        return True
    if pred.decimals is not None and pred.decimals <= 6 and diff <= 0.5 * 10.0**-pred.decimals + 1e-9:
        return True
    return diff <= 1e-5 * abs(target)


def values_equal(gold: Any, pred: Any, scale: float = 1.0) -> bool:
    if gold is None or pred is None:
        return gold is None and pred is None
    if isinstance(gold, Num) and isinstance(pred, Num):
        return numbers_equal(gold.value, pred, scale)
    if scale != 1.0:
        return False
    if isinstance(gold, dt.date) and isinstance(pred, dt.date):
        return gold == pred
    return bool(gold == pred)


def _column(rows: Sequence[Sequence[Any]], index: int) -> list[Any]:
    return [row[index] for row in rows]


def _sort_key(value: Any) -> tuple[int, Any]:
    if value is None:
        return (0, 0)
    if isinstance(value, Num):
        return (1, value.value)
    if isinstance(value, dt.datetime):
        return (2, value.isoformat())
    if isinstance(value, dt.date):
        return (2, value.isoformat())
    return (3, str(value))


def _columns_match(gold: list[Any], pred: list[Any], ordered: bool, scale: float) -> bool:
    if not ordered:
        gold = sorted(gold, key=_sort_key)
        pred = sorted(pred, key=_sort_key)
    return all(values_equal(g, p, scale) for g, p in zip(gold, pred, strict=True))


def compare_results(
    gold_rows: Sequence[Sequence[Any]],
    pred_rows: Sequence[Sequence[Any]],
    *,
    order_matters: bool = False,
) -> Comparison:
    gold = [[normalize(v) for v in row] for row in gold_rows]
    pred = [[normalize(v) for v in row] for row in pred_rows]
    if not gold and not pred:
        return Comparison(True, "exact", "both empty")
    if len(gold) != len(pred):
        fallback = _values_fallback(gold, pred)
        if fallback is not None:
            return fallback
        return Comparison(False, "none", f"row count {len(pred)} != {len(gold)}")
    n_gold = len(gold[0]) if gold else 0
    n_pred = len(pred[0]) if pred else 0
    if n_pred < n_gold:
        fallback = _values_fallback(gold, pred)
        if fallback is not None:
            return fallback
        return Comparison(False, "none", f"{n_pred} columns, expected at least {n_gold}")
    candidates: list[list[tuple[int, float]]] = []
    for gi in range(n_gold):
        gold_col = _column(gold, gi)
        options = [
            (pi, scale)
            for pi in range(n_pred)
            for scale in SCALES
            if _columns_match(gold_col, _column(pred, pi), order_matters, scale)
        ]
        if not options:
            fallback = _values_fallback(gold, pred)
            if fallback is not None:
                return fallback
            return Comparison(False, "none", f"no predicted column matches gold column {gi + 1}")
        candidates.append(options)
    mapping = _assign(candidates, gold, pred, order_matters)
    if mapping is None:
        return Comparison(False, "none", "columns match individually but not row by row")
    method = (
        "exact" if n_pred == n_gold and all(pi == gi and s == 1.0 for gi, (pi, s) in enumerate(mapping)) else "columns"
    )
    return Comparison(True, method)


def _rows_match(gold: list[list[Any]], pred: list[list[Any]], mapping: list[tuple[int, float]], ordered: bool) -> bool:
    def row_equal(g: list[Any], p: list[Any]) -> bool:
        return all(values_equal(g[gi], p[pi], s) for gi, (pi, s) in enumerate(mapping))

    if ordered:
        return all(row_equal(g, p) for g, p in zip(gold, pred, strict=True))
    unused = list(range(len(pred)))
    for g in gold:
        hit = next((k for k in unused if row_equal(g, pred[k])), None)
        if hit is None:
            return False
        unused.remove(hit)
    return True


def _assign(
    candidates: list[list[tuple[int, float]]], gold: list[list[Any]], pred: list[list[Any]], ordered: bool
) -> list[tuple[int, float]] | None:
    chosen: list[tuple[int, float]] = []

    def search(i: int) -> bool:
        if i == len(candidates):
            return _rows_match(gold, pred, chosen, ordered)
        for option in candidates[i]:
            if any(option[0] == c[0] for c in chosen):
                continue
            chosen.append(option)
            if search(i + 1):
                return True
            chosen.pop()
        return False

    return list(chosen) if search(0) else None


def _values_fallback(gold: list[list[Any]], pred: list[list[Any]]) -> Comparison | None:
    """Only for an all-numeric single gold row (e.g. "2024 vs 2025") against at most one row per number."""
    if len(gold) != 1 or not all(isinstance(v, Num) for v in gold[0]):
        return None
    gold_numbers = [v for v in gold[0] if isinstance(v, Num)]
    pred_numbers = [v for row in pred for v in row if isinstance(v, Num)]
    if len(gold_numbers) < 2 or len(pred) > len(gold_numbers) or len(pred_numbers) > 3 * len(gold_numbers):
        return None
    unused = list(range(len(pred_numbers)))
    for g in gold_numbers:
        hit = next((k for k in unused if numbers_equal(g.value, pred_numbers[k])), None)
        if hit is None:
            return None
        unused.remove(hit)
    return Comparison(True, "values", "every gold number appears in a differently shaped result")
