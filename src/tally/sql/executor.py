"""The execution guard: every generated query runs as the read-only reader role, in a READ ONLY transaction that is
always rolled back, with a statement timeout, a fixed time zone and search path, the row-level security scope, a row
cap, and an EXPLAIN cost check before it runs.

psycopg sends queries with the extended protocol, which refuses more than one statement per call, so even text that
somehow contained "SELECT 1; DROP TABLE x" could not run its second half (tested).
"""

from __future__ import annotations

import datetime as dt
import time
from collections.abc import Iterator, Sequence
from contextlib import contextmanager
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any, Protocol

import psycopg
from psycopg import errors, sql

from tally.db import Database
from tally.security import SCOPE_SETTING


@dataclass(frozen=True)
class ResultColumn:
    name: str
    type: str


@dataclass
class QueryResult:
    columns: list[ResultColumn]
    rows: list[tuple[Any, ...]]
    truncated: bool = False
    duration_ms: float = 0.0

    @property
    def row_count(self) -> int:
        return len(self.rows)

    def json_rows(self, limit: int | None = None) -> list[list[Any]]:
        rows = self.rows if limit is None else self.rows[:limit]
        return [[to_json_value(v) for v in row] for row in rows]


@dataclass(frozen=True)
class PlanCost:
    total_cost: float
    plan_rows: float
    """The largest row estimate of any node in the plan."""
    node: str = ""
    details: dict[str, Any] = field(default_factory=dict)


class QueryError(RuntimeError):
    """The database rejected the query (syntax, missing column, permission, ...): worth a correction."""

    def __init__(self, message: str, *, code: str = "", timeout: bool = False, permission: bool = False) -> None:
        super().__init__(message)
        self.code = code
        self.timeout = timeout
        self.permission = permission


class Executor(Protocol):
    def explain(self, sql: str) -> PlanCost: ...

    def execute(self, sql: str) -> QueryResult: ...


def to_json_value(value: Any) -> Any:
    if isinstance(value, Decimal):
        return float(value) if value == value.to_integral_value() or abs(value) < Decimal("1e15") else str(value)
    if isinstance(value, dt.datetime):
        return value.isoformat()
    if isinstance(value, dt.date | dt.time):
        return value.isoformat()
    if isinstance(value, dt.timedelta):
        return value.total_seconds()
    if isinstance(value, bytes | memoryview):
        return "<binary>"
    if isinstance(value, list | tuple):
        return [to_json_value(v) for v in value]
    return value


def _message(exc: psycopg.Error) -> str:
    diag = exc.diag
    parts = [diag.message_primary or str(exc).splitlines()[0]]
    if diag.message_detail:
        parts.append(diag.message_detail)
    if diag.message_hint:
        parts.append(f"Hint: {diag.message_hint}")
    return " ".join(p for p in parts if p)[:500]


class ReadOnlyExecutor:
    def __init__(
        self,
        db: Database,
        *,
        statement_timeout_ms: int = 5000,
        max_rows: int = 1000,
        region_scope: str = "*",
        data_schema: str = "public",
        timezone: str = "UTC",
        scoped_role: str = "tally_scoped_reader",
    ) -> None:
        self.db = db
        self.statement_timeout_ms = statement_timeout_ms
        self.max_rows = max_rows
        self.region_scope = region_scope
        self.data_schema = data_schema
        self.timezone = timezone
        self.scoped_role = scoped_role

    @contextmanager
    def _transaction(self) -> Iterator[psycopg.Cursor[Any]]:
        with self.db.reader() as conn:
            conn.execute("BEGIN READ ONLY")
            try:
                if self.region_scope != "*":
                    conn.execute(sql.SQL("SET LOCAL ROLE {}").format(sql.Identifier(self.scoped_role)))
                with conn.cursor() as cur:
                    cur.execute(
                        "SELECT set_config('statement_timeout', %s, true), set_config(%s, %s, true), "
                        "set_config('TimeZone', %s, true), set_config('search_path', %s, true)",
                        (
                            f"{self.statement_timeout_ms}ms",
                            SCOPE_SETTING,
                            self.region_scope,
                            self.timezone,
                            self.data_schema,
                        ),
                    )
                    yield cur
            finally:
                conn.execute("ROLLBACK")

    def explain(self, sql: str) -> PlanCost:
        try:
            with self._transaction() as cur:
                cur.execute(f"EXPLAIN (FORMAT JSON) {sql}")  # validated SQL only
                row = cur.fetchone()
        except psycopg.Error as exc:
            raise self._error(exc) from exc
        plan = row[0][0]["Plan"] if row else {}
        return PlanCost(
            total_cost=float(plan.get("Total Cost", 0.0)),
            plan_rows=_max_rows(plan),
            node=str(plan.get("Node Type", "")),
            details={k: plan.get(k) for k in ("Node Type", "Startup Cost", "Total Cost", "Plan Rows")},
        )

    def execute(self, sql: str) -> QueryResult:
        started = time.monotonic()
        try:
            with self._transaction() as cur:
                cur.execute(sql)  # validated SQL only
                description = cur.description or []
                rows = cur.fetchmany(self.max_rows + 1)
                columns = [ResultColumn(d.name, _type_name(cur, d.type_code)) for d in description]
        except psycopg.Error as exc:
            raise self._error(exc) from exc
        truncated = len(rows) > self.max_rows
        return QueryResult(
            columns=columns,
            rows=[tuple(r) for r in rows[: self.max_rows]],
            truncated=truncated,
            duration_ms=round((time.monotonic() - started) * 1000, 1),
        )

    @staticmethod
    def _error(exc: psycopg.Error) -> QueryError:
        if isinstance(exc, errors.QueryCanceled):
            return QueryError(
                "the query was cancelled by the statement timeout; aggregate or filter more",
                code="timeout",
                timeout=True,
            )
        if isinstance(exc, errors.InsufficientPrivilege):
            return QueryError(_message(exc), code=exc.sqlstate or "", permission=True)
        return QueryError(_message(exc), code=exc.sqlstate or "")


def _max_rows(node: dict[str, Any]) -> float:
    """The largest row estimate of any node: a cross join shows up here even under a COUNT(*) or a LIMIT."""
    rows = float(node.get("Plan Rows", 0.0))
    for child in node.get("Plans", []) or []:
        rows = max(rows, _max_rows(child))
    return rows


def _type_name(cur: psycopg.Cursor[Any], oid: int) -> str:
    info = cur.connection.adapters.types.get(oid)
    return info.name if info else str(oid)


def run_owner_query(db: Database, sql: str, params: Sequence[Any] = ()) -> QueryResult:
    """For the evaluation's gold queries (trusted, hand-written): same session settings, owner connection."""
    started = time.monotonic()
    with db.owner() as conn, conn.transaction(), conn.cursor() as cur:
        cur.execute("SET LOCAL TimeZone = 'UTC'")
        if params:
            cur.execute(sql, params)
        else:
            cur.execute(sql)
        description = cur.description or []
        rows = cur.fetchall()
        columns = [ResultColumn(d.name, _type_name(cur, d.type_code)) for d in description]
    return QueryResult(columns, [tuple(r) for r in rows], duration_ms=round((time.monotonic() - started) * 1000, 1))
