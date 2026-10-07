"use client";

import { Fragment, useEffect, useState } from "react";

import { highlightSql } from "@/components/SqlBlock";
import { api, type AuditRecord } from "@/lib/api";
import { cn, ms, shortModel } from "@/lib/format";

const STATUS: Record<string, string> = {
  answered: "bg-emerald-50 text-emerald-800",
  clarification: "bg-amber-50 text-amber-800",
  blocked: "bg-red-50 text-red-800",
  refused: "bg-red-50 text-red-800",
  error: "bg-zinc-100 text-zinc-700",
};

export default function AuditPage() {
  const [rows, setRows] = useState<AuditRecord[] | null>(null);
  const [open, setOpen] = useState<number | null>(null);
  useEffect(() => {
    api.audit().then(setRows, () => setRows([]));
  }, []);
  return (
    <div className="px-8 py-6">
      <header className="mb-5">
        <h1 className="text-[17px] font-semibold tracking-tight">Audit log</h1>
        <p className="max-w-3xl text-[12.5px] text-zinc-500">
          Every question: the SQL that ran (or was refused), the outcome, the row count, the time, the model and how many
          model calls and self-corrections it took. Result data is never stored here, and the read-only role that runs
          queries cannot read this table.
        </p>
      </header>
      <div className="overflow-hidden rounded-lg border border-line bg-white">
        <table className="w-full text-left text-[13px]">
          <thead className="bg-zinc-50 text-[11.5px] text-zinc-500">
            <tr>
              {["Time (UTC)", "Question", "Outcome", "Rows", "Duration", "Model calls", "Model", "Violations"].map((h) => (
                <th key={h} className="px-3 py-2 font-semibold whitespace-nowrap">{h}</th>
              ))}
            </tr>
          </thead>
          <tbody>
            {rows?.map((r) => (
              <Fragment key={r.id}>
                <tr onClick={() => setOpen(open === r.id ? null : r.id)} className="cursor-pointer border-t border-line align-top hover:bg-zinc-50/60">
                  <td className="px-3 py-2 font-mono text-[12px] whitespace-nowrap text-zinc-500">{r.ts.slice(0, 19).replace("T", " ")}</td>
                  <td className="max-w-md px-3 py-2">{r.question}</td>
                  <td className="px-3 py-2"><span className={cn("rounded px-1.5 py-0.5 text-[12px] font-medium", STATUS[r.status])}>{r.status}</span></td>
                  <td className="px-3 py-2 text-right tabular-nums">{r.row_count}</td>
                  <td className="px-3 py-2 text-right whitespace-nowrap tabular-nums">{ms(r.duration_ms)}</td>
                  <td className="px-3 py-2 text-right tabular-nums">{r.llm_calls}{r.corrections ? <span className="text-amber-700"> ({r.corrections} retry)</span> : ""}</td>
                  <td className="px-3 py-2 text-[12px] text-zinc-500">{shortModel(r.model)}</td>
                  <td className="px-3 py-2">
                    {r.violations.map((v) => <span key={v} className="mr-1 rounded bg-red-50 px-1.5 py-0.5 font-mono text-[11px] text-red-700">{v}</span>)}
                  </td>
                </tr>
                {open === r.id && (
                  <tr className="border-t border-line bg-zinc-50/60">
                    <td colSpan={8} className="px-4 py-3">
                      {r.sql ? (
                        <pre className="max-h-72 overflow-auto font-mono text-[12px] leading-relaxed whitespace-pre-wrap">{highlightSql(r.sql)}</pre>
                      ) : (
                        <span className="text-[12.5px] text-zinc-500">No SQL (a clarifying question or a refusal before any query was written).</span>
                      )}
                    </td>
                  </tr>
                )}
              </Fragment>
            ))}
          </tbody>
        </table>
        {rows && rows.length === 0 && <p className="px-4 py-6 text-[13px] text-zinc-500">No questions yet.</p>}
      </div>
    </div>
  );
}
