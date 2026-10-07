"""Evaluation results for the web app: run summaries from results/*.json, without the per-item SQL and answers."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

ITEM_FIELDS = ("id", "category", "expect", "status", "correct", "clarified", "blocked", "rescued", "llm_calls",
               "total_ms", "blocked_layer", "answer_source", "question", "unsafe_executed")  # fmt: skip


def load_results(directory: Path) -> dict[str, Any]:
    runs: list[dict[str, Any]] = []
    extra: dict[str, Any] = {}
    if not directory.exists():
        return {"runs": runs}
    for path in sorted(directory.glob("*.json")):
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if isinstance(data, dict) and "summary" in data and "items" in data:
            runs.append(
                {
                    "name": data.get("name", path.stem),
                    "date": data.get("date"),
                    "config": data.get("config", {}),
                    "summary": data["summary"],
                    "items": [{k: item.get(k) for k in ITEM_FIELDS} for item in data["items"]],
                }
            )
        elif path.stem in {"calls_summary", "smoke", "comparison"}:
            extra[path.stem] = data
    return {"runs": runs, **extra}
