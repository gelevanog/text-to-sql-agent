"use client";

import { useState } from "react";

import { cn } from "@/lib/format";

const KEYWORDS = new Set(
  (
    "select from where group by order having limit offset with as join left right inner outer full cross on and or " +
    "not in is null case when then else end distinct union all intersect except between like ilike exists asc desc " +
    "nulls first last over partition rows range preceding following current row filter within interval date " +
    "timestamp at time zone cast true false using lateral recursive fetch only values"
  ).split(" "),
);
const TOKEN = /(--[^\n]*|\/\*[\s\S]*?\*\/)|('(?:[^']|'')*')|(\b\d+(?:\.\d+)?\b)|([A-Za-z_][A-Za-z0-9_]*)(\s*\()?|(\s+|[^\sA-Za-z0-9_']+)/g;

export function highlightSql(sql: string, views: string[] = []) {
  const parts: React.ReactNode[] = [];
  const viewSet = new Set(views.map((v) => v.toLowerCase()));
  let match: RegExpExecArray | null;
  let i = 0;
  TOKEN.lastIndex = 0;
  while ((match = TOKEN.exec(sql)) !== null) {
    const [whole, comment, str, num, word, paren] = match;
    if (comment) parts.push(<span key={i++} className="sql-comment">{comment}</span>);
    else if (str) parts.push(<span key={i++} className="sql-string">{str}</span>);
    else if (num) parts.push(<span key={i++} className="sql-number">{num}</span>);
    else if (word) {
      const lower = word.toLowerCase();
      const cls = viewSet.has(lower) ? "sql-view" : paren ? "sql-function" : KEYWORDS.has(lower) ? "sql-keyword" : "";
      parts.push(<span key={i++} className={cls}>{word}</span>);
      if (paren) parts.push(paren);
    } else parts.push(whole);
  }
  return parts;
}

type Props = {
  sql: string;
  expandedSql?: string;
  views?: string[];
  explanation?: string;
  plan?: string;
  assumptions?: string[];
  tone?: "default" | "blocked";
};

export function SqlBlock({ sql, expandedSql, views = [], explanation, plan, assumptions = [], tone = "default" }: Props) {
  const [explain, setExplain] = useState(false);
  const [expanded, setExpanded] = useState(false);
  const [copied, setCopied] = useState(false);
  const shown = expanded && expandedSql ? expandedSql : sql;
  return (
    <div className={cn("overflow-hidden rounded-lg border bg-white", tone === "blocked" ? "border-red-200" : "border-line")}>
      <div className="flex flex-wrap items-center gap-2 border-b border-line bg-zinc-50/70 px-3 py-1.5">
        <span className="text-[11px] font-semibold tracking-wider text-zinc-500 uppercase">SQL</span>
        {views.length > 0 && (
          <span className="text-[11px] text-zinc-500">
            uses semantic view{views.length > 1 ? "s" : ""}{" "}
            {views.map((v) => (
              <code key={v} className="rounded bg-tally-soft px-1 text-tally">{v}</code>
            ))}
          </span>
        )}
        <div className="ml-auto flex gap-1">
          {(explanation || plan) && (
            <button
              type="button"
              onClick={() => setExplain((v) => !v)}
              className={cn("rounded px-2 py-0.5 text-[12px] font-medium", explain ? "bg-ink text-white" : "text-zinc-600 hover:bg-zinc-200/70")}
            >
              Explain
            </button>
          )}
          {expandedSql && expandedSql !== sql && (
            <button
              type="button"
              onClick={() => setExpanded((v) => !v)}
              className={cn("rounded px-2 py-0.5 text-[12px] font-medium", expanded ? "bg-ink text-white" : "text-zinc-600 hover:bg-zinc-200/70")}
            >
              {expanded ? "As written" : "As executed"}
            </button>
          )}
          <button
            type="button"
            onClick={() => {
              void navigator.clipboard?.writeText(shown);
              setCopied(true);
              setTimeout(() => setCopied(false), 1200);
            }}
            className="rounded px-2 py-0.5 text-[12px] font-medium text-zinc-600 hover:bg-zinc-200/70"
          >
            {copied ? "Copied" : "Copy"}
          </button>
        </div>
      </div>
      {explain && (
        <div className="space-y-1.5 border-b border-line bg-amber-50/40 px-4 py-3 text-[13.5px] leading-relaxed text-zinc-700">
          {explanation && <p>{explanation}</p>}
          {plan && <p className="text-zinc-500"><span className="font-medium text-zinc-600">Plan: </span>{plan}</p>}
          {assumptions.length > 0 && (
            <ul className="list-disc pl-5 text-zinc-500">
              {assumptions.map((a) => <li key={a}>{a}</li>)}
            </ul>
          )}
        </div>
      )}
      <pre className={cn("overflow-x-auto px-4 py-3 font-mono text-[12.5px] leading-relaxed whitespace-pre-wrap text-zinc-800", expanded && "max-h-96")}>
        {highlightSql(shown, views)}
      </pre>
    </div>
  );
}
