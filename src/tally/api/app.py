"""FastAPI application: ask (JSON and server-sent events), conversations, saved questions, schema and semantic layer,
validator, audit log and evaluation results."""

from __future__ import annotations

import asyncio
import json
import threading
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Annotated, Any

from fastapi import Depends, FastAPI, HTTPException, Query, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field

from tally.agent.store import SavedQuestion
from tally.api.evaluation import load_results
from tally.config import Settings, get_settings
from tally.logging_config import configure_logging, get_logger
from tally.services import Services, build_services
from tally.sql.expand import expand_views

log = get_logger(__name__)


class Clarification(BaseModel):
    id: str = Field(max_length=100)
    value: str = Field(max_length=200)


class AskRequest(BaseModel):
    question: str = Field(min_length=1, max_length=2000)
    conversation_id: str | None = Field(default=None, max_length=64)
    clarification: Clarification | None = None


class SavedIn(BaseModel):
    question: str = Field(min_length=1, max_length=2000)
    description: str = Field(default="", max_length=500)
    category: str = Field(default="", max_length=100)


class ValidateRequest(BaseModel):
    sql: str = Field(min_length=1, max_length=20000)


def get_services(request: Request) -> Services:
    services: Services | None = request.app.state.services
    if services is None:
        raise HTTPException(503, "starting up")
    return services


Svc = Annotated[Services, Depends(get_services)]


def _sse(event: str, data: Any) -> str:
    return f"event: {event}\ndata: {json.dumps(data, default=str)}\n\n"


def create_app(services: Services | None = None, settings: Settings | None = None) -> FastAPI:
    settings = settings or (services.settings if services else get_settings())

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        if getattr(app.state, "services", None) is None:
            configure_logging(settings.log_level, settings.log_format)
            app.state.services = await asyncio.to_thread(build_services, settings, tag="api")
        yield
        if app.state.services is not None:
            app.state.services.close()

    app = FastAPI(
        title="Tally",
        version="0.1.0",
        description="Ask your database questions in plain English and get checked SQL, a chart and an answer.",
        lifespan=lifespan,
    )
    app.state.services = services
    app.add_middleware(
        CORSMiddleware,
        allow_origins=settings.cors_origins,
        allow_methods=["GET", "POST", "DELETE"],
        allow_headers=["Content-Type"],
    )

    @app.get("/health")
    def health(svc: Svc) -> dict[str, Any]:
        s = svc.settings
        return {
            "status": "ok",
            "model": svc.llm.label,
            "provider": s.llm_provider,
            "free_only": s.require_free_models,
            "semantic_layer": s.semantic_layer,
            "retrieval_mode": s.retrieval_mode,
            "clarify_policy": s.clarify_policy,
            "region_scope": s.region_scope,
            "today": s.effective_today.isoformat(),
            "tables": len(svc.catalog.tables),
            "views": len(svc.catalog.views),
            "company": svc.layer.company,
        }

    # ---- asking ---------------------------------------------------------------------------------------------
    @app.post("/api/ask")
    def ask(body: AskRequest, svc: Svc) -> dict[str, Any]:
        clarification = body.clarification.model_dump() if body.clarification else None
        result = svc.agent.run(body.question, conversation_id=body.conversation_id, clarification=clarification)
        return result.to_dict()

    @app.post("/api/ask/stream")
    async def ask_stream(body: AskRequest, svc: Svc) -> StreamingResponse:
        loop = asyncio.get_running_loop()
        queue: asyncio.Queue[tuple[str, Any] | None] = asyncio.Queue()
        clarification = body.clarification.model_dump() if body.clarification else None

        def emit(event: str, data: dict[str, Any]) -> None:
            loop.call_soon_threadsafe(queue.put_nowait, (event, data))

        def work() -> None:
            try:
                svc.agent.run(body.question, conversation_id=body.conversation_id, clarification=clarification,
                              emit=emit)  # fmt: skip
            except Exception as exc:  # report, never hang the stream
                log.exception("ask.failed")
                loop.call_soon_threadsafe(queue.put_nowait, ("error", {"error": type(exc).__name__}))
            finally:
                loop.call_soon_threadsafe(queue.put_nowait, None)

        threading.Thread(target=work, daemon=True).start()

        async def events() -> AsyncIterator[str]:
            while True:
                item = await queue.get()
                if item is None:
                    break
                yield _sse(*item)

        return StreamingResponse(
            events(), media_type="text/event-stream", headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"}
        )

    @app.get("/api/conversations")
    def conversations(svc: Svc, limit: Annotated[int, Query(ge=1, le=200)] = 50) -> list[dict[str, Any]]:
        return svc.store.conversations(limit)

    @app.get("/api/conversations/{conversation_id}")
    def conversation(conversation_id: str, svc: Svc) -> dict[str, Any]:
        turns = svc.store.turns(conversation_id)
        if not turns:
            raise HTTPException(404, "conversation not found")
        return {"id": conversation_id, "turns": [t.to_dict() for t in turns]}

    # ---- saved questions --------------------------------------------------------------------------------------
    @app.get("/api/saved")
    def saved(svc: Svc) -> list[dict[str, Any]]:
        return [s.to_dict() for s in svc.store.saved()]

    @app.post("/api/saved", status_code=201)
    def add_saved(body: SavedIn, svc: Svc) -> dict[str, Any]:
        item = svc.store.add_saved(SavedQuestion(question=body.question, description=body.description,
                                                 category=body.category))  # fmt: skip
        return item.to_dict()

    @app.delete("/api/saved/{saved_id}", status_code=204)
    def delete_saved(saved_id: int, svc: Svc) -> None:
        if not svc.store.delete_saved(saved_id):
            raise HTTPException(404, "saved question not found")

    # ---- schema, validator, audit, evaluation ---------------------------------------------------------------
    @app.get("/api/schema")
    def schema(svc: Svc) -> dict[str, Any]:
        layer = svc.layer
        data = svc.catalog.to_json()
        for table in data["tables"]:
            for column in table["columns"]:
                if column["pii"]:
                    column["samples"] = []
        return {
            "company": layer.company,
            "description": layer.description,
            "reporting": layer.reporting.model_dump(),
            "rules": [" ".join(r.split()) for r in layer.rules],
            "synonyms": layer.synonyms,
            "metrics": {name: m.model_dump() for name, m in layer.metrics.items()},
            "ambiguities": [a.model_dump() for a in layer.ambiguities],
            **data,
        }

    @app.post("/api/validate")
    def validate(body: ValidateRequest, svc: Svc) -> dict[str, Any]:
        """Static validation only: nothing is executed."""
        views = {n: v.sql for n, v in svc.catalog.views.items()} if svc.settings.semantic_layer else {}
        expansion = expand_views(body.sql, views)
        result = svc.validator.validate(expansion.sql)
        return {**result.to_dict(), "views_expanded": expansion.views}

    @app.get("/api/audit")
    def audit(svc: Svc, limit: Annotated[int, Query(ge=1, le=1000)] = 200) -> list[dict[str, Any]]:
        return [r.to_dict() for r in svc.store.audit_log(limit)]

    @app.get("/api/eval")
    def evaluation(svc: Svc) -> dict[str, Any]:
        return load_results(svc.settings.results_dir)

    return app


def create_default_app() -> FastAPI:
    return create_app(settings=get_settings())
