export function cn(...classes: (string | false | null | undefined)[]): string {
  return classes.filter(Boolean).join(" ");
}

const MONEY = /(_usd$|revenue|refund|amount|budget|aov|price|received)/i;
const RATIO = /(pct|percent|share|rate|ratio|roi)/i;

export function humanize(column: string): string {
  const text = column.replace(/_usd$/, " (USD)").replace(/_/g, " ");
  return text.charAt(0).toUpperCase() + text.slice(1);
}

export function isMoney(column: string): boolean {
  return MONEY.test(column) && !RATIO.test(column);
}

export function isRatio(column: string): boolean {
  return RATIO.test(column) && !/roi/i.test(column);
}

export function formatValue(value: unknown, column = ""): string {
  if (value === null || value === undefined) return "—";
  if (typeof value === "number") {
    if (isRatio(column) && Math.abs(value) <= 5) return `${(value * 100).toFixed(1)}%`;
    if (isMoney(column)) return value.toLocaleString("en-US", { style: "currency", currency: "USD", maximumFractionDigits: 2 });
    if (Number.isInteger(value)) return value.toLocaleString("en-US");
    return value.toLocaleString("en-US", { maximumFractionDigits: 4 });
  }
  if (typeof value === "string" && /^\d{4}-\d{2}-\d{2}T00:00:00/.test(value)) return value.slice(0, 10);
  if (typeof value === "string" && /^\d{4}-\d{2}-\d{2}T/.test(value)) return value.slice(0, 16).replace("T", " ");
  return String(value);
}

export function compact(value: number, column = ""): string {
  if (isRatio(column) && Math.abs(value) <= 5) return `${(value * 100).toFixed(0)}%`;
  const prefix = isMoney(column) ? "$" : "";
  const abs = Math.abs(value);
  if (abs >= 1e6) return `${prefix}${(value / 1e6).toFixed(1)}M`;
  if (abs >= 1e3) return `${prefix}${(value / 1e3).toFixed(0)}K`;
  return `${prefix}${Number.isInteger(value) ? value : value.toFixed(1)}`;
}

export function ms(value: number | null | undefined): string {
  if (value === null || value === undefined) return "—";
  return value >= 1000 ? `${(value / 1000).toFixed(1)} s` : `${Math.round(value)} ms`;
}

export function shortModel(label: string): string {
  return label.replace(/^openrouter\//, "").replace(/^fake\/demo$/, "offline demo model");
}
