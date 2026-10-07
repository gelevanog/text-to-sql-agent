"""Conversation memory, the audit log and the saved-questions library, in Tally's own schema (owner connection; the
reader role has no access to it). An in-memory store with the same interface serves tests and the evaluation.

The audit log records the question, the SQL, the status, the row count, the duration, the model, the number of
model calls and corrections, and violation codes. Never result data.
"""

from __future__ import annotations

import datetime as dt
import threading
import uuid
from dataclasses import asdict, dataclass, field
from typing import Any, Protocol

from psycopg import sql
from psycopg.types.json import Jsonb

from tally.db import Database


def _now() -> dt.datetime:
    return dt.datetime.now(dt.UTC)


@dataclass
class TurnRecord:
    conversation_id: str
    question: str
    status: str
    sql: str = ""
    explanation: str = ""
    answer: str = ""
    columns: list[str] = field(default_factory=list)
    preview: list[list[Any]] = field(default_factory=list)
    row_count: int = 0
    pending_clarification: str | None = None
    resolved: dict[str, str] = field(default_factory=dict)
    views: list[str] = field(default_factory=list)
    tables: list[str] = field(default_factory=list)
    created_at: dt.datetime = field(default_factory=_now)
    id: str = field(default_factory=lambda: uuid.uuid4().hex)

    def to_dict(self) -> dict[str, Any]:
        data = asdict(self)
        data["created_at"] = self.created_at.isoformat()
        return data


@dataclass
class AuditRecord:
    question: str
    status: str
    sql: str = ""
    row_count: int = 0
    duration_ms: int = 0
    model: str = ""
    llm_calls: int = 0
    corrections: int = 0
    violations: list[str] = field(default_factory=list)
    region_scope: str = "*"
    conversation_id: str | None = None
    ts: dt.datetime = field(default_factory=_now)
    id: int = 0

    def to_dict(self) -> dict[str, Any]:
        data = asdict(self)
        data["ts"] = self.ts.isoformat()
        return data


@dataclass
class SavedQuestion:
    question: str
    description: str = ""
    category: str = ""
    id: int = 0

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


class Store(Protocol):
    def new_conversation(self, title: str = "") -> str: ...
    def turns(self, conversation_id: str) -> list[TurnRecord]: ...
    def add_turn(self, turn: TurnRecord) -> None: ...
    def conversations(self, limit: int = 50) -> list[dict[str, Any]]: ...
    def audit(self, record: AuditRecord) -> None: ...
    def audit_log(self, limit: int = 200) -> list[AuditRecord]: ...
    def saved(self) -> list[SavedQuestion]: ...
    def add_saved(self, question: SavedQuestion) -> SavedQuestion: ...
    def delete_saved(self, saved_id: int) -> bool: ...


class InMemoryStore:
    def __init__(self) -> None:
        self._conversations: dict[str, dict[str, Any]] = {}
        self._turns: dict[str, list[TurnRecord]] = {}
        self._audit: list[AuditRecord] = []
        self._saved: list[SavedQuestion] = []
        self._lock = threading.Lock()

    def new_conversation(self, title: str = "") -> str:
        conversation_id = uuid.uuid4().hex
        with self._lock:
            self._conversations[conversation_id] = {"id": conversation_id, "title": title, "created_at": _now()}
            self._turns[conversation_id] = []
        return conversation_id

    def turns(self, conversation_id: str) -> list[TurnRecord]:
        return list(self._turns.get(conversation_id, []))

    def add_turn(self, turn: TurnRecord) -> None:
        with self._lock:
            if turn.conversation_id not in self._conversations:
                self._conversations[turn.conversation_id] = {
                    "id": turn.conversation_id,
                    "title": turn.question,
                    "created_at": _now(),
                }
            if not self._conversations[turn.conversation_id]["title"]:
                self._conversations[turn.conversation_id]["title"] = turn.question
            self._turns.setdefault(turn.conversation_id, []).append(turn)

    def conversations(self, limit: int = 50) -> list[dict[str, Any]]:
        items = sorted(self._conversations.values(), key=lambda c: c["created_at"], reverse=True)[:limit]
        return [
            {**c, "created_at": c["created_at"].isoformat(), "turns": len(self._turns.get(c["id"], []))} for c in items
        ]

    def audit(self, record: AuditRecord) -> None:
        with self._lock:
            record.id = len(self._audit) + 1
            self._audit.append(record)

    def audit_log(self, limit: int = 200) -> list[AuditRecord]:
        return list(reversed(self._audit))[:limit]

    def saved(self) -> list[SavedQuestion]:
        return list(self._saved)

    def add_saved(self, question: SavedQuestion) -> SavedQuestion:
        with self._lock:
            existing = next((s for s in self._saved if s.question == question.question), None)
            if existing:
                return existing
            question.id = max((s.id for s in self._saved), default=0) + 1
            self._saved.append(question)
        return question

    def delete_saved(self, saved_id: int) -> bool:
        with self._lock:
            before = len(self._saved)
            self._saved = [s for s in self._saved if s.id != saved_id]
            return len(self._saved) < before


DDL = """
CREATE SCHEMA IF NOT EXISTS {schema};
CREATE TABLE IF NOT EXISTS {schema}.conversations (
    id          text PRIMARY KEY,
    title       text NOT NULL DEFAULT '',
    created_at  timestamptz NOT NULL DEFAULT now()
);
CREATE TABLE IF NOT EXISTS {schema}.turns (
    id                     text PRIMARY KEY,
    conversation_id        text NOT NULL REFERENCES {schema}.conversations (id) ON DELETE CASCADE,
    question               text NOT NULL,
    status                 text NOT NULL,
    sql                    text NOT NULL DEFAULT '',
    explanation            text NOT NULL DEFAULT '',
    answer                 text NOT NULL DEFAULT '',
    columns                jsonb NOT NULL DEFAULT '[]',
    preview                jsonb NOT NULL DEFAULT '[]',
    row_count              integer NOT NULL DEFAULT 0,
    pending_clarification  text,
    resolved               jsonb NOT NULL DEFAULT '{{}}',
    views                  jsonb NOT NULL DEFAULT '[]',
    tables                 jsonb NOT NULL DEFAULT '[]',
    created_at             timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS turns_conversation_idx ON {schema}.turns (conversation_id, created_at);
CREATE TABLE IF NOT EXISTS {schema}.audit_log (
    id               bigserial PRIMARY KEY,
    ts               timestamptz NOT NULL DEFAULT now(),
    conversation_id  text,
    question         text NOT NULL,
    status           text NOT NULL,
    sql              text NOT NULL DEFAULT '',
    row_count        integer NOT NULL DEFAULT 0,
    duration_ms      integer NOT NULL DEFAULT 0,
    model            text NOT NULL DEFAULT '',
    llm_calls        integer NOT NULL DEFAULT 0,
    corrections      integer NOT NULL DEFAULT 0,
    violations       jsonb NOT NULL DEFAULT '[]',
    region_scope     text NOT NULL DEFAULT '*'
);
CREATE TABLE IF NOT EXISTS {schema}.saved_questions (
    id           serial PRIMARY KEY,
    question     text NOT NULL UNIQUE,
    description  text NOT NULL DEFAULT '',
    category     text NOT NULL DEFAULT '',
    created_at   timestamptz NOT NULL DEFAULT now()
);
"""


class PostgresStore:
    def __init__(self, db: Database, schema: str = "tally") -> None:
        self.db = db
        self.schema = schema
        self._s = sql.Identifier(schema)

    def ensure_schema(self) -> None:
        with self.db.owner() as conn:
            conn.execute(sql.SQL(DDL).format(schema=self._s))

    def _t(self, table: str) -> sql.Composed:
        return sql.SQL("{}.{}").format(self._s, sql.Identifier(table))

    def new_conversation(self, title: str = "") -> str:
        conversation_id = uuid.uuid4().hex
        with self.db.owner() as conn:
            conn.execute(
                sql.SQL("INSERT INTO {} (id, title) VALUES (%s, %s)").format(self._t("conversations")),
                (conversation_id, title),
            )
        return conversation_id

    def turns(self, conversation_id: str) -> list[TurnRecord]:
        with self.db.owner() as conn:
            rows = conn.execute(
                sql.SQL(
                    "SELECT id, conversation_id, question, status, sql, explanation, answer, columns, preview,"
                    " row_count, pending_clarification, resolved, views, tables, created_at FROM {}"
                    " WHERE conversation_id = %s ORDER BY created_at"
                ).format(self._t("turns")),
                (conversation_id,),
            ).fetchall()
        return [
            TurnRecord(
                id=r[0],
                conversation_id=r[1],
                question=r[2],
                status=r[3],
                sql=r[4],
                explanation=r[5],
                answer=r[6],
                columns=r[7],
                preview=r[8],
                row_count=r[9],
                pending_clarification=r[10],
                resolved=r[11],
                views=r[12],
                tables=r[13],
                created_at=r[14],
            )
            for r in rows
        ]

    def add_turn(self, turn: TurnRecord) -> None:
        with self.db.owner() as conn, conn.transaction():
            conn.execute(
                sql.SQL(
                    "INSERT INTO {} (id, title) VALUES (%s, %s) ON CONFLICT (id) DO UPDATE SET title = "
                    "CASE WHEN {}.title = '' THEN EXCLUDED.title ELSE {}.title END"
                ).format(self._t("conversations"), self._t("conversations"), self._t("conversations")),
                (turn.conversation_id, turn.question[:200]),
            )
            conn.execute(
                sql.SQL(
                    "INSERT INTO {} (id, conversation_id, question, status, sql, explanation, answer, columns, preview,"
                    " row_count, pending_clarification, resolved, views, tables, created_at)"
                    " VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)"
                ).format(self._t("turns")),
                (
                    turn.id,
                    turn.conversation_id,
                    turn.question,
                    turn.status,
                    turn.sql,
                    turn.explanation,
                    turn.answer,
                    Jsonb(turn.columns),
                    Jsonb(turn.preview),
                    turn.row_count,
                    turn.pending_clarification,
                    Jsonb(turn.resolved),
                    Jsonb(turn.views),
                    Jsonb(turn.tables),
                    turn.created_at,
                ),
            )

    def conversations(self, limit: int = 50) -> list[dict[str, Any]]:
        with self.db.owner() as conn:
            rows = conn.execute(
                sql.SQL(
                    "SELECT c.id, c.title, c.created_at, count(t.id) FROM {} c"
                    " LEFT JOIN {} t ON t.conversation_id = c.id GROUP BY c.id ORDER BY c.created_at DESC LIMIT %s"
                ).format(self._t("conversations"), self._t("turns")),
                (limit,),
            ).fetchall()
        return [{"id": r[0], "title": r[1], "created_at": r[2].isoformat(), "turns": r[3]} for r in rows]

    def audit(self, record: AuditRecord) -> None:
        with self.db.owner() as conn:
            conn.execute(
                sql.SQL(
                    "INSERT INTO {} (ts, conversation_id, question, status, sql, row_count, duration_ms, model,"
                    " llm_calls, corrections, violations, region_scope)"
                    " VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)"
                ).format(self._t("audit_log")),
                (
                    record.ts,
                    record.conversation_id,
                    record.question,
                    record.status,
                    record.sql,
                    record.row_count,
                    record.duration_ms,
                    record.model,
                    record.llm_calls,
                    record.corrections,
                    Jsonb(record.violations),
                    record.region_scope,
                ),
            )

    def audit_log(self, limit: int = 200) -> list[AuditRecord]:
        with self.db.owner() as conn:
            rows = conn.execute(
                sql.SQL(
                    "SELECT id, ts, conversation_id, question, status, sql, row_count, duration_ms, model, llm_calls,"
                    " corrections, violations, region_scope FROM {} ORDER BY id DESC LIMIT %s"
                ).format(self._t("audit_log")),
                (limit,),
            ).fetchall()
        return [
            AuditRecord(
                id=r[0],
                ts=r[1],
                conversation_id=r[2],
                question=r[3],
                status=r[4],
                sql=r[5],
                row_count=r[6],
                duration_ms=r[7],
                model=r[8],
                llm_calls=r[9],
                corrections=r[10],
                violations=r[11],
                region_scope=r[12],
            )
            for r in rows
        ]

    def saved(self) -> list[SavedQuestion]:
        with self.db.owner() as conn:
            rows = conn.execute(
                sql.SQL("SELECT id, question, description, category FROM {} ORDER BY id").format(
                    self._t("saved_questions")
                )
            ).fetchall()
        return [SavedQuestion(id=r[0], question=r[1], description=r[2], category=r[3]) for r in rows]

    def add_saved(self, question: SavedQuestion) -> SavedQuestion:
        with self.db.owner() as conn:
            row = conn.execute(
                sql.SQL(
                    "INSERT INTO {} (question, description, category) VALUES (%s, %s, %s) ON CONFLICT (question)"
                    " DO UPDATE SET description = EXCLUDED.description RETURNING id"
                ).format(self._t("saved_questions")),
                (question.question, question.description, question.category),
            ).fetchone()
        question.id = int(row[0]) if row else 0
        return question

    def delete_saved(self, saved_id: int) -> bool:
        with self.db.owner() as conn:
            cur = conn.execute(sql.SQL("DELETE FROM {} WHERE id = %s").format(self._t("saved_questions")), (saved_id,))
            return cur.rowcount > 0
