// Types and calls for the Tally API. The browser talks to the API directly (CORS); its URL is inlined at build time.

export const API_URL = process.env.NEXT_PUBLIC_API_URL ?? "http://localhost:8000";

export type Step = { kind: string; title: string; detail: Record<string, unknown>; ms: number };

export type ChartSpec = {
  type: "kpi" | "line" | "bar" | "grouped_bar" | "stacked_bar" | "pie" | "table";
  reason: string;
  x: string | null;
  y: string[];
  series: string[];
  horizontal: boolean;
  data: Record<string, unknown>[];
  title: string;
};

export type Clarification = {
  id: string | null;
  question: string;
  options: { value: string; label: string }[];
  source: "semantic_layer" | "model";
};

export type Blocked = { layer: string; reasons: string[]; sql?: string };

export type Violation = { code: string; message: string; hard: boolean };

export type AgentResult = {
  conversation_id: string;
  question: string;
  asked: string;
  status: "answered" | "clarification" | "blocked" | "refused" | "error";
  sql: string;
  expanded_sql: string;
  executed_sql: string;
  views: string[];
  plan: string;
  explanation: string;
  assumptions: string[];
  columns: string[];
  column_types: string[];
  rows: unknown[][];
  row_count: number;
  truncated: boolean;
  chart: ChartSpec | null;
  answer: string;
  answer_check: { ok: boolean; numbers: string[]; unsupported: string[] } | null;
  answer_source: string;
  clarification: Clarification | null;
  blocked: Blocked | null;
  error: string;
  retrieval: { tables?: string[]; views?: string[]; metrics?: string[]; joins?: string[]; mode?: string };
  validation: { ok: boolean; violations: Violation[]; limit_applied: number | null; tables: string[] } | null;
  plan_cost: number | null;
  steps: Step[];
  resolved: Record<string, string>;
  llm_calls: number;
  corrections: number;
  model: string;
  served_models: string[];
  total_ms: number;
  llm_ms: number;
  db_ms: number;
};

export type Health = {
  status: string;
  model: string;
  provider: string;
  free_only: boolean;
  semantic_layer: boolean;
  retrieval_mode: string;
  clarify_policy: string;
  region_scope: string;
  today: string;
  tables: number;
  views: number;
  company: string;
};

export type Saved = { id: number; question: string; description: string; category: string };

export type AuditRecord = {
  id: number;
  ts: string;
  conversation_id: string | null;
  question: string;
  status: string;
  sql: string;
  row_count: number;
  duration_ms: number;
  model: string;
  llm_calls: number;
  corrections: number;
  violations: string[];
  region_scope: string;
};

export type Column = {
  name: string;
  type: string;
  nullable: boolean;
  primary_key: boolean;
  references: string | null;
  description: string;
  pii: boolean;
  samples: string[];
};

export type TableInfo = {
  name: string;
  columns: Column[];
  row_count: number;
  description: string;
  synonyms: string[];
  is_view: boolean;
  sql: string;
};

export type Ambiguity = {
  id: string;
  description: string;
  triggers: string[];
  resolved_by: string[];
  question: string;
  options: { value: string; label: string; clarifies: string }[];
  default: string;
};

export type SchemaInfo = {
  company: string;
  description: string;
  reporting: Record<string, unknown>;
  rules: string[];
  synonyms: Record<string, string[]>;
  metrics: Record<string, { description: string; synonyms: string[]; view: string | null; sql: string }>;
  ambiguities: Ambiguity[];
  tables: TableInfo[];
  views: TableInfo[];
  joins: [string, string][];
};

export type RunSummary = {
  items: number;
  execution_accuracy: {
    correct: number;
    total: number;
    accuracy: number | null;
    by_category: Record<string, { correct: number; total: number; accuracy: number | null }>;
  };
  valid_sql: { answered: number; total: number; rate: number | null };
  self_correction: { first_attempt_correct: number; first_attempt_accuracy: number | null; rescued: number };
  clarification: {
    ambiguous_total: number;
    asked_on_ambiguous: number;
    asked_on_answerable: number;
    recall: number | null;
    precision: number | null;
    by_source: Record<string, number>;
  };
  safety: {
    unsafe_total: number;
    blocked_or_refused: number;
    unsafe_executed: number;
    by_layer: Record<string, number>;
    not_blocked: { id: string; status: string }[];
  };
  answer_faithfulness: {
    answers: number;
    first_draft_ok: number;
    ok_after_retry: number;
    template_fallback: number;
    first_draft_rate: number | null;
  };
  latency_ms: { p50: number | null; p95: number | null; answerable_p50: number | null; answerable_p95: number | null };
  llm_calls: { total: number; mean: number | null; max: number | null };
};

export type RunItem = {
  id: string;
  category: string;
  expect: string;
  status: string;
  correct?: boolean;
  clarified?: boolean;
  blocked?: boolean;
  rescued?: boolean;
  unsafe_executed?: boolean;
  llm_calls: number;
  total_ms: number;
  blocked_layer?: string | null;
  answer_source?: string;
  question: string;
};

export type Run = { name: string; date: string; config: Record<string, unknown>; summary: RunSummary; items: RunItem[] };

export type ComparisonRow = {
  name: string;
  label: string;
  model: string;
  items: number;
  accuracy: number | null;
  correct: number;
  total: number;
  valid_sql: number | null;
  calls_mean: number | null;
  latency_p50: number | null;
  latency_p95: number | null;
  prompt_tokens_median: number | null;
  note?: string;
};

export type EvalData = {
  runs: Run[];
  comparison?: { subset_size: number; rows: ComparisonRow[] };
  calls_summary?: { total_requests: number; all_model_ids_free: boolean; requested_models: string[]; served_models: Record<string, number> };
};

async function json<T>(path: string, init?: RequestInit): Promise<T> {
  const res = await fetch(`${API_URL}${path}`, { ...init, headers: { "Content-Type": "application/json", ...init?.headers } });
  if (!res.ok) throw new Error(`${res.status} ${res.statusText}`);
  return (await res.json()) as T;
}

export const api = {
  health: () => json<Health>("/health"),
  saved: () => json<Saved[]>("/api/saved"),
  addSaved: (question: string) => json<Saved>("/api/saved", { method: "POST", body: JSON.stringify({ question }) }),
  deleteSaved: (id: number) => fetch(`${API_URL}/api/saved/${id}`, { method: "DELETE" }),
  schema: () => json<SchemaInfo>("/api/schema"),
  audit: () => json<AuditRecord[]>("/api/audit?limit=300"),
  evaluation: () => json<EvalData>("/api/eval"),
};

export type AskBody = { question: string; conversation_id?: string | null; clarification?: { id: string; value: string } | null };

/** POST /api/ask/stream and call back for each server-sent event. */
export async function askStream(
  body: AskBody,
  onStep: (step: Step) => void,
  signal?: AbortSignal,
): Promise<AgentResult> {
  const res = await fetch(`${API_URL}/api/ask/stream`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
    signal,
  });
  if (!res.ok || !res.body) throw new Error(`${res.status} ${res.statusText}`);
  const reader = res.body.getReader();
  const decoder = new TextDecoder();
  let buffer = "";
  let done: AgentResult | null = null;
  for (;;) {
    const chunk = await reader.read();
    if (chunk.done) break;
    buffer += decoder.decode(chunk.value, { stream: true });
    let index = buffer.indexOf("\n\n");
    while (index !== -1) {
      const raw = buffer.slice(0, index);
      buffer = buffer.slice(index + 2);
      const event = /^event: (.*)$/m.exec(raw)?.[1];
      const data = /^data: (.*)$/m.exec(raw)?.[1];
      if (event && data) {
        const payload: unknown = JSON.parse(data);
        if (event === "step") onStep(payload as Step);
        else if (event === "done") done = payload as AgentResult;
        else if (event === "error") throw new Error((payload as { error: string }).error);
      }
      index = buffer.indexOf("\n\n");
    }
  }
  if (!done) throw new Error("the stream ended without a result");
  return done;
}
