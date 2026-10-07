"""The agent loop: clarify -> retrieve -> plan and write SQL -> expand semantic views -> validate -> cost guard ->
execute read-only -> self-correct on errors or empty results (bounded) -> chart -> answer -> number check.

Each stage emits a step event, which the API streams to the browser as server-sent events.
"""

from __future__ import annotations

import datetime as dt
import time
from collections.abc import Callable
from dataclasses import asdict, dataclass, field
from typing import Any

from tally.agent.answer_check import AnswerCheck, check_answer, template_answer
from tally.agent.chart import recommend_chart
from tally.agent.clarify import (
    PendingClarification,
    assumed_defaults,
    clarified_texts,
    match_option,
    pending_ambiguities,
)
from tally.agent.prompts import (
    ANSWER_SYSTEM,
    EMPTY_RESULT_FEEDBACK,
    GENERATE_SYSTEM,
    ModelPlan,
    ReplyParseError,
    TurnContext,
    answer_correction_message,
    answer_user_message,
    clean_answer,
    correction_message,
    generation_user_message,
    parse_plan,
)
from tally.agent.store import AuditRecord, Store, TurnRecord
from tally.llm.base import ChatModel, LLMError, Message
from tally.schema.catalog import Catalog
from tally.schema.retrieval import RetrievedContext, SchemaRetriever
from tally.sql.executor import Executor, QueryError, QueryResult
from tally.sql.expand import expand_views
from tally.sql.validator import SQLValidator, ValidationResult

Emit = Callable[[str, dict[str, Any]], None]
POLICY_CODES = frozenset({"pii_via_star"})
"""Soft violations that still mean "blocked" once the correction budget is spent."""


@dataclass
class AgentConfig:
    clarify_policy: str = "ask"
    max_corrections: int = 2
    retrieval_mode: str = "retrieval"
    semantic_layer: bool = True
    history_turns: int = 3
    answer_retries: int = 1
    max_plan_cost: float = 50_000_000.0
    max_plan_rows: float = 5_000_000.0
    max_tokens: int = 4000
    answer_max_tokens: int = 1500
    temperature: float = 0.0
    today: dt.date = dt.date(2026, 10, 1)
    region_scope: str = "*"
    generate_answer: bool = True


@dataclass
class Step:
    kind: str
    title: str
    detail: dict[str, Any] = field(default_factory=dict)
    ms: float = 0.0


@dataclass
class Attempt:
    sql: str
    stage: str
    """parse | validation | cost | execution | empty | ok | refused | clarify"""
    error: str = ""
    expanded_sql: str = ""
    executed_sql: str = ""
    validation: dict[str, Any] | None = None
    plan_cost: float | None = None
    row_count: int | None = None


@dataclass
class AgentResult:
    conversation_id: str
    question: str
    status: str = "error"
    """answered | clarification | blocked | refused | error"""
    asked: str = ""
    """The question as the user typed it (differs from `question` when it answered a clarification)."""
    turn_id: str = ""
    sql: str = ""
    expanded_sql: str = ""
    executed_sql: str = ""
    views: list[str] = field(default_factory=list)
    plan: str = ""
    explanation: str = ""
    assumptions: list[str] = field(default_factory=list)
    columns: list[str] = field(default_factory=list)
    column_types: list[str] = field(default_factory=list)
    rows: list[list[Any]] = field(default_factory=list)
    row_count: int = 0
    truncated: bool = False
    chart: dict[str, Any] | None = None
    answer: str = ""
    answer_check: dict[str, Any] | None = None
    answer_source: str = ""
    clarification: dict[str, Any] | None = None
    blocked: dict[str, Any] | None = None
    error: str = ""
    retrieval: dict[str, Any] = field(default_factory=dict)
    validation: dict[str, Any] | None = None
    plan_cost: float | None = None
    attempts: list[Attempt] = field(default_factory=list)
    steps: list[Step] = field(default_factory=list)
    resolved: dict[str, str] = field(default_factory=dict)
    llm_calls: int = 0
    corrections: int = 0
    model: str = ""
    served_models: list[str] = field(default_factory=list)
    total_ms: float = 0.0
    llm_ms: float = 0.0
    db_ms: float = 0.0

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


class Agent:
    def __init__(
        self,
        *,
        catalog: Catalog,
        retriever: SchemaRetriever,
        validator: SQLValidator,
        executor: Executor,
        llm: ChatModel,
        store: Store,
        config: AgentConfig,
    ) -> None:
        self.catalog = catalog
        self.retriever = retriever
        self.validator = validator
        self.executor = executor
        self.llm = llm
        self.store = store
        self.config = config
        self.views = {name: view.sql for name, view in catalog.views.items()} if config.semantic_layer else {}

    # ---- public ---------------------------------------------------------------------------------------------
    def run(
        self,
        question: str,
        *,
        conversation_id: str | None = None,
        clarification: dict[str, str] | None = None,
        emit: Emit | None = None,
    ) -> AgentResult:
        started = time.monotonic()
        cid = conversation_id or self.store.new_conversation(question[:200])
        turns = self.store.turns(cid)
        result = AgentResult(
            conversation_id=cid, question=question.strip(), asked=question.strip(), model=self.llm.label
        )
        sink = _Sink(result, emit)
        resolved: dict[str, str] = {}
        for turn in turns:
            resolved.update(turn.resolved)
        self._resolve_reply(result, turns, clarification, resolved)
        answered = [t for t in turns if t.status == "answered"]
        try:
            self._run(result, sink, answered, resolved)
        except LLMError as exc:
            result.status = "error"
            result.error = f"model error: {exc}"
            sink.step("error", "The model request failed", {"error": str(exc)[:300]})
        result.resolved = resolved
        result.total_ms = round((time.monotonic() - started) * 1000, 1)
        self._persist(result)
        sink.emit("done", result.to_dict())
        return result

    # ---- stages ---------------------------------------------------------------------------------------------
    def _resolve_reply(
        self,
        result: AgentResult,
        turns: list[TurnRecord],
        clarification: dict[str, str] | None,
        resolved: dict[str, str],
    ) -> None:
        """If the previous turn asked a clarifying question, an option pick (or a reply naming one) answers it and
        the original question runs again with that choice."""
        if not turns or turns[-1].status != "clarification" or not turns[-1].pending_clarification:
            if clarification and clarification.get("id") and clarification.get("value"):
                resolved[clarification["id"]] = clarification["value"]
            return
        last = turns[-1]
        pending_id = last.pending_clarification or ""
        ambiguity = self.catalog.layer.ambiguity(pending_id)
        if ambiguity is None:
            return
        value = None
        if clarification and clarification.get("id") == pending_id:
            value = clarification.get("value")
        if value is None:
            option = match_option(result.question, ambiguity)
            value = option.value if option else None
        if value and ambiguity.option(value):
            resolved[pending_id] = value
            result.question = last.question

    def _run(self, result: AgentResult, sink: _Sink, answered: list[TurnRecord], resolved: dict[str, str]) -> None:
        cfg = self.config
        layer = self.catalog.layer
        history_texts = [t.question for t in answered]

        if cfg.semantic_layer and layer.ambiguities:
            pending = pending_ambiguities(result.question, layer, history=history_texts, resolved=resolved)
            if pending and cfg.clarify_policy == "ask":
                clarify = PendingClarification(pending[0])
                result.status = "clarification"
                result.clarification = clarify.to_dict()
                sink.step("clarification", clarify.ambiguity.question, result.clarification)
                return
            if pending:
                chosen, notes = assumed_defaults(pending)
                resolved.update(chosen)
                result.assumptions.extend(notes)

        # Retrieval: the question, plus the previous question for follow-ups ("now by month").
        t0 = time.monotonic()
        query = result.question
        extra: list[str] = []
        if answered:
            query = f"{result.question} {answered[-1].question}"
            extra = [*answered[-1].views, *answered[-1].tables]
        context = self.retriever.retrieve(query, extra_tables=extra, full=cfg.retrieval_mode == "full")
        result.retrieval = context.summary()
        sink.step("retrieval", self._retrieval_title(context), result.retrieval, t0)

        history = [
            TurnContext(
                question=t.question,
                sql=t.sql,
                columns=t.columns,
                preview=t.preview,
                row_count=t.row_count,
                answer=t.answer,
            )
            for t in answered[-cfg.history_turns :]
        ]
        messages: list[Message] = [
            {"role": "system", "content": GENERATE_SYSTEM},
            {
                "role": "user",
                "content": generation_user_message(
                    question=result.question,
                    schema_text=context.text,
                    today=cfg.today,
                    first_date=layer.reporting.first_data_date if cfg.semantic_layer else None,
                    last_date=layer.reporting.last_data_date if cfg.semantic_layer else None,
                    history=history,
                    clarifications=[*clarified_texts(layer, resolved), *result.assumptions],
                ),
            },
        ]
        query_result = self._generate_and_run(result, sink, messages)
        if query_result is None:
            return
        chart = recommend_chart(result.columns, result.column_types, result.rows)
        result.chart = chart.to_dict()
        sink.step("chart", f"Chart: {chart.type}", {"type": chart.type, "reason": chart.reason})
        if cfg.generate_answer:
            self._answer(result, sink, query_result)
        result.status = "answered"

    def _generate_and_run(self, result: AgentResult, sink: _Sink, messages: list[Message]) -> QueryResult | None:
        cfg = self.config
        empty_retry_used = False
        last_error = ""
        for attempt_no in range(cfg.max_corrections + 1):
            if attempt_no:
                result.corrections += 1
            t0 = time.monotonic()
            reply = self._call(result, messages, cfg.max_tokens)
            messages.append({"role": "assistant", "content": reply})
            try:
                plan = parse_plan(reply)
            except ReplyParseError as exc:
                last_error = f"the reply could not be parsed ({exc})"
                result.attempts.append(Attempt(sql="", stage="parse", error=str(exc)))
                sink.step("correction", "The model's reply was not valid JSON; asking again", {"error": str(exc)}, t0)
                messages.append({"role": "user", "content": correction_message(f"{exc}. Reply with the JSON object.")})
                continue
            if plan.action == "refuse":
                result.status = "refused"
                result.attempts.append(Attempt(sql="", stage="refused", error=plan.refusal))
                result.blocked = {"layer": "model", "reasons": [plan.refusal or "the model declined the request"]}
                sink.step("blocked", "The model declined the request", result.blocked, t0)
                return None
            if plan.action == "clarify":
                result.status = "clarification"
                result.attempts.append(Attempt(sql="", stage="clarify"))
                result.clarification = {
                    "id": None,
                    "question": plan.clarification_question or "Could you clarify the question?",
                    "options": [{"value": o, "label": o} for o in plan.clarification_options],
                    "source": "model",
                }
                sink.step("clarification", result.clarification["question"], result.clarification, t0)
                return None
            self._record_plan(result, plan)
            sink.step(
                "sql",
                "SQL written" if attempt_no == 0 else f"SQL corrected (attempt {attempt_no + 1})",
                {"sql": plan.sql, "plan": plan.plan, "explanation": plan.explanation},
                t0,
            )
            outcome = self._check_and_execute(result, sink, plan, attempt_no, empty_retry_used)
            if isinstance(outcome, QueryResult):
                return outcome
            if outcome is None:
                return None  # blocked
            feedback, empty = outcome
            empty_retry_used = empty_retry_used or empty
            last_error = feedback
            messages.append({"role": "user", "content": correction_message(feedback)})
        last = result.attempts[-1] if result.attempts else None
        policy = [v for v in ((last.validation or {}).get("violations", []) if last else []) if v.get("code") in
                  POLICY_CODES]  # fmt: skip
        if policy:
            # The model kept asking for personal data: report it as blocked, not as a failure.
            self._block(result, sink, "validator", [str(v["message"]) for v in policy], result.sql)
            return None
        result.status = "error"
        result.error = f"no working query after {cfg.max_corrections + 1} attempts: {last_error}"
        sink.step("error", "Gave up after the correction budget", {"error": last_error})
        return None

    def _record_plan(self, result: AgentResult, plan: ModelPlan) -> None:
        result.sql = plan.sql
        result.plan = plan.plan
        result.explanation = plan.explanation
        for assumption in plan.assumptions:
            if assumption not in result.assumptions:
                result.assumptions.append(assumption)

    def _check_and_execute(
        self, result: AgentResult, sink: _Sink, plan: ModelPlan, attempt_no: int, empty_retry_used: bool
    ) -> QueryResult | tuple[str, bool] | None:
        """A QueryResult on success, (feedback, was_empty) to retry, or None when the request is blocked."""
        cfg = self.config
        t0 = time.monotonic()
        expansion = expand_views(plan.sql, self.views)
        result.views = expansion.views
        result.expanded_sql = expansion.sql
        validation = self.validator.validate(expansion.sql)
        result.validation = validation.to_dict()
        attempt = Attempt(sql=plan.sql, stage="validation", expanded_sql=expansion.sql, validation=result.validation)
        result.attempts.append(attempt)
        sink.step(
            "validation",
            "Validation passed" if validation.ok else "Validation failed",
            self._validation_detail(validation, expansion.views),
            t0,
        )
        if not validation.ok:
            attempt.error = validation.feedback()
            if validation.hard:
                self._block(result, sink, "validator", [v.message for v in validation.violations], plan.sql)
                return None
            return validation.feedback(), False
        result.executed_sql = validation.sql
        attempt.executed_sql = validation.sql

        t1 = time.monotonic()
        try:
            cost = self.executor.explain(validation.sql)
        except QueryError as exc:
            result.db_ms += (time.monotonic() - t1) * 1000
            attempt.stage, attempt.error = "execution", str(exc)
            sink.step("execution", "The database rejected the query", {"error": str(exc)}, t1)
            if exc.permission:
                self._block(result, sink, "database", [str(exc)], plan.sql)
                return None
            return f"PostgreSQL error: {exc}", False
        result.db_ms += (time.monotonic() - t1) * 1000
        result.plan_cost = cost.total_cost
        attempt.plan_cost = cost.total_cost
        too_costly = cost.total_cost > cfg.max_plan_cost or cost.plan_rows > cfg.max_plan_rows
        sink.step(
            "cost",
            "Cost check passed" if not too_costly else "Query too expensive",
            {
                "total_cost": cost.total_cost,
                "plan_rows": cost.plan_rows,
                "max_cost": cfg.max_plan_cost,
                "max_rows": cfg.max_plan_rows,
            },
            t1,
        )
        if too_costly:
            attempt.stage = "cost"
            if cost.plan_rows > cfg.max_plan_rows:
                attempt.error = (
                    f"a step of the plan is estimated at {cost.plan_rows:,.0f} rows, above the limit of "
                    f"{cfg.max_plan_rows:,.0f}"
                )
            else:
                attempt.error = f"estimated cost {cost.total_cost:,.0f} is above the limit of {cfg.max_plan_cost:,.0f}"
            if attempt_no >= cfg.max_corrections:
                self._block(result, sink, "cost_guard", [attempt.error], plan.sql)
                return None
            return (
                f"the query is too expensive ({attempt.error}). Aggregate earlier, filter by date and avoid "
                "cross joins."
            ), False

        t2 = time.monotonic()
        try:
            query_result = self.executor.execute(validation.sql)
        except QueryError as exc:
            result.db_ms += (time.monotonic() - t2) * 1000
            attempt.stage, attempt.error = "execution", str(exc)
            sink.step("execution", "The query failed", {"error": str(exc)}, t2)
            if exc.permission:
                self._block(result, sink, "database", [str(exc)], plan.sql)
                return None
            return f"PostgreSQL error: {exc}", False
        result.db_ms += (time.monotonic() - t2) * 1000
        attempt.row_count = query_result.row_count
        result.columns = [c.name for c in query_result.columns]
        result.column_types = [c.type for c in query_result.columns]
        result.rows = query_result.json_rows()
        result.row_count = query_result.row_count
        result.truncated = query_result.truncated
        sink.step(
            "execution",
            f"{query_result.row_count} rows in {query_result.duration_ms:.0f} ms",
            {
                "row_count": query_result.row_count,
                "truncated": query_result.truncated,
                "duration_ms": query_result.duration_ms,
            },
            t2,
        )
        if query_result.row_count == 0 and not empty_retry_used and attempt_no < cfg.max_corrections:
            attempt.stage = "empty"
            return EMPTY_RESULT_FEEDBACK, True
        attempt.stage = "ok"
        sink.step(
            "result",
            "Result",
            {
                "columns": result.columns,
                "rows": result.rows[:200],
                "row_count": result.row_count,
                "truncated": result.truncated,
            },
        )
        return query_result

    def _answer(self, result: AgentResult, sink: _Sink, query_result: QueryResult) -> None:
        t0 = time.monotonic()
        rows = result.rows
        messages: list[Message] = [
            {"role": "system", "content": ANSWER_SYSTEM},
            {
                "role": "user",
                "content": answer_user_message(
                    question=result.question,
                    explanation=result.explanation,
                    columns=result.columns,
                    rows=rows,
                    row_count=result.row_count,
                    truncated=result.truncated,
                ),
            },
        ]
        check: AnswerCheck | None = None
        answer = ""
        for attempt in range(self.config.answer_retries + 1):
            try:
                answer = clean_answer(self._call(result, messages, self.config.answer_max_tokens))
            except LLMError as exc:
                answer, check = "", None
                sink.step("answer", "The model could not write the answer", {"error": str(exc)[:200]})
                break
            check = check_answer(answer, rows, question=result.question, row_count=result.row_count)
            if check.ok:
                result.answer_source = "model" if attempt == 0 else "model_retry"
                break
            messages.append({"role": "assistant", "content": answer})
            messages.append({"role": "user", "content": answer_correction_message(check.unsupported)})
        if check is None or not check.ok:
            first_check = check
            answer = template_answer(result.columns, rows, result.row_count, result.truncated)
            result.answer_source = "template"
            check = check_answer(answer, rows, question=result.question, row_count=result.row_count)
            if first_check is not None:
                check.unsupported = first_check.unsupported
        result.answer = answer
        result.answer_check = check.to_dict()
        sink.step(
            "answer",
            "Answer checked against the result"
            if result.answer_source != "template"
            else "Answer built from the result (the model's answer cited other numbers)",
            {"answer": answer, "check": result.answer_check, "source": result.answer_source},
            t0,
        )

    # ---- helpers ----------------------------------------------------------------------------------------------
    def _call(self, result: AgentResult, messages: list[Message], max_tokens: int) -> str:
        t0 = time.monotonic()
        result.llm_calls += 1
        try:
            completion = self.llm.complete(messages, max_tokens=max_tokens, temperature=self.config.temperature)
        finally:
            result.llm_ms += (time.monotonic() - t0) * 1000
        if completion.model and completion.model not in result.served_models:
            result.served_models.append(completion.model)
        return completion.text

    def _block(self, result: AgentResult, sink: _Sink, layer: str, reasons: list[str], sql: str) -> None:
        result.status = "blocked"
        result.blocked = {"layer": layer, "reasons": reasons, "sql": sql}
        sink.step("blocked", f"Blocked by the {layer.replace('_', ' ')}", result.blocked)

    @staticmethod
    def _validation_detail(validation: ValidationResult, views: list[str]) -> dict[str, Any]:
        detail = validation.to_dict()
        detail["views_expanded"] = views
        return detail

    @staticmethod
    def _retrieval_title(context: RetrievedContext) -> str:
        if context.mode == "full":
            return f"Full schema ({len(context.tables)} tables, {len(context.views)} views)"
        names = [*context.views, *context.tables]
        return "Found " + ", ".join(names[:6]) + (" ..." if len(names) > 6 else "")

    def _persist(self, result: AgentResult) -> None:
        pending = None
        if result.status == "clarification" and result.clarification and result.clarification.get("id"):
            pending = str(result.clarification["id"])
        turn = TurnRecord(
            conversation_id=result.conversation_id,
            question=result.question if result.status != "clarification" or pending else result.asked,
            status=result.status,
            sql=result.sql,
            explanation=result.explanation,
            answer=result.answer or (result.clarification or {}).get("question", ""),
            columns=result.columns,
            preview=result.rows[:10],
            row_count=result.row_count,
            pending_clarification=pending,
            resolved=dict(result.resolved),
            views=result.views,
            tables=[t for t in result.retrieval.get("tables", []) if isinstance(t, str)][:3],
        )
        self.store.add_turn(turn)
        result.turn_id = turn.id
        violations = [str(v.get("code")) for v in (result.validation or {}).get("violations", [])]
        if result.blocked and result.blocked.get("layer") in {"cost_guard", "database", "model"}:
            violations.append(str(result.blocked["layer"]))
        self.store.audit(
            AuditRecord(
                question=result.asked,
                status=result.status,
                sql=result.executed_sql or result.sql,
                row_count=result.row_count,
                duration_ms=int(result.total_ms),
                model=", ".join(result.served_models) or result.model,
                llm_calls=result.llm_calls,
                corrections=result.corrections,
                violations=violations,
                region_scope=self.config.region_scope,
                conversation_id=result.conversation_id,
            )
        )


class _Sink:
    def __init__(self, result: AgentResult, emit: Emit | None) -> None:
        self.result = result
        self._emit = emit

    def step(self, kind: str, title: str, detail: dict[str, Any] | None = None, started: float | None = None) -> None:
        ms = round((time.monotonic() - started) * 1000, 1) if started is not None else 0.0
        step = Step(kind=kind, title=title, detail=detail or {}, ms=ms)
        self.result.steps.append(step)
        self.emit("step", asdict(step))

    def emit(self, event: str, data: dict[str, Any]) -> None:
        if self._emit is not None:
            self._emit(event, data)
