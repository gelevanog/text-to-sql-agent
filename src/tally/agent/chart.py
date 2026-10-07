"""A deterministic chart recommender: the chart follows from the shape of the result, not from the model.

Rules, first match wins:
1. no rows -> table; one row of numbers -> KPI tiles (with the change, if the result has one);
2. a time column and a measure -> line (one series per category when there is a category with at most 8 values);
3. one category and a measure that is a share of a whole (values add up to 1 or 100), at most 6 slices -> pie;
4. one category and one measure -> bar (horizontal when labels are long or there are many);
5. one category and 2-4 measures of the same kind (e.g. one per period) -> grouped bar;
6. two categories and a measure -> stacked bar (the smaller category as the stack);
7. anything else -> table only.
The output is a small JSON spec the web app renders with Recharts.
"""

from __future__ import annotations

import re
from collections.abc import Sequence
from dataclasses import asdict, dataclass, field
from typing import Any

NUMERIC_TYPES = {"int2", "int4", "int8", "numeric", "float4", "float8", "integer", "bigint", "smallint", "money"}
TEMPORAL_TYPES = {"date", "timestamp", "timestamptz"}
_TIME_NAME = re.compile(r"(^|_)(date|day|week|month|quarter|year|period|time|hour)(_|$)")
_ID_NAME = re.compile(r"(^id$|_id$)")
_CHANGE_NAME = re.compile(r"(pct|percent|change|diff|delta|growth|share|rate|ratio)")
_DATE_VALUE = re.compile(r"^\d{4}-\d{2}(-\d{2})?")
MAX_SERIES = 8
MAX_BARS = 30
MAX_PIE = 6


@dataclass
class ChartSpec:
    type: str  # kpi | line | bar | grouped_bar | stacked_bar | pie | table
    reason: str
    x: str | None = None
    y: list[str] = field(default_factory=list)
    series: list[str] = field(default_factory=list)
    """For pivoted charts (line by category, stacked bar): the series keys in data records."""
    horizontal: bool = False
    data: list[dict[str, Any]] = field(default_factory=list)
    title: str = ""

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def _is_number(value: Any) -> bool:
    return isinstance(value, int | float) and not isinstance(value, bool)


def _role(name: str, type_: str, values: Sequence[Any]) -> str:
    present = [v for v in values if v is not None]
    lowered = name.lower()
    if type_ in TEMPORAL_TYPES:
        return "time"
    if present and all(isinstance(v, str) and _DATE_VALUE.match(v) for v in present) and _TIME_NAME.search(lowered):
        return "time"
    if _ID_NAME.search(lowered):
        return "category"
    if type_ in NUMERIC_TYPES or (present and all(_is_number(v) for v in present)):
        if _TIME_NAME.search(lowered) and present and all(_is_number(v) and float(v).is_integer() for v in present):
            years = all(1990 <= float(v) <= 2100 for v in present)
            return "time" if years or lowered.endswith(("month", "quarter", "week", "hour")) else "measure"
        return "measure"
    return "category"


def _label(value: Any) -> str:
    if value is None:
        return "(none)"
    if isinstance(value, str) and _DATE_VALUE.match(value):
        return value[:10]
    return str(value)


def recommend_chart(columns: Sequence[str], types: Sequence[str], rows: Sequence[Sequence[Any]]) -> ChartSpec:
    if not rows:
        return ChartSpec("table", "no rows")
    roles = [_role(c, t, [r[i] for r in rows]) for i, (c, t) in enumerate(zip(columns, types, strict=True))]
    time = [c for c, r in zip(columns, roles, strict=True) if r == "time"]
    cats = [c for c, r in zip(columns, roles, strict=True) if r == "category"]
    measures = [c for c, r in zip(columns, roles, strict=True) if r == "measure"]
    index = {c: i for i, c in enumerate(columns)}
    records = [
        {c: (row[index[c]] if c not in cats and c not in time else _label(row[index[c]])) for c in columns}
        for row in rows
    ]

    if not measures:
        return ChartSpec("table", "no numeric column to plot")
    level = [m for m in measures if not _CHANGE_NAME.search(m.lower())] or measures

    if len(rows) == 1 and not time:
        shown = measures[:4]
        return ChartSpec("kpi", "a single row of numbers", x=cats[0] if cats else None, y=shown, data=records)

    if time and len(rows) >= 2:
        x = time[0]
        if cats:
            series_col = cats[0]
            keys = list(dict.fromkeys(str(r[series_col]) for r in records))
            if len(keys) <= MAX_SERIES:
                y = level[0]
                pivot: dict[str, dict[str, Any]] = {}
                for r in records:
                    point = pivot.setdefault(str(r[x]), {x: r[x]})
                    point[str(r[series_col])] = r[y]
                data = sorted(pivot.values(), key=lambda p: str(p[x]))
                return ChartSpec("line", f"{y} over {x}, one line per {series_col}", x=x, y=[y], series=keys, data=data)
        data = sorted(records, key=lambda r: str(r[x]))
        return ChartSpec("line", f"{', '.join(level[:3])} over {x}", x=x, y=level[:3], data=data)

    if len(cats) == 1:
        x = cats[0]
        long_labels = max(len(str(r[x])) for r in records) > 14
        if len(level) == 1:
            y = level[0]
            values = [float(r[y]) for r in records if _is_number(r[y])]
            total = sum(values)
            share_like = bool(re.search(r"share|pct|percent|portion|mix", y.lower())) and (
                abs(total - 1) < 0.02 or abs(total - 100) < 2
            )
            if share_like and len(rows) <= MAX_PIE and all(v >= 0 for v in values):
                return ChartSpec(
                    "pie", f"{y} is a share of a whole across {len(rows)} {x} values", x=x, y=[y], data=records
                )
            if len(rows) <= MAX_BARS:
                return ChartSpec(
                    "bar", f"{y} by {x}", x=x, y=[y], horizontal=long_labels or len(rows) > 8, data=records
                )
        elif 2 <= len(level) <= 4 and len(rows) <= 20:
            return ChartSpec(
                "grouped_bar",
                f"{', '.join(level)} side by side by {x}",
                x=x,
                y=level,
                horizontal=long_labels,
                data=records,
            )
        return ChartSpec("table", f"too many rows ({len(rows)}) for a readable chart")

    if len(cats) >= 2 and len(level) >= 1:
        a, b = cats[0], cats[1]
        distinct_a = list(dict.fromkeys(str(r[a]) for r in records))
        distinct_b = list(dict.fromkeys(str(r[b]) for r in records))
        x, s = (a, b) if len(distinct_a) >= len(distinct_b) else (b, a)
        keys = distinct_b if s == b else distinct_a
        if len(keys) <= MAX_SERIES and len(rows) <= 200:
            y = level[0]
            stacks: dict[str, dict[str, Any]] = {}
            for r in records:
                point = stacks.setdefault(str(r[x]), {x: r[x]})
                point[str(r[s])] = r[y]
            return ChartSpec(
                "stacked_bar", f"{y} by {x}, stacked by {s}", x=x, y=[y], series=keys, data=list(stacks.values())
            )
    return ChartSpec("table", "no chart fits this shape")
