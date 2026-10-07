import { formatValue, humanize } from "@/lib/format";

export function ResultTable({ columns, rows, rowCount, truncated }: { columns: string[]; rows: unknown[][]; rowCount: number; truncated: boolean }) {
  const shown = rows.slice(0, 100);
  return (
    <div className="overflow-hidden rounded-lg border border-line bg-white">
      <div className="max-h-80 overflow-auto">
        <table className="w-full text-left text-[13px]">
          <thead className="sticky top-0 bg-zinc-50 text-[11.5px] text-zinc-500">
            <tr>
              {columns.map((c) => (
                <th key={c} className="border-b border-line px-3 py-2 font-semibold whitespace-nowrap">{humanize(c)}</th>
              ))}
            </tr>
          </thead>
          <tbody>
            {shown.map((row, r) => (
              <tr key={r} className="odd:bg-white even:bg-zinc-50/50">
                {row.map((v, c) => (
                  <td
                    key={c}
                    className={typeof v === "number" ? "px-3 py-1.5 text-right tabular-nums" : "max-w-md px-3 py-1.5 align-top"}
                  >
                    {formatValue(v, columns[c])}
                  </td>
                ))}
              </tr>
            ))}
          </tbody>
        </table>
      </div>
      <div className="border-t border-line px-3 py-1.5 text-[11.5px] text-zinc-500">
        {rowCount.toLocaleString()} row{rowCount === 1 ? "" : "s"}
        {rowCount > shown.length && `, first ${shown.length} shown`}
        {truncated && " · capped by the row limit"}
      </div>
    </div>
  );
}
