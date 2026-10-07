"use client";

import Link from "next/link";
import { usePathname } from "next/navigation";
import { useEffect, useState } from "react";

import { api, type Health } from "@/lib/api";
import { cn, shortModel } from "@/lib/format";
import { AskIcon, AuditIcon, EvalIcon, LogoMark, SchemaIcon, ShieldIcon } from "./icons";

const NAV = [
  { href: "/", label: "Ask", icon: AskIcon },
  { href: "/schema", label: "Schema & semantic layer", icon: SchemaIcon },
  { href: "/eval", label: "Evaluation", icon: EvalIcon },
  { href: "/audit", label: "Audit log", icon: AuditIcon },
];

export function Sidebar() {
  const pathname = usePathname();
  const [health, setHealth] = useState<Health | null>(null);
  useEffect(() => {
    api.health().then(setHealth, () => setHealth(null));
  }, []);
  return (
    <aside className="sticky top-0 flex h-screen w-64 shrink-0 flex-col bg-ink text-zinc-300">
      <Link href="/" className="flex items-center gap-2.5 px-5 pt-6 pb-7">
        <span className="flex size-9 items-center justify-center rounded-lg bg-white/5 text-zinc-100 ring-1 ring-white/10">
          <LogoMark width={22} height={22} />
        </span>
        <span>
          <span className="block text-[18px] font-semibold tracking-tight text-white">Tally</span>
          <span className="block text-[11px] text-zinc-500">questions in, checked SQL out</span>
        </span>
      </Link>
      <nav className="flex flex-col gap-0.5 px-3">
        {NAV.map(({ href, label, icon: Icon }) => (
          <Link
            key={href}
            href={href}
            className={cn(
              "flex items-center gap-3 rounded-md px-3 py-2 text-[13.5px] font-medium transition",
              pathname === href ? "bg-white/10 text-white" : "text-zinc-400 hover:bg-white/5 hover:text-zinc-100",
            )}
          >
            <Icon width={17} height={17} />
            {label}
          </Link>
        ))}
      </nav>
      <div className="mt-auto space-y-3 border-t border-white/10 px-5 py-5 text-[12px]">
        {health ? (
          <>
            <div>
              <div className="text-zinc-500">Database</div>
              <div className="font-medium text-zinc-200">
                {health.company || "your database"} · {health.tables} tables, {health.views} views
              </div>
            </div>
            <div>
              <div className="text-zinc-500">Model</div>
              <div className="font-medium break-all text-zinc-200">{shortModel(health.model)}</div>
            </div>
            <div className="flex flex-wrap gap-1.5">
              <span className="inline-flex items-center gap-1 rounded bg-emerald-400/10 px-1.5 py-0.5 text-emerald-300">
                <ShieldIcon width={12} height={12} /> read-only
              </span>
              {health.free_only && health.provider === "openrouter" && (
                <span className="rounded bg-white/5 px-1.5 py-0.5 text-zinc-300">free models only</span>
              )}
              <span className="rounded bg-white/5 px-1.5 py-0.5 text-zinc-300">today {health.today}</span>
            </div>
          </>
        ) : (
          <div className="text-zinc-500">API not reachable</div>
        )}
      </div>
    </aside>
  );
}
