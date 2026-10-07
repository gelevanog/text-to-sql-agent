"use client";

import { useSearchParams } from "next/navigation";
import { Suspense, useCallback, useEffect, useRef, useState } from "react";

import { SendIcon, SparkIcon } from "@/components/icons";
import { TurnView, type Turn } from "@/components/TurnView";
import { api, askStream, type AskBody, type Saved } from "@/lib/api";
import { cn } from "@/lib/format";

export default function Page() {
  return (
    <Suspense fallback={null}>
      <Chat />
    </Suspense>
  );
}

function Chat() {
  const params = useSearchParams();
  const [turns, setTurns] = useState<Turn[]>([]);
  const [conversationId, setConversationId] = useState<string | null>(null);
  const [input, setInput] = useState("");
  const [busy, setBusy] = useState(false);
  const [saved, setSaved] = useState<Saved[]>([]);
  const endRef = useRef<HTMLDivElement>(null);
  const autoAsked = useRef(false);

  const loadSaved = useCallback(() => {
    api.saved().then(setSaved, () => setSaved([]));
  }, []);
  useEffect(loadSaved, [loadSaved]);

  const send = useCallback(
    async (body: AskBody, shown: string) => {
      const id = `${Date.now()}-${Math.random()}`;
      setBusy(true);
      setTurns((t) => [...t, { id, question: shown, steps: [], result: null, error: null }]);
      const update = (fn: (turn: Turn) => Turn) => setTurns((t) => t.map((turn) => (turn.id === id ? fn(turn) : turn)));
      try {
        const result = await askStream(body, (step) => update((turn) => ({ ...turn, steps: [...turn.steps, step] })));
        update((turn) => ({ ...turn, result }));
        setConversationId(result.conversation_id);
      } catch (error) {
        update((turn) => ({ ...turn, error: `Request failed: ${error instanceof Error ? error.message : String(error)}` }));
      } finally {
        setBusy(false);
      }
    },
    [],
  );

  const ask = useCallback(
    (question: string) => {
      const q = question.trim();
      if (!q || busy) return;
      setInput("");
      void send({ question: q, conversation_id: conversationId }, q);
    },
    [busy, conversationId, send],
  );

  useEffect(() => {
    const q = params.get("q");
    if (q && !autoAsked.current) {
      autoAsked.current = true;
      void send({ question: q, conversation_id: null }, q);
    }
  }, [params, send]);

  useEffect(() => {
    const node = endRef.current;
    if (node && typeof node.scrollIntoView === "function") node.scrollIntoView({ behavior: "smooth", block: "end" });
  }, [turns.length]);

  const clarify = (option: { value: string; label: string }, clarificationId: string | null) => {
    void send(
      { question: option.label, conversation_id: conversationId, clarification: clarificationId ? { id: clarificationId, value: option.value } : null },
      option.label,
    );
  };

  const save = (question: string) => {
    api.addSaved(question).then(loadSaved, () => undefined);
  };

  return (
    <div className="flex min-h-screen">
      <section className="flex min-w-0 flex-1 flex-col">
        <header className="flex items-center gap-3 border-b border-line bg-paper/90 px-8 py-4 backdrop-blur">
          <div>
            <h1 className="text-[17px] font-semibold tracking-tight">Ask the data</h1>
            <p className="text-[12.5px] text-zinc-500">Plain-English questions, answered with checked, read-only SQL. Follow-ups keep the context.</p>
          </div>
          {turns.length > 0 && (
            <button type="button" disabled={busy} onClick={() => { setTurns([]); setConversationId(null); }}
              className="ml-auto rounded-md border border-line bg-white px-3 py-1.5 text-[12.5px] font-medium text-zinc-700 hover:border-zinc-400 disabled:opacity-50">
              New conversation
            </button>
          )}
        </header>

        <div className="flex-1 space-y-10 px-8 py-8">
          {turns.length === 0 && (
            <div className="max-w-3xl">
              <div className="flex items-center gap-2 text-[13px] font-medium text-tally"><SparkIcon width={16} height={16} /> Lumora demo database</div>
              <h2 className="mt-2 text-[26px] leading-tight font-semibold tracking-tight">What do you want to know?</h2>
              <p className="mt-2 max-w-2xl text-[14.5px] leading-relaxed text-zinc-600">
                Tally finds the relevant tables, writes SQL, checks that it can only read (no writes, no personal data, no runaway joins),
                runs it as a read-only database role, fixes its own mistakes, and answers with numbers taken from the result.
              </p>
              <div className="mt-6 grid gap-2 sm:grid-cols-2">
                {saved.slice(0, 8).map((s) => (
                  <button key={s.id} type="button" onClick={() => ask(s.question)}
                    className="rounded-lg border border-line bg-white px-4 py-3 text-left text-[13.5px] text-zinc-800 shadow-[0_1px_0_rgba(0,0,0,0.02)] hover:border-zinc-400">
                    {s.question}
                    {s.description && <span className="mt-1 block text-[11.5px] text-zinc-500">{s.description}</span>}
                  </button>
                ))}
              </div>
            </div>
          )}
          {turns.map((turn) => (
            <TurnView key={turn.id} turn={turn} onClarify={clarify} onSave={save} busy={busy} />
          ))}
          <div ref={endRef} />
        </div>

        <form onSubmit={(e) => { e.preventDefault(); ask(input); }} className="sticky bottom-0 border-t border-line bg-paper/95 px-8 py-4 backdrop-blur">
          <div className="flex items-center gap-2 rounded-xl border border-zinc-300 bg-white px-3 py-2 shadow-sm focus-within:border-zinc-500">
            <input value={input} onChange={(e) => setInput(e.target.value)} disabled={busy}
              placeholder={turns.length ? "Ask a follow-up, e.g. “now by month” or “only EU”" : "e.g. What was net revenue by region last calendar quarter vs the one before?"}
              className="min-w-0 flex-1 bg-transparent py-1.5 text-[14.5px] outline-none placeholder:text-zinc-400" />
            <button type="submit" disabled={busy || !input.trim()}
              className={cn("flex items-center gap-1.5 rounded-lg px-3 py-1.5 text-[13px] font-medium text-white", busy || !input.trim() ? "bg-zinc-300" : "bg-ink hover:bg-ink-soft")}>
              {busy ? "Working…" : "Ask"} <SendIcon width={14} height={14} />
            </button>
          </div>
        </form>
      </section>

      <aside className="hidden w-72 shrink-0 border-l border-line bg-white/40 px-5 py-6 xl:block">
        <h3 className="text-[12px] font-semibold tracking-wider text-zinc-500 uppercase">Questions library</h3>
        <ul className="mt-3 space-y-1">
          {saved.map((s) => (
            <li key={s.id} className="group flex items-start gap-1">
              <button type="button" disabled={busy} onClick={() => ask(s.question)}
                className="flex-1 rounded-md px-2 py-1.5 text-left text-[13px] leading-snug text-zinc-700 hover:bg-white hover:text-zinc-900 disabled:opacity-50">
                {s.question}
                {s.category && <span className="ml-1 text-[11px] text-zinc-400">· {s.category}</span>}
              </button>
              <button type="button" title="Remove" onClick={() => { void api.deleteSaved(s.id).then(loadSaved); }}
                className="invisible mt-1 rounded px-1 text-[12px] text-zinc-400 group-hover:visible hover:text-red-600">×</button>
            </li>
          ))}
        </ul>
      </aside>
    </div>
  );
}
