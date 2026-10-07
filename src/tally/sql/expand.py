"""Metric expansion: semantic views referenced like tables become CTEs with their reviewed SQL, before validation.

The model writes `SELECT region_code, SUM(net_revenue_usd) FROM order_revenue GROUP BY 1`; Tally runs
`WITH order_revenue AS (<the semantic layer's SQL>) SELECT ...`. The business rules (currency conversion, refunds,
cancelled, soft-deleted and test rows) are written once by a person, and the expanded query is validated like any
other, so a semantic view can never widen what the reader role may see.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field

import sqlglot
from sqlglot import exp

DIALECT = "postgres"


@dataclass
class Expansion:
    sql: str
    views: list[str] = field(default_factory=list)

    @property
    def expanded(self) -> bool:
        return bool(self.views)


def expand_views(sql: str, views: Mapping[str, str]) -> Expansion:
    """`views` maps a view name to its SELECT. Unparseable SQL is returned unchanged (the validator reports it)."""
    if not views:
        return Expansion(sql)
    try:
        statements = sqlglot.parse(sql, read=DIALECT)
    except Exception:
        return Expansion(sql)
    if len(statements) != 1 or not isinstance(statements[0], exp.Query):
        return Expansion(sql)
    tree = statements[0]
    defined = {cte.alias_or_name.lower() for cte in tree.find_all(exp.CTE)}
    lowered = {name.lower(): body for name, body in views.items()}
    used: list[str] = []
    for table in tree.find_all(exp.Table):
        name = table.name.lower()
        if not table.db and name in lowered and name not in defined and name not in used:
            used.append(name)
    if not used:
        return Expansion(sql)
    ctes = [
        exp.CTE(this=sqlglot.parse_one(lowered[name], read=DIALECT), alias=exp.TableAlias(this=exp.to_identifier(name)))
        for name in used
    ]
    existing = tree.args.get("with_")
    if isinstance(existing, exp.With):
        existing.set("expressions", [*ctes, *existing.expressions])
    else:
        tree.set("with_", exp.With(expressions=ctes))
    return Expansion(tree.sql(dialect=DIALECT), used)
