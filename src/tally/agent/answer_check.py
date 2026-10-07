"""The answer may only use numbers that are in the result.

Every number in the model's answer is matched against the result cells, the column names, the numbers in the
question, and the literals of the SQL that produced the result (its date bounds and the day before each, LIMIT and
thresholds, so an answer may say which period it covers) plus today's date. A match
allows the usual ways a person writes a number: rounding or truncation to the digits shown, thousands separators,
K/M/B suffixes, a fraction written as a percentage, and a dropped minus sign ("fell 16%" for -0.16). Integers up to
12 (counts of rows, months) and the components of dates in the result are allowed too. Anything else is reported,
the model is asked once to rewrite, and if that still fails a template answer built from the result is used.
"""

from __future__ import annotations

import datetime as dt
import re
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation
from typing import Any

_NUMBER = re.compile(
    r"(?<![\w.])(?P<sign>[-−])?\$?(?P<num>\d{1,3}(?:,\d{3})+(?:\.\d+)?|\d+(?:\.\d+)?)"
    r"(?P<suffix>\s?(?:%|percent\b|pct\b|[kK]\b|[mM]\b|[bB]n?\b|thousand\b|million\b|billion\b))?"
)
_SCALE = {"k": 1e3, "thousand": 1e3, "m": 1e6, "million": 1e6, "b": 1e9, "bn": 1e9, "billion": 1e9}
_DATE = re.compile(r"^(\d{4})-(\d{2})(?:-(\d{2}))?")
SMALL_INT_LIMIT = 12


@dataclass(frozen=True)
class FoundNumber:
    text: str
    value: float
    unit: float
    """One unit of the last digit shown, in the value's scale (e.g. 0.1 for "12.3", 10,000 for "1.23M")."""
    percent: bool


@dataclass
class AnswerCheck:
    ok: bool
    numbers: list[str] = field(default_factory=list)
    unsupported: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {"ok": self.ok, "numbers": self.numbers, "unsupported": self.unsupported}


def extract_numbers(text: str) -> list[FoundNumber]:
    found: list[FoundNumber] = []
    for match in _NUMBER.finditer(text):
        raw = match.group("num").replace(",", "")
        try:
            value = float(raw)
        except ValueError:
            continue
        decimals = len(raw.split(".")[1]) if "." in raw else 0
        suffix = (match.group("suffix") or "").strip().lower()
        percent = suffix in {"%", "percent", "pct"}
        scale = _SCALE.get(suffix, 1.0)
        sign = -1.0 if match.group("sign") else 1.0
        found.append(
            FoundNumber(
                text=match.group(0).strip(),
                value=sign * value * scale,
                unit=(10.0**-decimals) * scale,
                percent=percent,
            )
        )
    return found


def reference_values(rows: Iterable[Sequence[Any]], extra_texts: Iterable[str] = ()) -> list[float]:
    values: list[float] = []
    for row in rows:
        for cell in row:
            values.extend(_cell_values(cell))
    for text in extra_texts:
        values.extend(n.value for n in extract_numbers(text))
    return values


def _cell_values(cell: Any) -> list[float]:
    if cell is None or isinstance(cell, bool):
        return []
    if isinstance(cell, int | float | Decimal):
        return [float(cell)]
    if isinstance(cell, dt.datetime | dt.date):
        return [float(cell.year), float(cell.month), float(cell.day)]
    if isinstance(cell, str):
        stripped = cell.strip()
        date = _DATE.match(stripped)
        if date:
            return [float(g) for g in date.groups() if g]
        try:
            return [float(Decimal(stripped.replace(",", "")))]
        except (InvalidOperation, ValueError):
            return [n.value for n in extract_numbers(stripped)]
    if isinstance(cell, list | tuple):
        return [v for item in cell for v in _cell_values(item)]
    return []


def _supported(number: FoundNumber, references: Sequence[float]) -> bool:
    value = number.value
    if value.is_integer() and abs(value) <= SMALL_INT_LIMIT:
        return True
    tolerance = number.unit + 1e-9
    candidates = [(value, tolerance)]
    if number.percent:
        candidates.append((value / 100.0, tolerance / 100.0))
    for candidate, tol in candidates:
        for ref in references:
            # Rounding (half a unit) always; truncation (a whole unit) only of a value that has a fraction.
            allowed = tol if not float(ref).is_integer() else tol / 2
            if abs(abs(candidate) - abs(ref)) <= allowed:
                return True
    return False


def check_answer(
    answer: str,
    rows: Iterable[Sequence[Any]],
    *,
    question: str = "",
    row_count: int = 0,
    columns: Sequence[str] = (),
    sql: str = "",
    today: dt.date | None = None,
) -> AnswerCheck:
    """Column names count as part of the result ("net_revenue_q3_2026" allows 2026)."""
    context = [question, *(c.replace("_", " ") for c in columns), *sql_literals(sql)]
    references = reference_values(rows, context)
    if today is not None:
        references.extend([float(today.year), float(today.month), float(today.day)])
    references.append(float(row_count))
    numbers = extract_numbers(answer)
    unsupported = [n.text for n in numbers if not _supported(n, references)]
    return AnswerCheck(ok=not unsupported, numbers=[n.text for n in numbers], unsupported=unsupported)


_SQL_STRING = re.compile(r"'((?:[^']|'')*)'")
_SQL_NUMBER = re.compile(r"(?<![\w.])\d+(?:\.\d+)?(?![\w.])")


def sql_literals(sql: str) -> list[str]:
    """Numbers written in the SQL: date strings ('2026-07-01' gives 2026, 7, 1) and numeric constants."""
    if not sql:
        return []
    literals: list[str] = []
    for text in _SQL_STRING.findall(sql):
        match = _DATE.match(text.strip())
        if not match:
            continue
        literals.extend(g for g in match.groups() if g)
        if match.group(3):
            # A half-open range ending on 2026-10-01 is described as "through September 30".
            day = dt.date(int(match.group(1)), int(match.group(2)), int(match.group(3))) - dt.timedelta(days=1)
            literals.extend([str(day.year), str(day.month), str(day.day)])
    without_strings = _SQL_STRING.sub(" ", sql)
    literals.extend(_SQL_NUMBER.findall(without_strings))
    return literals


def format_number(value: Any, column: str = "") -> str:
    if value is None:
        return "no value"
    if isinstance(value, bool):
        return "yes" if value else "no"
    if isinstance(value, int | float | Decimal):
        number = float(value)
        name = column.lower()
        if any(k in name for k in ("pct", "share", "rate", "ratio")) and abs(number) <= 5:
            return f"{number * 100:.1f}%"
        prefix = "$" if name.endswith("_usd") or "revenue" in name or "usd" in name else ""
        if number.is_integer() and not prefix:
            return f"{int(number):,}"
        return f"{prefix}{number:,.2f}"
    return str(value)


def template_answer(columns: Sequence[str], rows: Sequence[Sequence[Any]], row_count: int, truncated: bool) -> str:
    """A plain answer built from the result only (used when the model's answer kept citing other numbers)."""
    if row_count == 0:
        return "No rows matched the question."

    def describe(row: Sequence[Any]) -> str:
        return ", ".join(
            f"{col.replace('_', ' ')} {format_number(val, col)}" for col, val in zip(columns, row, strict=False)
        )

    if row_count == 1:
        return f"Result: {describe(rows[0])}."
    shown = "; ".join(describe(r) for r in rows[:3])
    more = f" ({row_count} rows{', truncated' if truncated else ''}; first 3 shown)" if row_count > 3 else ""
    return f"Result: {shown}{more}."
