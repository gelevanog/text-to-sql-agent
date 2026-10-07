"use client";

import {
  Bar,
  BarChart,
  CartesianGrid,
  Cell,
  Legend,
  Line,
  LineChart,
  Pie,
  PieChart,
  ResponsiveContainer,
  Tooltip,
  XAxis,
  YAxis,
} from "recharts";

import type { ChartSpec } from "@/lib/api";
import { compact, formatValue, humanize } from "@/lib/format";

// Categorical slots in fixed order (reference palette, validated for adjacent-pair colour-vision separation).
const SERIES = ["#2a78d6", "#eb6834", "#1baf7a", "#eda100", "#e87ba4", "#008300", "#4a3aa7", "#e34948"];
const GRID = "#ebe7df";
const AXIS = { fontSize: 11.5, fill: "#6b6f76" };

function label(value: unknown): string {
  const text = String(value ?? "");
  if (/^\d{4}-\d{2}-01(T00:00:00)?/.test(text)) return text.slice(0, 7);
  if (/^\d{4}-\d{2}-\d{2}/.test(text)) return text.slice(0, 10);
  return text;
}

export function ChartView({ spec }: { spec: ChartSpec }) {
  if (spec.type === "table" || !spec.data.length) return null;
  const measure = spec.y[0] ?? "";
  const tooltipFormatter = (value: unknown, name: unknown) => [formatValue(Number(value), spec.series.length ? measure : String(name)), humanize(String(name))];

  if (spec.type === "kpi") {
    const row = spec.data[0];
    return (
      <div className="grid grid-cols-[repeat(auto-fit,minmax(170px,1fr))] gap-3">
        {spec.y.map((key) => (
          <div key={key} className="rounded-lg border border-line bg-white px-4 py-3">
            <div className="text-[12px] text-zinc-500">{humanize(key)}</div>
            <div className="mt-1 text-[26px] font-semibold tracking-tight tabular-nums">{formatValue(row[key], key)}</div>
            {spec.x && <div className="text-[12px] text-zinc-500">{String(row[spec.x])}</div>}
          </div>
        ))}
      </div>
    );
  }

  const seriesKeys = spec.series.length ? spec.series : spec.y;
  const legend = seriesKeys.length > 1 ? <Legend iconType="circle" iconSize={8} itemSorter={null} wrapperStyle={{ fontSize: 12 }} formatter={(v: string) => humanize(v)} /> : null;
  const height = spec.horizontal ? Math.max(220, spec.data.length * 30 + 40) : 280;

  if (spec.type === "pie") {
    return (
      <ResponsiveContainer width="100%" height={260}>
        <PieChart>
          <Pie data={spec.data} dataKey={measure} nameKey={spec.x ?? ""} innerRadius={60} outerRadius={100} stroke="#fff" strokeWidth={2}
            label={(p: { name?: string; percent?: number }) => `${p.name ?? ""} ${(100 * (p.percent ?? 0)).toFixed(0)}%`}>
            {spec.data.map((_, i) => <Cell key={i} fill={SERIES[i % SERIES.length]} />)}
          </Pie>
          <Tooltip formatter={tooltipFormatter} />
        </PieChart>
      </ResponsiveContainer>
    );
  }

  if (spec.type === "line") {
    return (
      <ResponsiveContainer width="100%" height={280}>
        <LineChart data={spec.data} margin={{ top: 8, right: 16, bottom: 0, left: 4 }}>
          <CartesianGrid stroke={GRID} vertical={false} />
          <XAxis dataKey={spec.x ?? ""} tick={AXIS} tickFormatter={label} tickLine={false} axisLine={{ stroke: GRID }} minTickGap={16} />
          <YAxis tick={AXIS} tickFormatter={(v: number) => compact(v, measure)} tickLine={false} axisLine={false} width={56} />
          <Tooltip formatter={tooltipFormatter} labelFormatter={label} />
          {legend}
          {seriesKeys.map((key, i) => (
            <Line key={key} type="monotone" dataKey={key} name={key} stroke={SERIES[i % SERIES.length]} strokeWidth={2}
              dot={spec.data.length <= 14 ? { r: 3 } : false} activeDot={{ r: 5, stroke: "#fff", strokeWidth: 2 }} />
          ))}
        </LineChart>
      </ResponsiveContainer>
    );
  }

  const stacked = spec.type === "stacked_bar";
  const horizontal = spec.horizontal;
  return (
    <ResponsiveContainer width="100%" height={height}>
      <BarChart data={spec.data} layout={horizontal ? "vertical" : "horizontal"} margin={{ top: 8, right: 16, bottom: 0, left: 4 }} barCategoryGap="22%">
        <CartesianGrid stroke={GRID} vertical={horizontal} horizontal={!horizontal} />
        {horizontal ? (
          <>
            <XAxis type="number" tick={AXIS} tickFormatter={(v: number) => compact(v, measure)} tickLine={false} axisLine={false} />
            <YAxis type="category" dataKey={spec.x ?? ""} tick={AXIS} tickLine={false} axisLine={false} width={190} tickFormatter={(v: string) => (v.length > 30 ? `${v.slice(0, 29)}…` : v)} />
          </>
        ) : (
          <>
            <XAxis dataKey={spec.x ?? ""} tick={AXIS} tickFormatter={label} tickLine={false} axisLine={{ stroke: GRID }} />
            <YAxis tick={AXIS} tickFormatter={(v: number) => compact(v, measure)} tickLine={false} axisLine={false} width={56} />
          </>
        )}
        <Tooltip formatter={tooltipFormatter} cursor={{ fill: "rgba(20,24,33,0.04)" }} />
        {legend}
        {seriesKeys.map((key, i) => (
          <Bar key={key} dataKey={key} name={key} fill={SERIES[i % SERIES.length]} stackId={stacked ? "s" : undefined} maxBarSize={38}
            radius={stacked ? 0 : horizontal ? [0, 4, 4, 0] : [4, 4, 0, 0]} stroke="#fff" strokeWidth={stacked ? 1 : 0} />
        ))}
      </BarChart>
    </ResponsiveContainer>
  );
}
