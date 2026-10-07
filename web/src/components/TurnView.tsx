"use client";

import { useState } from "react";

import type { AgentResult, Step } from "@/lib/api";
import { cn, ms, shortModel } from "@/lib/format";
import { ChartView } from "./ChartView";
import { BlockIcon, CheckIcon, QuestionIcon, StarIcon } from "./icons";
import { ResultTable } from "./ResultTable";
import { SqlBlock } from "./SqlBlock";
import { StepTimeline } from "./StepTimeline";

export type Turn = { id: string; question: string; steps: Step[]; result: AgentResult | null; error: string | null };

const LAYER: Record<string, string> = {
  validator: "SQL validator",
  cost_guard: "Cost guard",
  database: "Database permissions",
  model: "The model declined",
};

function liveSql(steps: Step[]): string {
  const sql = [...steps].reverse().find((s) => s.kind === "sql");
  return sql ? String(sql.detail.sql ?? "") : "";
}

type Props = {
  turn: Turn;
  onClarify: (option: { value: string; label: string }, clarificationId: string | null) => void;
  onSave: (question: string) => void;
  busy: boolean;
};

export function TurnView({ turn, onClarify, onSave, busy }: Props) {
  const r = turn.result;
  const [showTable, setShowTable] = useState(true);
  const running = !r && !turn.error;
  const sql = r?.sql || liveSql(turn.steps);
  return (
    <article className="space-y-3">
      <div className="flex items-start gap-3">
        <div className="mt-0.5 flex size-7 shrink-0 items-center justify-center rounded-full bg-ink text-[11px] font-semibold text-white">You</div>
        <div className="min-w-0 flex-1">
          <h2 className="text-[16.5px] leading-snug font-semibold tracking-tight text-zinc-900">{turn.question}</h2>
          {r && r.asked !== r.question && (
            <p className="mt-0.5 text-[12.5px] text-zinc-500">Answering: {r.question}</p>
          )}
        </div>
        {r?.status === "answered" && (
          <button type="button" onClick={() => onSave(r.question)} title="Save to the questions library"
            className="flex items-center gap-1 rounded-md px-2 py-1 text-[12px] text-zinc-500 hover:bg-white hover:text-zinc-800">
            <StarIcon width={14} height={14} /> Save
          </button>
        )}
      </div>

      <div className="ml-10 space-y-3">
        <StepTimeline steps={turn.steps} running={running} />

        {turn.error && <div className="rounded-lg border border-red-200 bg-red-50 px-4 py-3 text-[13.5px] text-red-800">{turn.error}</div>}

        {r?.status === "clarification" && r.clarification && (
          <div className="rounded-lg border border-amber-200 bg-amber-50/70 px-4 py-3">
            <div className="flex items-center gap-2 text-[13px] font-semibold text-amber-900">
              <QuestionIcon width={16} height={16} /> Before I run anything: {r.clarification.question}
            </div>
            <p className="mt-1 text-[12.5px] text-amber-800/80">
              {r.clarification.source === "semantic_layer"
                ? "This term is marked as ambiguous in the semantic layer, so Tally asks instead of guessing. No query has run yet."
                : "The model found the question ambiguous."}
            </p>
            {r.clarification.options.length > 0 && (
              <div className="mt-3 flex flex-wrap gap-2">
                {r.clarification.options.map((o) => (
                  <button key={o.value} type="button" disabled={busy} onClick={() => onClarify(o, r.clarification?.id ?? null)}
                    className="rounded-md border border-amber-300 bg-white px-3 py-1.5 text-[13px] font-medium text-amber-900 shadow-sm hover:border-amber-500 disabled:opacity-50">
                    {o.label}
                  </button>
                ))}
              </div>
            )}
          </div>
        )}

        {(r?.status === "blocked" || r?.status === "refused") && r.blocked && (
          <div className="rounded-lg border border-red-200 bg-white">
            <div className="flex items-center gap-2 border-b border-red-100 bg-red-50 px-4 py-2.5 text-[13.5px] font-semibold text-red-800">
              <BlockIcon width={16} height={16} /> Refused · {LAYER[r.blocked.layer] ?? r.blocked.layer}
              <span className="ml-auto text-[12px] font-normal text-red-700/80">nothing was executed against your data</span>
            </div>
            <ul className="space-y-1 px-4 py-3 text-[13.5px] text-zinc-700">
              {r.blocked.reasons.map((reason) => (
                <li key={reason} className="flex gap-2"><span className="text-red-500">•</span>{reason}</li>
              ))}
            </ul>
            {r.blocked.sql && <div className="px-4 pb-4"><SqlBlock sql={r.blocked.sql} tone="blocked" /></div>}
          </div>
        )}

        {r?.status === "error" && (
          <div className="rounded-lg border border-zinc-300 bg-white px-4 py-3 text-[13.5px] text-zinc-700">
            <span className="font-semibold">No answer.</span> {r.error}
          </div>
        )}

        {r?.status === "answered" && (
          <>
            <div className="rounded-lg border border-line bg-white px-5 py-4">
              <p className="text-[15px] leading-relaxed text-zinc-800">{r.answer}</p>
              <div className="mt-3 flex flex-wrap items-center gap-2 text-[11.5px]">
                {r.answer_check?.ok && r.answer_source !== "template" ? (
                  <span className="inline-flex items-center gap-1 rounded bg-emerald-50 px-1.5 py-0.5 font-medium text-emerald-800">
                    <CheckIcon width={12} height={12} /> every number checked against the result
                  </span>
                ) : (
                  <span className="rounded bg-amber-50 px-1.5 py-0.5 font-medium text-amber-800">answer built from the result (the model cited other numbers)</span>
                )}
                <span className="text-zinc-400">·</span>
                <span className="text-zinc-500">{r.row_count} rows</span>
                <span className="text-zinc-400">·</span>
                <span className="text-zinc-500">{ms(r.total_ms)} total, {ms(r.db_ms)} in the database</span>
                <span className="text-zinc-400">·</span>
                <span className="text-zinc-500">{r.llm_calls} model call{r.llm_calls === 1 ? "" : "s"}{r.corrections ? `, ${r.corrections} self-correction${r.corrections > 1 ? "s" : ""}` : ""}</span>
                <span className="text-zinc-400">·</span>
                <span className="text-zinc-500">{(r.served_models.length ? r.served_models : [r.model]).map(shortModel).join(", ")}</span>
              </div>
            </div>
            {r.chart && r.chart.type !== "table" && (
              <div className="rounded-lg border border-line bg-white px-4 pt-4 pb-2">
                <div className="mb-2 flex items-center gap-2 text-[11.5px] text-zinc-500">
                  <span className="rounded bg-zinc-100 px-1.5 py-0.5 font-medium text-zinc-600">{r.chart.type.replace("_", " ")}</span>
                  chosen by rule: {r.chart.reason}
                </div>
                <ChartView spec={r.chart} />
              </div>
            )}
            <div>
              <button type="button" onClick={() => setShowTable((v) => !v)} className="mb-1.5 text-[12px] font-medium text-zinc-500 hover:text-zinc-800">
                {showTable ? "Hide" : "Show"} result table
              </button>
              {showTable && <ResultTable columns={r.columns} rows={r.rows} rowCount={r.row_count} truncated={r.truncated} />}
            </div>
          </>
        )}

        {sql && r?.status !== "blocked" && r?.status !== "refused" && (
          <SqlBlock sql={sql} expandedSql={r?.executed_sql} views={r?.views ?? []} explanation={r?.explanation}
            plan={r?.plan} assumptions={r?.assumptions ?? []} />
        )}
        {r && r.status === "answered" && r.validation?.limit_applied && (
          <p className={cn("text-[12px] text-zinc-500")}>Tally added LIMIT {r.validation.limit_applied} to cap the result size.</p>
        )}
      </div>
    </article>
  );
}
