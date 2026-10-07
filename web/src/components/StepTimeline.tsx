import type { Step } from "@/lib/api";
import { cn, ms } from "@/lib/format";

const LABELS: Record<string, string> = {
  retrieval: "Schema",
  sql: "SQL",
  validation: "Validation",
  cost: "Cost guard",
  execution: "Run",
  result: "Result",
  chart: "Chart",
  answer: "Answer",
  correction: "Retry",
  clarification: "Clarify",
  blocked: "Blocked",
  error: "Error",
};

function tone(step: Step): string {
  const failed =
    step.kind === "blocked" ||
    step.kind === "error" ||
    (step.kind === "validation" && step.detail.ok === false) ||
    (step.kind === "cost" && step.title.includes("expensive")) ||
    (step.kind === "execution" && step.title.includes("fail"));
  if (failed) return "border-red-200 bg-red-50 text-red-800";
  if (step.kind === "correction" || (step.kind === "sql" && step.title.includes("corrected"))) return "border-amber-200 bg-amber-50 text-amber-800";
  if (step.kind === "clarification") return "border-amber-200 bg-amber-50 text-amber-800";
  return "border-line bg-white text-zinc-700";
}

export function StepTimeline({ steps, running }: { steps: Step[]; running: boolean }) {
  const visible = steps.filter((s) => s.kind !== "result");
  return (
    <ol className="flex flex-wrap items-center gap-1.5">
      {visible.map((step, i) => (
        <li key={i} className="flex items-center gap-1.5">
          <span title={step.title} className={cn("inline-flex max-w-[22rem] items-center gap-1.5 rounded-full border px-2.5 py-0.5 text-[12px]", tone(step))}>
            <span className="font-semibold">{LABELS[step.kind] ?? step.kind}</span>
            <span className="truncate text-current/80">{step.title}</span>
            {step.ms > 0 && <span className="whitespace-nowrap text-current/50 tabular-nums">{ms(step.ms)}</span>}
          </span>
          {i < visible.length - 1 && <span className="text-zinc-300">›</span>}
        </li>
      ))}
      {running && (
        <li className="inline-flex items-center gap-1.5 rounded-full border border-dashed border-zinc-300 px-2.5 py-0.5 text-[12px] text-zinc-500">
          <span className="size-1.5 animate-pulse rounded-full bg-tally" /> working
        </li>
      )}
    </ol>
  );
}
