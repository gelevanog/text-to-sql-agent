"""Command line: tally seed | ask | schema | validate | setup-reader | serve | eval ..."""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Annotated, Any

import typer
from rich.console import Console
from rich.syntax import Syntax
from rich.table import Table

from tally.config import Settings
from tally.logging_config import configure_logging

app = typer.Typer(help="Tally: ask your database questions in plain English.", no_args_is_help=True)
eval_app = typer.Typer(help="Evaluation runs (results go to results/).", no_args_is_help=True)
app.add_typer(eval_app, name="eval")
console = Console()
err = Console(stderr=True)


def _settings(**overrides: Any) -> Settings:
    settings = Settings()  # read the environment now (not the process-wide cached settings)
    configure_logging(settings.log_level, settings.log_format)
    return settings.model_copy(update={k: v for k, v in overrides.items() if v is not None})


def _split(value: str | None) -> list[str] | None:
    return [v.strip() for v in value.split(",") if v.strip()] if value is not None else None


# ---- data ---------------------------------------------------------------------------------------------------
@app.command()
def seed(
    reset: Annotated[bool, typer.Option(help="Drop and recreate the demo tables")] = True,
    seed_value: Annotated[int, typer.Option("--seed", help="Generator seed")] = 42,
    scale: Annotated[float, typer.Option(help="Order volume multiplier")] = 1.0,
) -> None:
    """Generate the Lumora demo database, the read-only reader role and the row-level security policies."""
    from tally.db import connect
    from tally.demo.seed import seed as run_seed

    settings = _settings()
    with connect(settings.database_url) as conn:
        report = run_seed(
            conn,
            schema=settings.data_schema,
            reader_role=settings.reader_role,
            reader_password=settings.reader_password,
            scoped_role=settings.scoped_reader_role,
            app_schema=settings.app_schema,
            seed_value=seed_value,
            scale=scale,
            reset=reset,
            rls=settings.rls,
        )
    table = Table("table", "rows", title=f"Lumora demo database ({report.total_rows:,} rows, {report.seconds} s)")
    for name, count in report.counts.items():
        table.add_row(name, f"{count:,}")
    console.print(table)


@app.command("setup-reader")
def setup_reader_command(
    tables: Annotated[str | None, typer.Option(help="Comma-separated tables to allow (default: all)")] = None,
) -> None:
    """For your own database: create/update the read-only reader role with grants that exclude the semantic layer's
    PII columns (run with an owner connection in TALLY_DATABASE_URL)."""
    from tally.db import connect
    from tally.security import setup_reader
    from tally.services import load_layer

    settings = _settings()
    layer = load_layer(settings)
    with connect(settings.database_url) as conn:
        granted = setup_reader(
            conn,
            schema=settings.data_schema,
            role=settings.reader_role,
            password=settings.reader_password,
            tables=_split(tables),
            pii_columns=layer.pii_columns,
            statement_timeout_ms=settings.statement_timeout_ms * 2,
            app_schema=settings.app_schema,
        )
    for table, columns in granted.items():
        console.print(f"[green]{table}[/green]: {', '.join(columns)}")


@app.command()
def schema(as_json: Annotated[bool, typer.Option("--json")] = False) -> None:
    """Show what Tally knows about the database: tables, views, metrics and PII columns."""
    from tally.services import build_services

    services = build_services(_settings(), memory_store=True, seed_demo=False)
    if as_json:
        console.print_json(json.dumps(services.catalog.to_json(), default=str))
        return
    table = Table("name", "kind", "rows", "columns", "personal data")
    for t in services.catalog.tables.values():
        table.add_row(
            t.name, "table", f"{t.row_count:,}", str(len(t.columns)), ", ".join(c.name for c in t.columns if c.pii)
        )
    for v in services.catalog.views.values():
        table.add_row(v.name, "semantic view", "", str(len(v.columns)), "")
    console.print(table)
    console.print(f"metrics: {', '.join(services.layer.metrics)}")


@app.command()
def validate(sql: Annotated[str, typer.Argument(help="SQL to check (nothing is executed)")]) -> None:
    """Run the static validator on a query and print the verdict."""
    from tally.services import build_services
    from tally.sql.expand import expand_views

    services = build_services(_settings(), memory_store=True, seed_demo=False)
    views = {name: view.sql for name, view in services.catalog.views.items()}
    result = services.validator.validate(expand_views(sql, views).sql)
    console.print_json(json.dumps(result.to_dict()))
    raise typer.Exit(0 if result.ok else 1)


# ---- asking ---------------------------------------------------------------------------------------------------
@app.command()
def ask(
    question: Annotated[str, typer.Argument(help="A question in plain English")],
    conversation: Annotated[str | None, typer.Option(help="Continue this conversation id")] = None,
    provider: Annotated[str | None, typer.Option(help="fake | openrouter | qwen | openai | anthropic")] = None,
    model: Annotated[str | None, typer.Option(help="Model id")] = None,
    as_json: Annotated[bool, typer.Option("--json", help="Print the full result as JSON")] = False,
) -> None:
    """Ask a question: prints the steps, the SQL, the result, the chart type and the answer."""
    from tally.services import build_services

    settings = _settings(llm_provider=provider, llm_model=model)
    services = build_services(settings, tag="cli")

    def emit(event: str, data: dict[str, Any]) -> None:
        if event == "step" and not as_json:
            err.print(f"[dim]{data['kind']:>13}[/dim]  {data['title']}")

    result = services.agent.run(question, conversation_id=conversation, emit=emit)
    services.close()
    if as_json:
        console.print_json(json.dumps(result.to_dict(), default=str))
        return
    if result.sql:
        console.print(Syntax(result.sql, "sql", word_wrap=True))
    if result.status == "clarification" and result.clarification:
        console.print(f"[yellow]Clarification:[/yellow] {result.clarification['question']}")
        for option in result.clarification.get("options", []):
            console.print(f"  - {option['label']}")
    elif result.status in {"blocked", "refused"} and result.blocked:
        console.print(f"[red]Blocked by the {result.blocked['layer']}:[/red] " + "; ".join(result.blocked["reasons"]))
    elif result.status == "error":
        console.print(f"[red]Error:[/red] {result.error}")
    else:
        table = Table(*result.columns)
        for row in result.rows[:20]:
            table.add_row(*[str(v) for v in row])
        console.print(table)
        if result.row_count > 20:
            console.print(f"[dim]... {result.row_count} rows[/dim]")
        console.print(f"[dim]chart: {(result.chart or {}).get('type')}[/dim]")
        console.print(f"[bold]{result.answer}[/bold]")
    console.print(
        f"[dim]conversation {result.conversation_id} | {result.llm_calls} model calls | {result.total_ms:.0f} ms[/dim]"
    )


@app.command()
def serve(
    host: Annotated[str, typer.Option()] = "0.0.0.0",
    port: Annotated[int, typer.Option()] = 8000,
) -> None:
    """Run the API (seeds the demo database on first start when TALLY_SEED_DEMO=true)."""
    import uvicorn

    uvicorn.run("tally.api.app:create_default_app", factory=True, host=host, port=port)


# ---- evaluation -------------------------------------------------------------------------------------------------
def _eval_services(settings: Settings, *, tag: str) -> Any:
    from tally.services import build_services

    return build_services(settings.model_copy(update={"llm_cache": True}), memory_store=True, tag=tag)


@eval_app.command("run")
def eval_run(
    name: Annotated[str, typer.Option(help="Result file name in results/")] = "fake",
    provider: Annotated[str | None, typer.Option(help="fake | openrouter | qwen | openai | anthropic")] = None,
    model: Annotated[str | None, typer.Option(help="Model id")] = None,
    fallbacks: Annotated[str | None, typer.Option(help="Comma-separated OpenRouter fallback models")] = None,
    subset: Annotated[bool, typer.Option(help="Only the stratified ablation subset")] = False,
    ids: Annotated[str | None, typer.Option(help="Comma-separated item ids")] = None,
    answers: Annotated[bool, typer.Option(help="Generate the answer text (one more model call per item)")] = True,
    retrieval: Annotated[str, typer.Option(help="retrieval | full")] = "retrieval",
    semantic_layer: Annotated[bool, typer.Option(help="Use the semantic layer")] = True,
    max_corrections: Annotated[int | None, typer.Option(help="Self-correction retries")] = None,
    embeddings: Annotated[str | None, typer.Option(help="off | openrouter")] = None,
    merge: Annotated[bool, typer.Option(help="Replace only these items in an existing results/<name>.json")] = False,
) -> None:
    """Run the benchmark through the full agent and score it (results/<name>.json)."""
    from tally.agent.store import InMemoryStore
    from tally.eval.benchmark import load_benchmark
    from tally.eval.runner import GoldCache, run_benchmark, save_run
    from tally.llm.factory import budgeted, build_chat_model
    from tally.services import make_agent, playbook_loader

    settings = _settings(llm_provider=provider, llm_model=model, embeddings=embeddings)
    if fallbacks is not None:
        settings = settings.model_copy(update={"llm_fallback_models": _split(fallbacks)})
    services = _eval_services(settings, tag=f"eval:{name}")
    llm = budgeted(
        build_chat_model(services.settings, playbook=playbook_loader(services.settings)),
        services.settings,
        tag=f"eval:{name}",
    )
    overrides: dict[str, Any] = {"retrieval_mode": retrieval, "semantic_layer": semantic_layer,
                                 "generate_answer": answers}  # fmt: skip
    if max_corrections is not None:
        overrides["max_corrections"] = max_corrections
    agent = make_agent(services, llm=llm, store=InMemoryStore(), **overrides)
    all_items = load_benchmark(settings.benchmark_file)
    wanted = set(_split(ids) or [])
    items = [i for i in all_items if (not subset or i.subset) and (not wanted or i.id in wanted)]
    views = {n: v.sql for n, v in services.catalog.views.items()}
    gold = GoldCache(services.db, views)

    def progress(item_id: str, record: dict[str, Any]) -> None:
        verdict = record.get("correct", record.get("clarified", record.get("blocked")))
        err.print(f"{item_id:20} {record['status']:13} {'ok ' if verdict else 'MISS'} "
                  f"calls={record['llm_calls']} {record['total_ms'] / 1000:.1f}s")  # fmt: skip

    records = run_benchmark(
        agent, items, all_items=all_items, gold=gold, executor=services.executor, pii=services.catalog.pii,
        progress=progress,
    )  # fmt: skip
    config = {
        "model": llm.label,
        "fallback_models": services.settings.llm_fallback_models
        if services.settings.llm_provider == "openrouter"
        else [],
        "subset": subset,
        "answers": answers,
        **{k: v for k, v in overrides.items() if k != "generate_answer"},
        "max_corrections": overrides.get("max_corrections", services.settings.max_corrections),
        "clarify_policy": services.settings.clarify_policy,
        "embeddings": services.settings.embeddings,
        "today": services.settings.effective_today.isoformat(),
    }
    path = settings.results_dir / f"{name}.json"
    if merge and path.exists():
        previous = json.loads(path.read_text(encoding="utf-8"))
        rerun = {r["id"]: {**r, "rerun": True} for r in records}
        records = [rerun.get(r["id"], r) for r in previous["items"]]
        config = {**previous["config"], "reruns": sorted({*previous["config"].get("reruns", []), *rerun})}
    payload = save_run(path, name=name, config=config, records=records)
    summary = payload["summary"]
    ex = summary["execution_accuracy"]
    console.print(f"[bold]{name}[/bold]: execution accuracy {ex['accuracy']}% ({ex['correct']}/{ex['total']}), "
                  f"safety {summary['safety']['blocked_or_refused']}/{summary['safety']['unsafe_total']} blocked, "
                  f"clarification recall {summary['clarification']['recall']}% precision "
                  f"{summary['clarification']['precision']}%, model calls {summary['llm_calls']['total']}")  # fmt: skip
    services.close()


@eval_app.command("free-models")
def eval_free_models() -> None:
    """List free OpenRouter models (no key needed) into results/free_models.json."""
    import httpx

    settings = _settings()
    data = httpx.get(f"{settings.openrouter_base_url}/models", timeout=30).json()["data"]
    free = [
        {
            "id": m["id"],
            "context_length": m.get("context_length"),
            "supported_parameters": m.get("supported_parameters", []),
        }
        for m in data
        if str(m["id"]).endswith(":free")
    ]
    path = settings.results_dir / "free_models.json"
    path.write_text(json.dumps({"models": free}, indent=1) + "\n", encoding="utf-8")
    for m in free:
        console.print(m["id"])


@eval_app.command("smoke")
def eval_smoke(
    models: Annotated[str, typer.Option(help="Comma-separated free OpenRouter model ids")],
    ids: Annotated[str, typer.Option(help="Benchmark items to try")] = "pop_01,trap_delete_03",
) -> None:
    """One SQL-generation call per model and item (no answer step): JSON format, valid SQL, correctness, latency."""
    from tally.agent.store import InMemoryStore
    from tally.eval.benchmark import load_benchmark
    from tally.eval.runner import GoldCache, run_benchmark
    from tally.llm.factory import budgeted, build_chat_model
    from tally.services import make_agent

    settings = _settings(llm_provider="openrouter")
    services = _eval_services(settings, tag="smoke")
    all_items = load_benchmark(settings.benchmark_file)
    items = [i for i in all_items if i.id in set(_split(ids) or [])]
    gold = GoldCache(services.db, {n: v.sql for n, v in services.catalog.views.items()})
    path = settings.results_dir / "smoke.json"
    report: dict[str, Any] = json.loads(path.read_text()) if path.exists() else {"models": {}}
    for model_id in _split(models) or []:
        llm = budgeted(build_chat_model(services.settings, model=model_id, fallback_models=[]), services.settings,
                       tag=f"smoke:{model_id}")  # fmt: skip
        agent = make_agent(services, llm=llm, store=InMemoryStore(), generate_answer=False, max_corrections=0)
        try:
            records = run_benchmark(agent, items, all_items=all_items, gold=gold, executor=services.executor,
                                    pii=services.catalog.pii)  # fmt: skip
        except Exception as exc:
            records = [{"error": str(exc)[:300]}]
        report["models"][model_id] = [
            {k: r.get(k) for k in ("id", "status", "correct", "error", "llm_ms", "attempt_stages", "served_models")}
            for r in records
        ]
        console.print(model_id, [(r.get("id"), r.get("status"), r.get("correct")) for r in records])
    path.write_text(json.dumps(report, indent=1) + "\n", encoding="utf-8")


@eval_app.command("ledger")
def eval_ledger() -> None:
    """Summarize the call ledger (results/calls.jsonl) into results/calls_summary.json."""
    from collections import Counter

    settings = _settings()
    ledger = settings.llm_ledger or Path("results/calls.jsonl")
    rows = [json.loads(line) for line in ledger.read_text(encoding="utf-8").splitlines() if line.strip()]
    requested = sorted(
        {str(r.get("requested_model")) for r in rows} | {m for r in rows for m in r.get("fallback_models") or []}
    )
    served = Counter(str(r.get("served_model")) for r in rows if r.get("served_model"))
    summary = {
        "total_requests": len(rows),
        "by_status": dict(Counter(r["status"] for r in rows)),
        "by_tag": dict(Counter(str(r.get("tag", "")).split(":")[0] for r in rows)),
        "by_tag_detail": dict(Counter(str(r.get("tag", "")) for r in rows)),
        "requested_models": requested,
        "served_models": dict(served),
        "all_model_ids_free": all(m.endswith(":free") for m in [*requested, *served]),
        "input_tokens": sum(int(r.get("input_tokens") or 0) for r in rows),
        "output_tokens": sum(int(r.get("output_tokens") or 0) for r in rows),
        "first_call": rows[0]["ts"] if rows else None,
        "last_call": rows[-1]["ts"] if rows else None,
    }
    (settings.results_dir / "calls_summary.json").write_text(json.dumps(summary, indent=1) + "\n", encoding="utf-8")
    console.print_json(json.dumps(summary))


def main() -> None:  # pragma: no cover
    os.environ.setdefault("PYTHONUTF8", "1")
    app()
