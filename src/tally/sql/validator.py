"""Static SQL validation with sqlglot, before anything reaches the database.

A query passes only if it is exactly one read-only SELECT (WITH allowed) that reads allow-listed tables and columns,
never a PII column (directly, through a CTE, a whole-row reference or SELECT *), and calls only allow-listed
functions. The outermost LIMIT is enforced. What runs is the SQL regenerated from the validated syntax tree with
comments removed, so the text the database sees is exactly what was checked.

Violations are either *hard* (writes, DDL, several statements, denied functions, system catalogs, explicitly
selected PII): the request is blocked and the model gets no second try, or *soft* (unknown tables or columns,
SELECT * over PII, parse errors): the message goes back to the model for a correction.
"""

from __future__ import annotations

import datetime as dt
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from functools import lru_cache
from typing import cast

import sqlglot
from sqlglot import exp
from sqlglot.errors import ErrorLevel, OptimizeError, ParseError
from sqlglot.optimizer.qualify import qualify
from sqlglot.optimizer.scope import Scope, traverse_scope

DIALECT = "postgres"

# PostgreSQL functions an analytics query may call. Everything else (pg_sleep, dblink, set_config, pg_read_file,
# lo_import, current_setting, query_to_xml, ...) is refused. Each entry is a sample call; the sqlglot expression
# classes it parses to are what the validator allows.
ALLOWED_FUNCTION_SAMPLES: dict[str, str] = {
    # aggregates
    "count": "count(x)",
    "sum": "sum(x)",
    "avg": "avg(x)",
    "min": "min(x)",
    "max": "max(x)",
    "stddev": "stddev(x)",
    "stddev_pop": "stddev_pop(x)",
    "stddev_samp": "stddev_samp(x)",
    "variance": "variance(x)",
    "var_pop": "var_pop(x)",
    "var_samp": "var_samp(x)",
    "percentile_cont": "percentile_cont(0.5) WITHIN GROUP (ORDER BY x)",
    "percentile_disc": "percentile_disc(0.5) WITHIN GROUP (ORDER BY x)",
    "mode": "mode() WITHIN GROUP (ORDER BY x)",
    "string_agg": "string_agg(x, ',')",
    "array_agg": "array_agg(x)",
    "bool_and": "bool_and(x)",
    "bool_or": "bool_or(x)",
    "every": "every(x)",
    "corr": "corr(x, y)",
    "covar_pop": "covar_pop(x, y)",
    "covar_samp": "covar_samp(x, y)",
    "regr_slope": "regr_slope(x, y)",
    "regr_intercept": "regr_intercept(x, y)",
    # window
    "row_number": "row_number() OVER ()",
    "rank": "rank() OVER ()",
    "dense_rank": "dense_rank() OVER ()",
    "percent_rank": "percent_rank() OVER ()",
    "cume_dist": "cume_dist() OVER ()",
    "ntile": "ntile(4) OVER ()",
    "lag": "lag(x) OVER ()",
    "lead": "lead(x) OVER ()",
    "first_value": "first_value(x) OVER ()",
    "last_value": "last_value(x) OVER ()",
    "nth_value": "nth_value(x, 2) OVER ()",
    # conditional and types
    "coalesce": "coalesce(x, 0)",
    "nullif": "nullif(x, 0)",
    "greatest": "greatest(x, y)",
    "least": "least(x, y)",
    "cast": "cast(x AS numeric)",
    "cast_op": "x::numeric",
    "try_cast": "x::text",
    # math
    "abs": "abs(x)",
    "ceil": "ceil(x)",
    "ceiling": "ceiling(x)",
    "floor": "floor(x)",
    "round": "round(x, 2)",
    "round1": "round(x)",
    "trunc": "trunc(x)",
    "sign": "sign(x)",
    "sqrt": "sqrt(x)",
    "cbrt": "cbrt(x)",
    "power": "power(x, 2)",
    "pow": "pow(x, 2)",
    "exp": "exp(x)",
    "ln": "ln(x)",
    "log": "log(x)",
    "log10": "log10(x)",
    "mod": "mod(x, 2)",
    "div": "div(x, 2)",
    "pi": "pi()",
    "width_bucket": "width_bucket(x, 0, 100, 10)",
    # dates and times
    "date_trunc": "date_trunc('month', x)",
    "date_part": "date_part('year', x)",
    "extract": "extract(year FROM x)",
    "age": "age(x, y)",
    "make_date": "make_date(2026, 1, 1)",
    "make_interval": "make_interval(days => 1)",
    "make_timestamp": "make_timestamp(2026, 1, 1, 0, 0, 0)",
    "to_char": "to_char(x, 'YYYY-MM')",
    "to_date": "to_date(x, 'YYYY-MM-DD')",
    "to_timestamp": "to_timestamp(x, 'YYYY-MM-DD')",
    "now": "now()",
    "current_date": "current_date",
    "current_timestamp": "current_timestamp",
    "localtimestamp": "localtimestamp",
    "date_bin": "date_bin(interval '1 day', x, timestamp '2026-01-01')",
    "justify_days": "justify_days(x)",
    "isfinite": "isfinite(x)",
    "timezone": "timezone('UTC', x)",
    "at_time_zone": "x AT TIME ZONE 'UTC'",
    "interval": "interval '1 month'",
    "date_add": "x + interval '1 day'",
    "generate_series": "generate_series(1, 3)",
    "generate_series_dates": "generate_series(date '2026-01-01', date '2026-03-01', interval '1 month')",
    # strings
    "lower": "lower(x)",
    "upper": "upper(x)",
    "initcap": "initcap(x)",
    "length": "length(x)",
    "char_length": "char_length(x)",
    "substring": "substring(x FROM 1 FOR 2)",
    "substring2": "substring(x, 1, 2)",
    "substr": "substr(x, 1, 2)",
    "position": "position('a' IN x)",
    "strpos": "strpos(x, 'a')",
    "trim": "trim(x)",
    "trim2": "trim(BOTH ' ' FROM x)",
    "ltrim": "ltrim(x)",
    "rtrim": "rtrim(x)",
    "btrim": "btrim(x)",
    "replace": "replace(x, 'a', 'b')",
    "concat": "concat(x, y)",
    "concat_op": "x || y",
    "concat_ws": "concat_ws(',', x, y)",
    "split_part": "split_part(x, '-', 1)",
    "left": "left(x, 2)",
    "right": "right(x, 2)",
    "lpad": "lpad(x, 2, '0')",
    "rpad": "rpad(x, 2, '0')",
    "starts_with": "starts_with(x, 'a')",
    "reverse": "reverse(x)",
    "regexp_replace": "regexp_replace(x, 'a', 'b')",
    "translate": "translate(x, 'a', 'b')",
    "format": "format('%s', x)",
    "array_length": "array_length(x, 1)",
    "cardinality": "cardinality(x)",
    "unnest": "unnest(x)",
    # expressions sqlglot models as functions
    "and_or": "a AND b OR NOT c",
    "regex_match": "x ~ 'a'",
    "case": "CASE WHEN x > 1 THEN 1 ELSE 0 END",
    "exists": "EXISTS (SELECT 1)",
    "array": "x = ANY(ARRAY[1, 2])",
}

# Statement and clause types that never belong in a read-only query.
_DENIED_NODE_NAMES = (
    "Insert",
    "Update",
    "Delete",
    "Merge",
    "Create",
    "Drop",
    "Alter",
    "AlterTable",
    "TruncateTable",
    "Copy",
    "Command",
    "Set",
    "Transaction",
    "Commit",
    "Rollback",
    "Grant",
    "Revoke",
    "Into",
    "Lock",
    "LoadData",
    "Use",
    "Pragma",
    "Analyze",
    "Vacuum",
    "Describe",
    "Refresh",
    "Cache",
    "Uncache",
    "Comment",
    "Kill",
    "Show",
)
DENIED_NODES: tuple[type[exp.Expression], ...] = tuple(
    getattr(exp, name) for name in _DENIED_NODE_NAMES if isinstance(getattr(exp, name, None), type)
)
SYSTEM_SCHEMAS = frozenset({"pg_catalog", "information_schema", "pg_toast"})


@dataclass(frozen=True)
class Violation:
    code: str
    message: str
    hard: bool = True

    def to_dict(self) -> dict[str, object]:
        return {"code": self.code, "message": self.message, "hard": self.hard}


@dataclass
class ValidationResult:
    sql: str
    """The SQL that may run: regenerated from the checked tree, comments removed, LIMIT enforced."""
    violations: list[Violation] = field(default_factory=list)
    tables: list[str] = field(default_factory=list)
    columns: list[str] = field(default_factory=list)
    functions: list[str] = field(default_factory=list)
    limit_applied: int | None = None
    """The LIMIT Tally added or lowered, if it had to."""
    pinned_today: bool = False

    @property
    def ok(self) -> bool:
        return not self.violations

    @property
    def hard(self) -> bool:
        return any(v.hard for v in self.violations)

    def feedback(self) -> str:
        return "; ".join(v.message for v in self.violations)

    def to_dict(self) -> dict[str, object]:
        return {
            "ok": self.ok,
            "sql": self.sql,
            "violations": [v.to_dict() for v in self.violations],
            "tables": self.tables,
            "columns": self.columns,
            "functions": self.functions,
            "limit_applied": self.limit_applied,
            "pinned_today": self.pinned_today,
        }


def _function_label(node: exp.Func) -> str:
    if isinstance(node, exp.Anonymous):
        return str(node.name).lower()
    return type(node).__name__


@lru_cache(maxsize=8)
def allowed_function_classes(extra: tuple[str, ...] = ()) -> frozenset[str]:
    """sqlglot class names (and lower-case names of functions sqlglot does not model) for the allow-list."""
    labels: set[str] = set()
    samples = list(ALLOWED_FUNCTION_SAMPLES.values()) + [f"{name}(x)" for name in extra]
    for sample in samples:
        tree = sqlglot.parse_one(f"SELECT {sample} FROM t", read=DIALECT)  # noqa: S608  # constant samples
        for node in tree.find_all(exp.Func):
            labels.add(_function_label(node))
    labels.update(name.lower() for name in extra)
    return frozenset(labels)


class SQLValidator:
    def __init__(
        self,
        *,
        schema: Mapping[str, Mapping[str, str]],
        pii: Mapping[str, Iterable[str]],
        max_rows: int = 1000,
        data_schema: str = "public",
        extra_functions: Iterable[str] = (),
        today: dt.date | None = None,
    ) -> None:
        self.schema = {t.lower(): {c.lower(): ty for c, ty in cols.items()} for t, cols in schema.items()}
        self.pii = {t.lower(): {c.lower() for c in cols} for t, cols in pii.items()}
        self.max_rows = max_rows
        self.data_schema = data_schema.lower()
        self.allowed_functions = allowed_function_classes(tuple(sorted(extra_functions)))
        self.today = today
        """When set (the demo), CURRENT_DATE / NOW() are pinned to this date so answers are reproducible."""

    # ---- entry point ------------------------------------------------------------------------------------------
    def validate(self, sql: str) -> ValidationResult:
        result = ValidationResult(sql="")
        text = sql.strip().rstrip(";").strip()
        if not text:
            result.violations.append(Violation("empty", "the query is empty", hard=False))
            return result
        try:
            statements = [s for s in sqlglot.parse(text, read=DIALECT, error_level=ErrorLevel.RAISE) if s is not None]
        except ParseError as exc:
            message = str(exc).splitlines()[0][:300]
            result.violations.append(Violation("parse_error", f"could not parse the SQL: {message}", hard=False))
            return result
        except Exception as exc:  # sqlglot can raise TokenError and friends
            result.violations.append(Violation("parse_error", f"could not parse the SQL: {exc}"[:300], hard=False))
            return result
        if len(statements) != 1:
            result.violations.append(
                Violation("multiple_statements", f"exactly one statement is allowed, got {len(statements)}")
            )
            return result
        tree = cast("exp.Expression", statements[0])
        self._check_statement(tree, result)
        if result.hard:
            return result
        self._check_tables(tree, result)
        self._check_functions(tree, result)
        if result.hard:
            return result
        self._check_columns(tree, result)
        if result.violations:
            return result
        self._enforce_limit(tree, result)
        if self.today is not None:
            tree = self._pin_today(tree, result)
        result.sql = tree.sql(dialect=DIALECT, comments=False)
        return result

    # ---- checks -----------------------------------------------------------------------------------------------
    def _check_statement(self, tree: exp.Expression, result: ValidationResult) -> None:
        if not isinstance(tree, exp.Query):
            kind = type(tree).__name__.upper()
            result.violations.append(
                Violation("not_select", f"only SELECT queries are allowed (got {kind}); Tally never changes data")
            )
            return
        for node in tree.walk():
            if isinstance(node, DENIED_NODES):
                kind = type(node).__name__.upper()
                result.violations.append(Violation("write_or_ddl", f"{kind} is not allowed inside a read-only query"))
                return

    def _cte_names(self, tree: exp.Expression) -> set[str]:
        return {cte.alias_or_name.lower() for cte in tree.find_all(exp.CTE)}

    def _check_tables(self, tree: exp.Expression, result: ValidationResult) -> None:
        ctes = self._cte_names(tree)
        seen: list[str] = []
        for table in tree.find_all(exp.Table):
            if isinstance(table.this, exp.Func):
                continue  # table functions such as generate_series(...) are checked as functions
            name = table.name.lower()
            schema = (table.db or "").lower()
            catalog = (table.catalog or "").lower()
            if catalog or schema in SYSTEM_SCHEMAS or name.startswith("pg_"):
                result.violations.append(
                    Violation("system_table", f"system catalogs are not allowed: {table.sql(dialect=DIALECT)}")
                )
                continue
            if schema and schema != self.data_schema:
                result.violations.append(
                    Violation("schema_not_allowed", f"only the {self.data_schema!r} schema may be queried")
                )
                continue
            if not schema and name in ctes:
                continue
            if name not in self.schema:
                result.violations.append(
                    Violation("unknown_table", f"table {name!r} does not exist or is not allowed", hard=False)
                )
                continue
            if name not in seen:
                seen.append(name)
        result.tables = seen

    def _check_functions(self, tree: exp.Expression, result: ValidationResult) -> None:
        used: list[str] = []
        for node in tree.find_all(exp.Func):
            label = _function_label(node)
            display = str(node.name).lower() if isinstance(node, exp.Anonymous) else (node.sql_name() or label).lower()
            if label not in self.allowed_functions:
                result.violations.append(
                    Violation("function_not_allowed", f"function {display}() is not on the allow-list")
                )
            elif display not in used:
                used.append(display)
        result.functions = used

    def _check_columns(self, tree: exp.Expression, result: ValidationResult) -> None:
        explicit = {ident.name.lower() for ident in tree.find_all(exp.Identifier)}
        try:
            qualified = qualify(
                tree.copy(),
                schema=cast("dict[str, object]", self.schema),
                dialect=DIALECT,
                validate_qualify_columns=True,
                quote_identifiers=False,
                identify=False,
            )
        except OptimizeError as exc:
            message = str(exc).split(". Line:")[0]
            result.violations.append(Violation("unknown_column", message, hard=False))
            return
        except Exception as exc:
            result.violations.append(
                Violation("analysis_error", f"could not analyse the query: {exc}"[:300], hard=False)
            )
            return
        columns: list[str] = []
        for scope in traverse_scope(qualified):
            aliases = self._source_aliases(scope)
            named = {s.lower() for s in getattr(scope.expression, "named_selects", [])}
            projections = list(getattr(scope.expression, "expressions", []))
            for column in scope.columns:
                name = column.name.lower()
                table_alias = column.table.lower()
                if not table_alias:
                    in_projection = any(column is p or column in list(p.find_all(exp.Column)) for p in projections)
                    if not in_projection and name in named:
                        continue  # ORDER BY / GROUP BY an output alias, even one named like a table or CTE
                    if name in aliases:
                        result.violations.append(
                            Violation("whole_row", f"whole-row reference {name!r} is not allowed; select columns")
                        )
                    elif in_projection or name not in named:
                        result.violations.append(
                            Violation("unresolved_column", f"column {name!r} could not be resolved", hard=False)
                        )
                    continue
                source = self._resolve(scope, table_alias)
                if isinstance(source, exp.Table):
                    base = source.name.lower()
                    if name in self.pii.get(base, set()):
                        if name in explicit:
                            result.violations.append(
                                Violation("pii_column", f"{base}.{name} is personal data and may not be queried")
                            )
                        else:
                            result.violations.append(
                                Violation(
                                    "pii_via_star",
                                    f"SELECT * on {base} would include personal data ({name}); list the columns needed",
                                    hard=False,
                                )
                            )
                    label = f"{base}.{name}"
                    if label not in columns:
                        columns.append(label)
        # Whole-row references (SELECT c FROM customers c, row_to_json(c)): the qualifier turns them into TableColumn.
        for node in qualified.find_all(exp.TableColumn):
            result.violations.append(
                Violation("whole_row", f"whole-row reference {node.name!r} is not allowed; select columns")
            )
        for scope in traverse_scope(qualified):
            aliases = self._source_aliases(scope)
            named = {s.lower() for s in getattr(scope.expression, "named_selects", [])}
            projections = list(getattr(scope.expression, "expressions", []))
            for column in scope.expression.find_all(exp.Column):
                name = column.name.lower()
                if column.table or name not in aliases or column in scope.columns:
                    continue
                in_projection = any(column is p or column in list(p.find_all(exp.Column)) for p in projections)
                if not in_projection and name in named:
                    continue
                result.violations.append(
                    Violation("whole_row", f"whole-row reference {column.name!r} is not allowed; select columns")
                )
        result.columns = columns
        seen: set[tuple[str, str]] = set()
        result.violations[:] = [
            v
            for v in result.violations
            if (v.code, v.message) not in seen and not seen.add((v.code, v.message))  # type: ignore[func-returns-value]
        ]

    @staticmethod
    def _source_aliases(scope: Scope) -> set[str]:
        aliases: set[str] = set()
        current: Scope | None = scope
        while current is not None:
            aliases.update(alias.lower() for alias in current.sources)
            current = current.parent
        return aliases

    @staticmethod
    def _resolve(scope: Scope, alias: str) -> object:
        current: Scope | None = scope
        while current is not None:
            for key, source in current.sources.items():
                if key.lower() == alias:
                    return source
            current = current.parent
        return None

    def _pin_today(self, tree: exp.Expression, result: ValidationResult) -> exp.Expression:
        assert self.today is not None
        day = self.today.isoformat()

        def replace(node: exp.Expression) -> exp.Expression:
            if isinstance(node, exp.CurrentDate):
                result.pinned_today = True
                return exp.cast(exp.Literal.string(day), exp.DataType.Type.DATE)
            if isinstance(node, (exp.CurrentTimestamp, exp.Localtimestamp)):
                result.pinned_today = True
                return exp.cast(exp.Literal.string(f"{day} 00:00:00+00"), exp.DataType.Type.TIMESTAMPTZ)
            return node

        return tree.transform(replace)

    def _enforce_limit(self, tree: exp.Expression, result: ValidationResult) -> None:
        fetch = tree.args.get("fetch")
        if fetch is not None:
            count = fetch.args.get("count")
            value = _literal_int(count)
            if value is None or value > self.max_rows:
                tree.set("fetch", None)
            else:
                return
        limit = tree.args.get("limit")
        if limit is not None:
            value = _literal_int(limit.expression)
            if value is not None and value <= self.max_rows:
                return
        tree.set("limit", exp.Limit(expression=exp.Literal.number(self.max_rows)))
        result.limit_applied = self.max_rows


def _literal_int(node: exp.Expression | None) -> int | None:
    if isinstance(node, exp.Literal) and node.is_int:
        return int(node.this)
    return None
