"""Schema introspection: tables, columns, types, keys, foreign keys, row counts and sample values, merged with the
semantic layer into one catalog that retrieval, prompting and validation share."""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

import psycopg
from psycopg import sql

from tally.schema.semantic import EMPTY_LAYER, SemanticLayer

SAMPLE_TYPES = {"text", "character", "character varying", "boolean", "smallint"}
MAX_SAMPLE_DISTINCT = 25


@dataclass
class ColumnInfo:
    name: str
    type: str
    nullable: bool = True
    primary_key: bool = False
    references: str | None = None
    """"table.column" this column points to (foreign key or semantic-layer join)."""
    description: str = ""
    pii: bool = False
    samples: list[str] = field(default_factory=list)


@dataclass
class TableInfo:
    name: str
    columns: list[ColumnInfo]
    row_count: int = 0
    description: str = ""
    synonyms: list[str] = field(default_factory=list)
    is_view: bool = False
    sql: str = ""
    """Semantic views only: the SELECT that Tally expands as a CTE."""

    def column(self, name: str) -> ColumnInfo | None:
        return next((c for c in self.columns if c.name == name), None)

    @property
    def visible_columns(self) -> list[ColumnInfo]:
        return [c for c in self.columns if not c.pii]


@dataclass
class Catalog:
    tables: dict[str, TableInfo]
    views: dict[str, TableInfo]
    layer: SemanticLayer = EMPTY_LAYER
    joins: list[tuple[str, str]] = field(default_factory=list)

    @property
    def pii(self) -> dict[str, set[str]]:
        return {
            t.name: {c.name for c in t.columns if c.pii} for t in self.tables.values() if any(c.pii for c in t.columns)
        }

    def sqlglot_schema(self) -> dict[str, dict[str, str]]:
        """Base tables only (views are expanded into CTEs before validation); PII columns included so that they
        resolve - and are then refused - instead of looking like typos."""
        return {t.name: {c.name: c.type for c in t.columns} for t in self.tables.values()}

    def without_semantic_layer(self) -> Catalog:
        """The ablation: introspected names, types, keys, row counts and samples only - no descriptions, synonyms,
        metrics, rules or semantic views. PII flags stay (they are a safety setting, not semantics)."""
        tables = {
            name: TableInfo(
                name=t.name,
                columns=[
                    ColumnInfo(
                        c.name,
                        c.type,
                        c.nullable,
                        c.primary_key,
                        c.references if c.references else None,
                        pii=c.pii,
                        samples=c.samples,
                    )
                    for c in t.columns
                ],
                row_count=t.row_count,
            )
            for name, t in self.tables.items()
        }
        fk_joins = [(f"{t.name}.{c.name}", c.references) for t in tables.values() for c in t.columns if c.references]
        return Catalog(tables=tables, views={}, layer=EMPTY_LAYER, joins=fk_joins)

    def to_json(self) -> dict[str, Any]:
        return {
            "tables": [asdict(t) for t in self.tables.values()],
            "views": [asdict(v) for v in self.views.values()],
            "joins": [list(j) for j in self.joins],
        }

    def save(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(self.to_json(), indent=1, default=str), encoding="utf-8")

    @classmethod
    def from_json(cls, data: dict[str, Any], layer: SemanticLayer = EMPTY_LAYER) -> Catalog:
        def table(raw: dict[str, Any]) -> TableInfo:
            columns = [ColumnInfo(**c) for c in raw.pop("columns")]
            return TableInfo(columns=columns, **raw)

        tables = {t["name"]: table(dict(t)) for t in data["tables"]}
        views = {v["name"]: table(dict(v)) for v in data.get("views", [])}
        joins = [(a, b) for a, b in data.get("joins", [])]
        return cls(tables=tables, views=views, layer=layer, joins=joins)


def introspect(
    conn: psycopg.Connection,
    *,
    schema: str,
    reader_role: str,
    layer: SemanticLayer = EMPTY_LAYER,
    view_columns: dict[str, list[tuple[str, str]]] | None = None,
    sample_values: bool = True,
) -> Catalog:
    """Read the data schema with the owner connection. Only tables the reader role may SELECT from are included,
    and columns the reader may not select are marked PII (the database's grants are the source of truth)."""
    rows = conn.execute(
        """
        SELECT c.table_name, c.column_name, c.data_type, c.is_nullable = 'YES',
               has_column_privilege(%s, quote_ident(c.table_schema) || '.' || quote_ident(c.table_name),
                                    c.column_name, 'SELECT')
        FROM information_schema.columns c
        JOIN information_schema.tables t ON t.table_schema = c.table_schema AND t.table_name = c.table_name
        WHERE c.table_schema = %s AND t.table_type = 'BASE TABLE'
        ORDER BY c.table_name, c.ordinal_position
        """,
        (reader_role, schema),
    ).fetchall()
    keys = conn.execute(
        """
        SELECT tc.table_name, kcu.column_name, tc.constraint_type, ccu.table_name, ccu.column_name
        FROM information_schema.table_constraints tc
        JOIN information_schema.key_column_usage kcu
          ON kcu.constraint_name = tc.constraint_name AND kcu.table_schema = tc.table_schema
        LEFT JOIN information_schema.constraint_column_usage ccu
          ON ccu.constraint_name = tc.constraint_name AND ccu.table_schema = tc.table_schema
         AND tc.constraint_type = 'FOREIGN KEY'
        WHERE tc.table_schema = %s AND tc.constraint_type IN ('PRIMARY KEY', 'FOREIGN KEY')
        """,
        (schema,),
    ).fetchall()
    primary: set[tuple[str, str]] = set()
    foreign: dict[tuple[str, str], str] = {}
    for table, column, kind, ref_table, ref_column in keys:
        if kind == "PRIMARY KEY":
            primary.add((table, column))
        elif ref_table:
            foreign[(table, column)] = f"{ref_table}.{ref_column}"
    for left, right in layer.joins:
        lt, lc = left.split(".")
        foreign.setdefault((lt, lc), right)

    raw: dict[str, list[ColumnInfo]] = {}
    readable: dict[str, bool] = {}
    for table, column, data_type, nullable, can_select in rows:
        meta = layer.tables.get(table)
        if meta is not None and not meta.allowed:
            continue
        col_meta = meta.columns.get(column) if meta else None
        pii = (not can_select) or bool(col_meta and col_meta.pii)
        readable[table] = readable.get(table, False) or bool(can_select)
        raw.setdefault(table, []).append(
            ColumnInfo(
                name=column,
                type=data_type,
                nullable=bool(nullable),
                primary_key=(table, column) in primary,
                references=foreign.get((table, column)),
                description=col_meta.description if col_meta else "",
                pii=pii,
            )
        )
    counts: dict[str, int] = dict(
        conn.execute(
            "SELECT relname, GREATEST(reltuples, 0)::bigint FROM pg_class c "
            "JOIN pg_namespace n ON n.oid = c.relnamespace WHERE n.nspname = %s AND c.relkind = 'r'",
            (schema,),
        ).fetchall()
    )
    tables: dict[str, TableInfo] = {}
    for table, columns in raw.items():
        if not readable.get(table):
            continue  # the reader cannot see this table at all
        meta = layer.tables.get(table)
        info = TableInfo(
            name=table,
            columns=columns,
            row_count=int(counts.get(table, 0)),
            description=meta.description if meta else "",
            synonyms=list(meta.synonyms) if meta else [],
        )
        if info.row_count < 1_000_000:
            exact = conn.execute(sql.SQL("SELECT count(*) FROM {}").format(sql.Identifier(schema, table))).fetchone()
            info.row_count = int(exact[0]) if exact else 0
        if sample_values:
            _add_samples(conn, schema, info, layer)
        tables[table] = info

    views: dict[str, TableInfo] = {}
    for name, view in layer.semantic_views.items():
        typed = dict(view_columns.get(name, [])) if view_columns else {}
        names = list(typed) or list(view.columns)
        views[name] = TableInfo(
            name=name,
            columns=[ColumnInfo(name=c, type=typed.get(c, ""), description=view.columns.get(c, "")) for c in names],
            description=view.description,
            synonyms=list(view.synonyms),
            is_view=True,
            sql=view.sql.strip(),
        )
    joins = [(f"{t}.{c}", ref) for (t, c), ref in foreign.items() if t in tables and ref.split(".")[0] in tables]
    return Catalog(tables=tables, views=views, layer=layer, joins=sorted(set(joins)))


def _add_samples(conn: psycopg.Connection, schema: str, info: TableInfo, layer: SemanticLayer) -> None:
    meta = layer.tables.get(info.name)
    for column in info.columns:
        col_meta = meta.columns.get(column.name) if meta else None
        if column.pii or column.primary_key or column.references or (col_meta and not col_meta.sample):
            continue
        if column.type not in SAMPLE_TYPES:
            continue
        query = sql.SQL(
            "SELECT {col}::text, count(*) FROM {table} WHERE {col} IS NOT NULL GROUP BY 1 ORDER BY 2 DESC, 1 LIMIT {n}"
        ).format(col=sql.Identifier(column.name), table=sql.Identifier(schema, info.name), n=MAX_SAMPLE_DISTINCT + 1)
        values = conn.execute(query).fetchall()
        if 0 < len(values) <= MAX_SAMPLE_DISTINCT:
            column.samples = [str(v[0]) for v in values]


def view_column_types(conn: psycopg.Connection, layer: SemanticLayer) -> dict[str, list[tuple[str, str]]]:
    """Output column names and types of each semantic view, from a LIMIT 0 run (as the reader, in a read-only
    transaction, so a broken view definition fails here rather than in front of a user)."""
    result: dict[str, list[tuple[str, str]]] = {}
    for name, view in layer.semantic_views.items():
        with conn.transaction(), conn.cursor() as cur:
            cur.execute("SET TRANSACTION READ ONLY")
            cur.execute("SELECT set_config('tally.region_scope', '*', true)")
            cur.execute(sql.SQL("SELECT * FROM ({}) AS v LIMIT 0").format(sql.SQL(view.sql)))
            columns: list[tuple[str, str]] = []
            for desc in cur.description or []:
                type_info = conn.adapters.types.get(desc.type_code)
                columns.append((desc.name, _pretty_type(type_info.name if type_info else "unknown")))
            result[name] = columns
    return result


def _pretty_type(name: str) -> str:
    return {
        "int4": "integer",
        "int8": "bigint",
        "int2": "smallint",
        "numeric": "numeric",
        "timestamptz": "timestamp with time zone",
        "bool": "boolean",
        "bpchar": "character",
        "varchar": "character varying",
    }.get(name, name)
