"""Build comparison numbers: free baseline vs paid runs (reads results/ and results/paid-models/)."""
import json, sys
from pathlib import Path
sys.path.insert(0, "src")
from tally.eval.runner import summarize

R = Path("results"); P = R / "paid-models"
def load(p): return json.loads(p.read_text())
main = load(R / "main.json"); rerun = load(R / "main_rerun.json")
rr = {r["id"]: r for r in rerun["items"]}
main_fixed = [rr.get(r["id"], r) for r in main["items"]]
subset_ids = {r["id"] for r in load(R / "subset_full.json")["items"]}

runs = {
    "free (as run)": main["items"],
    "free (after fixes)": main_fixed,
    "free subset": load(R / "subset_full.json")["items"],
}
for name in ["main_haiku", "main_haiku_as_run", "subset_haiku", "subset_opus", "hard_opus", "safety_opus", "smoke_qwen", "subset_qwen", "safety_qwen"]:
    if (P / f"{name}.json").exists():
        runs[name] = load(P / f"{name}.json")["items"]
if "main_haiku" in runs:
    runs["haiku subset (from full run)"] = [r for r in runs["main_haiku"] if r["id"] in subset_ids]
runs["free subset (from main after fixes)"] = [r for r in main_fixed if r["id"] in subset_ids]
opus45 = runs["subset_opus"] + runs["hard_opus"]
ids45 = {r["id"] for r in opus45 if r["expect"] == "answer"}
runs["opus 45"] = [r for r in opus45 if r["expect"] == "answer"]
runs["free 45"] = [r for r in main_fixed if r["id"] in ids45]
runs["haiku 45"] = [r for r in runs["main_haiku"] if r["id"] in ids45]
hard = {r["id"] for r in main_fixed if r["expect"] == "answer" and not r.get("correct")} | {r["id"] for r in runs["main_haiku"] if r["expect"] == "answer" and not r.get("correct")}
print("hard union", len(hard))
for nm, src in [("opus", opus45), ("free", main_fixed), ("haiku", runs["main_haiku"])]:
    print("hard", nm, sum(1 for r in src if r["id"] in hard and r.get("correct")), "/", len(hard))

def repair(rows):
    ans = [r for r in rows if r["expect"] == "answer"]
    first_wrong_corrected = [r for r in ans if not r.get("first_attempt_correct") and r["corrections"] > 0]
    return {"rescued": sum(1 for r in ans if r.get("rescued")),
            "items_with_corrections": sum(1 for r in rows if r["corrections"] > 0),
            "answerable_with_corrections": len([r for r in ans if r["corrections"] > 0]),
            "repair_success": f"{sum(1 for r in first_wrong_corrected if r.get('correct'))}/{len(first_wrong_corrected)}"}

out = {}
for name, rows in runs.items():
    s = summarize(rows)
    out[name] = {
        "items": s["items"],
        "ex": f"{s['execution_accuracy']['accuracy']}% ({s['execution_accuracy']['correct']}/{s['execution_accuracy']['total']})",
        "first": f"{s['self_correction']['first_attempt_accuracy']}% ({s['self_correction']['first_attempt_correct']})",
        "valid_sql": s["valid_sql"]["rate"],
        "repair": repair(rows),
        "clar": f"recall {s['clarification']['recall']} precision {s['clarification']['precision']} ({s['clarification']['by_source']})",
        "safety": f"{s['safety']['blocked_or_refused']}/{s['safety']['unsafe_total']} blocked, executed unsafe {s['safety']['unsafe_executed']}, layers {s['safety']['by_layer']}, not_blocked {s['safety']['not_blocked']}, contact {s['safety']['answers_with_contact_data']}",
        "faith": s["answer_faithfulness"],
        "lat": s["latency_ms"],
        "calls": s["llm_calls"],
        "by_cat": {k: f"{v['correct']}/{v['total']}" for k, v in s["execution_accuracy"]["by_category"].items()},
    }
Path(sys.argv[1]).write_text(json.dumps(out, indent=1))

# ledger usage per tag
calls = [json.loads(l) for l in (P / "calls.jsonl").read_text().splitlines() if l.strip()]
price = {"claude-haiku-5-5": (0.10, 0.50), "claude-opus-5-5": (4.0, 20.0), "qwen3.7-plus": (0, 0)}
agg = {}
for c in calls:
    a = agg.setdefault(c["tag"], {"requests": 0, "ok": 0, "in": 0, "out": 0, "usd": 0.0, "lat": []})
    a["requests"] += 1; a["ok"] += c["status"] == "ok"; a["in"] += c["input_tokens"]; a["out"] += c["output_tokens"]
    pi, po = price.get(c["requested_model"], (0, 0))
    a["usd"] += (c["input_tokens"] * pi + c["output_tokens"] * po) / 1e6
    if c["status"] == "ok": a["lat"].append(c["latency_s"])
for t, a in agg.items():
    lat = sorted(a.pop("lat")); a["usd"] = round(a["usd"], 4)
    a["call_latency_p50_s"] = lat[len(lat)//2] if lat else None
    print(t, a)
