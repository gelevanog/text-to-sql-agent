"use client";

import { useEffect, useMemo, useState } from "react";
import { Bar, BarChart, CartesianGrid, Cell, LabelList, ResponsiveContainer, Tooltip, XAxis, YAxis } from "recharts";

import { api, type EvalData, type Run } from "@/lib/api";
import { cn, ms, shortModel } from "@/lib/format";

const CATEGORY: Record<string, string> = {
  lookup: "Lookups",
  aggregation: "Aggregations",
  join: "Joins (3-5 tables)",
  time_window: "Time windows",
  period_over_period: "Period over period",
  top_n_ties: "Top-N and ties",
  cohort_retention: "Cohorts and retention",
  trap_refund: "Trap: refunds",
  trap_fx: "Trap: currencies",
  trap_soft_delete: "Trap: deleted / test rows",
  null_handling: "NULL handling",
  timezone: "Time zones",
  follow_up: "Follow-ups",
  injection: "Injection in the data",
};

function pct(value: number | null | undefined): string {
  return value === null || value === undefined ? "—" : `${value}%`;
}

function Tile({ label, value, sub }: { label: string; value: string; sub?: string }) {
  return (
    <div className="rounded-lg border border-line bg-white px-4 py-3">
      <div className="text-[12px] text-zinc-500">{label}</div>
      <div className="mt-0.5 text-[24px] font-semibold tracking-tight tabular-nums">{value}</div>
      {sub && <div className="text-[11.5px] text-zinc-500">{sub}</div>}
    </div>
  );
}

export default function EvalPage() {
  const [data, setData] = useState<EvalData | null>(null);
  const [onlyMisses, setOnlyMisses] = useState(false);
  useEffect(() => {
    api.evaluation().then(setData, () => setData({ runs: [] }));
  }, []);
  const main: Run | undefined = useMemo(() => data?.runs.find((r) => r.name === "main") ?? data?.runs.find((r) => r.name !== "fake") ?? data?.runs[0], [data]);

  if (!data) return <div className="px-8 py-6 text-[13px] text-zinc-500">Loading…</div>;
  if (!main) return <div className="px-8 py-6 text-[13px] text-zinc-500">No evaluation results yet. Run <code>tally eval run</code>.</div>;
  const s = main.summary;
  const categories = Object.entries(s.execution_accuracy.by_category).map(([key, v]) => ({
    name: CATEGORY[key] ?? key,
    accuracy: v.accuracy ?? 0,
    label: `${v.correct}/${v.total}`,
  }));
  const unsafe = main.items.filter((i) => i.expect === "block");
  const items = main.items.filter((i) => !onlyMisses || (i.expect === "answer" ? !i.correct : i.expect === "clarify" ? !i.clarified : !i.blocked));

  return (
    <div className="space-y-8 px-8 py-6">
      <header>
        <h1 className="text-[17px] font-semibold tracking-tight">Evaluation</h1>
        <p className="max-w-3xl text-[12.5px] text-zinc-500">
          {main.items.length} hand-written questions about the demo database (written by an AI agent in the session that built
          this repository), each run through the full agent with {shortModel(String(main.config.model ?? ""))} on {main.date?.slice(0, 10)}.
          Execution accuracy compares the returned rows with a gold query&apos;s.
        </p>
      </header>

      <section className="grid grid-cols-2 gap-3 lg:grid-cols-4 xl:grid-cols-7">
        <Tile label="Execution accuracy" value={pct(s.execution_accuracy.accuracy)} sub={`${s.execution_accuracy.correct} of ${s.execution_accuracy.total} answerable`} />
        <Tile label="Unsafe queries executed" value={`${s.safety.unsafe_executed} of ${s.safety.unsafe_total}`} sub={`${s.safety.blocked_or_refused} refused, the rest answered safely or failed`} />
        <Tile label="Clarification recall" value={pct(s.clarification.recall)} sub={`${s.clarification.asked_on_ambiguous} of ${s.clarification.ambiguous_total} ambiguous`} />
        <Tile label="Clarification precision" value={pct(s.clarification.precision)} sub={`${s.clarification.asked_on_answerable} needless questions`} />
        <Tile label="Self-correction" value={`+${s.self_correction.rescued}`} sub={`first attempt ${pct(s.self_correction.first_attempt_accuracy)}`} />
        <Tile label="Answers checked first time" value={pct(s.answer_faithfulness.first_draft_rate)} sub={`${s.answer_faithfulness.template_fallback} template fallbacks`} />
        <Tile label="Answer time p50" value={`${ms(s.latency_ms.answerable_p50)}`} sub={`p95 ${ms(s.latency_ms.answerable_p95)} · ${s.llm_calls.mean ?? "—"} model calls / question`} />
      </section>

      <section className="grid gap-6 xl:grid-cols-[1.1fr_1fr]">
        <div className="rounded-lg border border-line bg-white px-4 py-4">
          <h2 className="mb-3 text-[13px] font-semibold">Execution accuracy by category</h2>
          <ResponsiveContainer width="100%" height={categories.length * 28 + 30}>
            <BarChart data={categories} layout="vertical" margin={{ left: 8, right: 44 }}>
              <CartesianGrid stroke="#ebe7df" horizontal={false} />
              <XAxis type="number" domain={[0, 100]} tick={{ fontSize: 11, fill: "#6b6f76" }} tickFormatter={(v: number) => `${v}%`} axisLine={false} tickLine={false} />
              <YAxis type="category" dataKey="name" width={170} tick={{ fontSize: 12, fill: "#3f434a" }} axisLine={false} tickLine={false} />
              <Tooltip formatter={(v: unknown) => [`${String(v)}%`, "accuracy"]} cursor={{ fill: "rgba(20,24,33,0.04)" }} />
              <Bar dataKey="accuracy" radius={[0, 4, 4, 0]} maxBarSize={16}>
                {categories.map((c) => <Cell key={c.name} fill={c.accuracy >= 80 ? "#2a78d6" : c.accuracy >= 50 ? "#6fa3e6" : "#a9c7ef"} />)}
                <LabelList dataKey="label" position="right" style={{ fontSize: 11, fill: "#52514e" }} />
              </Bar>
            </BarChart>
          </ResponsiveContainer>
        </div>
        <div className="space-y-6">
          {data.comparison && (
            <div className="overflow-hidden rounded-lg border border-line bg-white">
              <h2 className="border-b border-line px-4 py-2.5 text-[13px] font-semibold">
                Ablations and model comparison <span className="font-normal text-zinc-500">({data.comparison.subset_size}-question subset)</span>
              </h2>
              <table className="w-full text-[12.5px]">
                <thead className="bg-zinc-50 text-[11px] text-zinc-500">
                  <tr><th className="px-3 py-1.5 text-left font-semibold">Configuration</th><th className="px-3 py-1.5 text-right font-semibold">Accuracy</th><th className="px-3 py-1.5 text-right font-semibold">Valid SQL</th><th className="px-3 py-1.5 text-right font-semibold">Prompt tokens</th></tr>
                </thead>
                <tbody>
                  {data.comparison.rows.map((row) => (
                    <tr key={row.name} className="border-t border-line">
                      <td className="px-3 py-1.5"><div className="font-medium">{row.label}</div><div className="text-[11px] text-zinc-500">{shortModel(row.model)}</div></td>
                      <td className="px-3 py-1.5 text-right font-semibold tabular-nums">{pct(row.accuracy)} <span className="font-normal text-zinc-400">{row.correct}/{row.total}</span></td>
                      <td className="px-3 py-1.5 text-right tabular-nums">{pct(row.valid_sql)}</td>
                      <td className="px-3 py-1.5 text-right tabular-nums">{row.prompt_tokens_median?.toLocaleString() ?? "—"}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          )}
          <div className="overflow-hidden rounded-lg border border-line bg-white">
            <h2 className="border-b border-line px-4 py-2.5 text-[13px] font-semibold">Unsafe requests</h2>
            <ul className="divide-y divide-line text-[12.5px]">
              {unsafe.map((i) => (
                <li key={i.id} className="flex items-center gap-2 px-4 py-1.5">
                  <span className={cn("rounded px-1.5 py-0.5 text-[11px] font-semibold whitespace-nowrap", i.blocked ? "bg-emerald-50 text-emerald-800" : i.unsafe_executed ? "bg-red-50 text-red-800" : "bg-amber-50 text-amber-800")}>
                    {i.blocked ? "refused" : i.unsafe_executed ? "UNSAFE" : i.status === "answered" ? "answered safely" : `${i.status}, nothing ran`}
                  </span>
                  <span className="min-w-0 flex-1 truncate">{i.question}</span>
                  <span className="text-[11px] whitespace-nowrap text-zinc-500">{i.blocked_layer ? `by the ${i.blocked_layer.replace("_", " ")}` : ""}</span>
                </li>
              ))}
            </ul>
          </div>
        </div>
      </section>

      <section className="overflow-hidden rounded-lg border border-line bg-white">
        <div className="flex items-center border-b border-line px-4 py-2.5">
          <h2 className="text-[13px] font-semibold">Every question</h2>
          <label className="ml-auto flex items-center gap-1.5 text-[12px] text-zinc-600">
            <input type="checkbox" checked={onlyMisses} onChange={(e) => setOnlyMisses(e.target.checked)} /> only misses
          </label>
        </div>
        <table className="w-full text-[12.5px]">
          <tbody>
            {items.map((i) => {
              const ok = i.expect === "answer" ? i.correct : i.expect === "clarify" ? i.clarified : i.blocked;
              return (
                <tr key={i.id} className="border-t border-line first:border-0">
                  <td className="w-8 px-3 py-1.5">{ok ? <span className="text-emerald-600">✓</span> : <span className="text-red-600">✗</span>}</td>
                  <td className="px-2 py-1.5 font-mono text-[11.5px] whitespace-nowrap text-zinc-500">{i.id}</td>
                  <td className="px-2 py-1.5">{i.question}</td>
                  <td className="px-2 py-1.5 text-[11.5px] whitespace-nowrap text-zinc-500">{i.status}{i.rescued ? " · rescued by a retry" : ""}</td>
                  <td className="px-3 py-1.5 text-right text-[11.5px] whitespace-nowrap text-zinc-500 tabular-nums">{i.llm_calls} calls · {ms(i.total_ms)}</td>
                </tr>
              );
            })}
          </tbody>
        </table>
      </section>
    </div>
  );
}
