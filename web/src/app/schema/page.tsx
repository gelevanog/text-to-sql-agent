"use client";

import { useEffect, useMemo, useState } from "react";

import { highlightSql } from "@/components/SqlBlock";
import { KeyIcon, LockIcon } from "@/components/icons";
import { api, type SchemaInfo, type TableInfo } from "@/lib/api";
import { cn } from "@/lib/format";

type Tab = "tables" | "views" | "metrics" | "rules" | "ambiguities";
const TABS: { id: Tab; label: string }[] = [
  { id: "tables", label: "Tables" },
  { id: "views", label: "Semantic views" },
  { id: "metrics", label: "Metrics" },
  { id: "rules", label: "Rules & synonyms" },
  { id: "ambiguities", label: "Ambiguous terms" },
];

export default function SchemaPage() {
  const [schema, setSchema] = useState<SchemaInfo | null>(null);
  const [error, setError] = useState("");
  const [tab, setTab] = useState<Tab>("tables");
  const [selected, setSelected] = useState("orders");
  const [filter, setFilter] = useState("");
  useEffect(() => {
    api.schema().then(setSchema, (e: unknown) => setError(String(e)));
  }, []);

  const list: TableInfo[] = useMemo(() => {
    if (!schema) return [];
    const items = tab === "views" ? schema.views : schema.tables;
    const f = filter.toLowerCase();
    return items.filter((t) => !f || t.name.includes(f) || t.columns.some((c) => c.name.includes(f)));
  }, [schema, tab, filter]);
  const current = list.find((t) => t.name === selected) ?? list[0];

  return (
    <div className="px-8 py-6">
      <header className="mb-5">
        <h1 className="text-[17px] font-semibold tracking-tight">Schema &amp; semantic layer</h1>
        <p className="text-[12.5px] text-zinc-500">
          What Tally knows about {schema?.company ?? "the database"}: introspected tables, columns, keys, row counts and sample
          values, plus the semantic layer (descriptions, metric definitions, synonyms, join paths and ambiguous terms) from YAML.
          Personal-data columns are listed but never shown to the model or sampled.
        </p>
      </header>
      {error && <p className="text-red-700">{error}</p>}
      <div className="mb-4 flex gap-1 border-b border-line">
        {TABS.map((t) => (
          <button key={t.id} type="button" onClick={() => setTab(t.id)}
            className={cn("-mb-px border-b-2 px-3 py-2 text-[13px] font-medium", tab === t.id ? "border-tally text-zinc-900" : "border-transparent text-zinc-500 hover:text-zinc-800")}>
            {t.label}
            {schema && t.id === "tables" && <span className="ml-1 text-zinc-400">{schema.tables.length}</span>}
            {schema && t.id === "views" && <span className="ml-1 text-zinc-400">{schema.views.length}</span>}
            {schema && t.id === "metrics" && <span className="ml-1 text-zinc-400">{Object.keys(schema.metrics).length}</span>}
          </button>
        ))}
      </div>

      {schema && (tab === "tables" || tab === "views") && (
        <div className="grid grid-cols-[220px_1fr] gap-6">
          <div>
            <input value={filter} onChange={(e) => setFilter(e.target.value)} placeholder="Filter tables or columns"
              className="mb-2 w-full rounded-md border border-line bg-white px-2.5 py-1.5 text-[13px] outline-none focus:border-zinc-400" />
            <ul className="space-y-0.5">
              {list.map((t) => (
                <li key={t.name}>
                  <button type="button" onClick={() => setSelected(t.name)}
                    className={cn("flex w-full items-center justify-between rounded-md px-2.5 py-1.5 text-left text-[13px]", current?.name === t.name ? "bg-ink text-white" : "text-zinc-700 hover:bg-white")}>
                    <span className="font-mono text-[12.5px]">{t.name}</span>
                    {!t.is_view && <span className={cn("text-[11px] tabular-nums", current?.name === t.name ? "text-zinc-400" : "text-zinc-400")}>{t.row_count.toLocaleString()}</span>}
                  </button>
                </li>
              ))}
            </ul>
          </div>
          {current && <TableDetail table={current} />}
        </div>
      )}

      {schema && tab === "metrics" && (
        <div className="grid gap-3 lg:grid-cols-2">
          {Object.entries(schema.metrics).map(([name, m]) => (
            <div key={name} className="rounded-lg border border-line bg-white px-4 py-3">
              <div className="flex items-baseline gap-2">
                <span className="font-mono text-[13.5px] font-semibold">{name}</span>
                {m.view && <span className="text-[11.5px] text-zinc-500">over <code className="text-tally">{m.view}</code></span>}
              </div>
              <p className="mt-1 text-[13px] text-zinc-600">{m.description}</p>
              <pre className="mt-2 rounded bg-zinc-50 px-2.5 py-1.5 font-mono text-[12px] whitespace-pre-wrap">{highlightSql(m.sql)}</pre>
              {m.synonyms.length > 0 && <p className="mt-2 text-[11.5px] text-zinc-500">also called: {m.synonyms.join(", ")}</p>}
            </div>
          ))}
        </div>
      )}

      {schema && tab === "rules" && (
        <div className="grid gap-6 lg:grid-cols-2">
          <section>
            <h2 className="mb-2 text-[13px] font-semibold">Business rules (sent with every question)</h2>
            <ol className="space-y-2">
              {schema.rules.map((rule, i) => (
                <li key={i} className="rounded-lg border border-line bg-white px-4 py-2.5 text-[13px] leading-relaxed text-zinc-700">{rule}</li>
              ))}
            </ol>
          </section>
          <section>
            <h2 className="mb-2 text-[13px] font-semibold">Synonyms</h2>
            <div className="overflow-hidden rounded-lg border border-line bg-white">
              {Object.entries(schema.synonyms).map(([term, words]) => (
                <div key={term} className="flex gap-3 border-b border-line px-4 py-2 text-[13px] last:border-0">
                  <span className="w-36 shrink-0 font-medium">{term}</span>
                  <span className="text-zinc-600">{words.join(", ")}</span>
                </div>
              ))}
            </div>
            <h2 className="mt-6 mb-2 text-[13px] font-semibold">Join paths</h2>
            <div className="columns-2 gap-4 font-mono text-[12px] text-zinc-600">
              {schema.joins.map(([a, b]) => <div key={`${a}${b}`}>{a} = {b}</div>)}
            </div>
          </section>
        </div>
      )}

      {schema && tab === "ambiguities" && (
        <div className="space-y-3">
          <p className="max-w-3xl text-[13px] text-zinc-600">
            When a question uses one of these terms without settling it, Tally asks before running anything (policy
            <code className="mx-1">TALLY_CLARIFY_POLICY=ask</code>), or uses the default and says so (<code>assume</code>).
          </p>
          {schema.ambiguities.map((a) => (
            <div key={a.id} className="rounded-lg border border-line bg-white px-4 py-3">
              <div className="font-mono text-[13px] font-semibold">{a.id}</div>
              <p className="text-[13px] text-zinc-600">{a.description}</p>
              <p className="mt-2 text-[13.5px] font-medium text-amber-900">“{a.question}”</p>
              <div className="mt-2 flex flex-wrap gap-2">
                {a.options.map((o) => (
                  <span key={o.value} className={cn("rounded border px-2 py-0.5 text-[12px]", o.value === a.default ? "border-amber-300 bg-amber-50" : "border-line")}>
                    {o.label}{o.value === a.default && " · default"}
                  </span>
                ))}
              </div>
              <p className="mt-2 font-mono text-[11.5px] text-zinc-500">triggers: {a.triggers.join("  ")}</p>
              <p className="font-mono text-[11.5px] text-zinc-500">settled by: {a.resolved_by.join("  ")}</p>
            </div>
          ))}
        </div>
      )}
    </div>
  );
}

function TableDetail({ table }: { table: TableInfo }) {
  return (
    <section className="min-w-0">
      <div className="flex items-baseline gap-3">
        <h2 className="font-mono text-[16px] font-semibold">{table.name}</h2>
        {table.is_view ? (
          <span className="rounded bg-tally-soft px-1.5 py-0.5 text-[11.5px] font-medium text-tally">semantic view · expanded as a CTE</span>
        ) : (
          <span className="text-[12px] text-zinc-500">{table.row_count.toLocaleString()} rows</span>
        )}
      </div>
      {table.description && <p className="mt-1 max-w-3xl text-[13.5px] text-zinc-600">{table.description}</p>}
      {table.synonyms.length > 0 && <p className="mt-1 text-[12px] text-zinc-500">also called: {table.synonyms.join(", ")}</p>}
      <div className="mt-4 overflow-hidden rounded-lg border border-line bg-white">
        <table className="w-full text-left text-[13px]">
          <thead className="bg-zinc-50 text-[11.5px] text-zinc-500">
            <tr>
              <th className="px-3 py-2 font-semibold">Column</th>
              <th className="px-3 py-2 font-semibold">Type</th>
              <th className="px-3 py-2 font-semibold">Description and sample values</th>
            </tr>
          </thead>
          <tbody>
            {table.columns.map((c) => (
              <tr key={c.name} className={cn("border-t border-line align-top", c.pii && "bg-red-50/40")}>
                <td className="px-3 py-2 font-mono text-[12.5px] whitespace-nowrap">
                  <span className="inline-flex items-center gap-1.5">
                    {c.name}
                    {c.primary_key && <KeyIcon width={12} height={12} className="text-amber-600" />}
                    {c.pii && <span className="inline-flex items-center gap-0.5 rounded bg-red-100 px-1 text-[10.5px] font-semibold text-red-700"><LockIcon width={10} height={10} />PII</span>}
                  </span>
                </td>
                <td className="px-3 py-2 text-[12px] whitespace-nowrap text-zinc-500">{c.type}</td>
                <td className="px-3 py-2 text-zinc-600">
                  {c.references && <span className="mr-2 font-mono text-[11.5px] text-indigo-700">→ {c.references}</span>}
                  {c.pii ? <span className="text-red-700">Personal data: not granted to the reader role, never sent to the model.</span> : c.description}
                  {c.samples.length > 0 && (
                    <div className="mt-1 flex flex-wrap gap-1">
                      {c.samples.slice(0, 12).map((s) => <span key={s} className="rounded bg-zinc-100 px-1.5 font-mono text-[11px] text-zinc-600">{s}</span>)}
                    </div>
                  )}
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
      {table.is_view && table.sql && (
        <pre className="mt-4 overflow-x-auto rounded-lg border border-line bg-white px-4 py-3 font-mono text-[12px] leading-relaxed whitespace-pre-wrap">{highlightSql(table.sql)}</pre>
      )}
    </section>
  );
}
