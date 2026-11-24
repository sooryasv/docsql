import { useQuery } from "@tanstack/react-query";
import { AlertTriangle, BookOpen, ShieldAlert, X } from "lucide-react";
import { useState } from "react";
import { Bar, BarChart, CartesianGrid, Legend, Line, LineChart, ResponsiveContainer, Tooltip, XAxis, YAxis } from "recharts";
import { getChunk, type ChartSpec, type Citation, type QueryResponse } from "../api";
import { Badge } from "./ui/badge";
import { Button } from "./ui/button";
import { Card } from "./ui/card";

const COLORS = ["#4f46e5", "#0891b2", "#d97706", "#059669"];
const MAX_TABLE_ROWS = 50;
const ROUTE_TONE = { DOC: "indigo", SQL: "green", HYBRID: "amber", OUT_OF_SCOPE: "slate" } as const;

export function AnswerCard({ result }: { result: QueryResponse }) {
  const [open, setOpen] = useState<Citation | null>(null);
  const hasRows = result.executed_sql !== null;
  return (
    <Card className="min-w-0 space-y-4 p-4" aria-live="polite">
      <div className="flex flex-wrap items-center gap-2 text-xs">
        {result.route && <Badge tone={ROUTE_TONE[result.route]}>{result.route}</Badge>}
        <span className="text-slate-500">{result.latency_ms} ms · {result.tokens_used} tokens</span>
        {!result.sql_blocked && !result.refused && (result.citations.length > 0 || result.chart_spec !== null) && (
          <Badge tone="green">Citations & chart verified</Badge>
        )}
      </div>

      {result.sql_blocked && (
        <Banner tone="red" icon={<ShieldAlert className="h-4 w-4" aria-hidden />} title="Blocked by SQL safety policy">
          The generated SQL was rejected before it reached the database. Nothing was executed.
        </Banner>
      )}
      {result.refused && !result.sql_blocked && (
        <Banner tone="amber" icon={<AlertTriangle className="h-4 w-4" aria-hidden />} title="No supporting source">
          This question is outside the loaded documents and the connected database, so no answer was generated.
        </Banner>
      )}

      {!result.sql_blocked && !result.refused && (
        <p className="whitespace-pre-wrap break-words text-sm leading-relaxed">{result.answer_text}</p>
      )}

      {result.citations.length > 0 && (
        <div className="flex flex-wrap items-center gap-2">
          <BookOpen className="h-4 w-4 shrink-0 text-slate-400" aria-hidden />
          {result.citations.map((c) => (
            <button key={c.chunk_id} onClick={() => setOpen(c)}
              className="max-w-full rounded-full bg-indigo-50 px-2.5 py-1 text-left text-xs font-medium text-indigo-700 break-words hover:bg-indigo-100">
              {c.title} · p.{c.page}
            </button>
          ))}
        </div>
      )}

      {result.chart_spec && hasRows && <Chart spec={result.chart_spec} columns={result.sql_columns} rows={result.sql_rows} />}

      {hasRows && (
        <details className="min-w-0 rounded-md border border-slate-200">
          <summary className="cursor-pointer px-3 py-2 text-sm font-medium">Executed SQL & Rows ({result.sql_rows.length})</summary>
          <div className="min-w-0 space-y-3 border-t border-slate-200 p-3">
            <pre className="max-w-full overflow-x-auto rounded bg-slate-900 p-3 text-xs text-slate-100"><code>{result.executed_sql}</code></pre>
            <RowsTable columns={result.sql_columns} rows={result.sql_rows} />
          </div>
        </details>
      )}
      {open && <SourceDrawer citation={open} onClose={() => setOpen(null)} />}
    </Card>
  );
}

function Banner({ tone, icon, title, children }: { tone: "red" | "amber"; icon: React.ReactNode; title: string; children: React.ReactNode }) {
  const cls = tone === "red" ? "border-red-200 bg-red-50 text-red-800" : "border-amber-200 bg-amber-50 text-amber-900";
  return (
    <div role="status" className={`flex gap-2 rounded-md border p-3 text-sm ${cls}`}>
      <span className="mt-0.5">{icon}</span>
      <div><p className="font-medium">{title}</p><p>{children}</p></div>
    </div>
  );
}

function Chart({ spec, columns, rows }: { spec: ChartSpec; columns: string[]; rows: unknown[][] }) {
  const data = rows.map((row) => Object.fromEntries(columns.map((c, i) => [c, row[i]])));
  const axes = (
    <>
      <CartesianGrid strokeDasharray="3 3" />
      <XAxis dataKey={spec.x} tick={{ fontSize: 12 }} />
      <YAxis tick={{ fontSize: 12 }} width={70} />
      <Tooltip />
      {spec.y.length > 1 && <Legend />}
    </>
  );
  return (
    <div className="min-w-0 w-full overflow-hidden" role="img" aria-label={spec.title || `${spec.type} chart of ${spec.y.join(", ")} by ${spec.x}`}>
      {spec.title && <p className="mb-1 text-xs font-medium text-slate-600">{spec.title}</p>}
      <div className="h-64 w-full">
        <ResponsiveContainer>
          {spec.type === "line" ? (
            <LineChart data={data}>{axes}{spec.y.map((y, i) => <Line key={y} dataKey={y} stroke={COLORS[i % COLORS.length]} />)}</LineChart>
          ) : (
            <BarChart data={data}>{axes}{spec.y.map((y, i) => <Bar key={y} dataKey={y} fill={COLORS[i % COLORS.length]} />)}</BarChart>
          )}
        </ResponsiveContainer>
      </div>
    </div>
  );
}

function RowsTable({ columns, rows }: { columns: string[]; rows: unknown[][] }) {
  return (
    <div className="max-h-64 overflow-auto">
      <table className="w-full text-left text-xs">
        <thead className="sticky top-0 bg-slate-100"><tr>{columns.map((c) => <th key={c} className="px-2 py-1 font-medium">{c}</th>)}</tr></thead>
        <tbody>
          {rows.slice(0, MAX_TABLE_ROWS).map((row, i) => (
            <tr key={i} className="border-t border-slate-100">
              {row.map((cell, j) => <td key={j} className="px-2 py-1 font-mono">{cell === null ? "NULL" : String(cell)}</td>)}
            </tr>
          ))}
        </tbody>
      </table>
      {rows.length > MAX_TABLE_ROWS && <p className="p-2 text-xs text-slate-500">Showing first {MAX_TABLE_ROWS} of {rows.length} rows.</p>}
    </div>
  );
}

function SourceDrawer({ citation, onClose }: { citation: Citation; onClose: () => void }) {
  const chunk = useQuery({
    queryKey: ["chunk", citation.document_id, citation.chunk_id],
    queryFn: () => getChunk(citation.document_id, citation.chunk_id),
  });
  return (
    <aside className="fixed inset-y-0 right-0 z-40 w-full max-w-md overflow-y-auto border-l border-slate-200 bg-white p-4 shadow-xl" aria-label="Source passage">
      <div className="mb-3 flex items-start justify-between gap-2">
        <div><p className="font-medium">{citation.title}</p><p className="text-xs text-slate-500">Page {citation.page}</p></div>
        <Button variant="ghost" size="icon" onClick={onClose} aria-label="Close source"><X className="h-4 w-4" /></Button>
      </div>
      {chunk.isLoading && <p className="text-sm text-slate-500">Loading passage…</p>}
      {chunk.isError && <p className="text-sm text-red-600">Could not load this passage.</p>}
      {chunk.data && <p className="whitespace-pre-wrap rounded bg-amber-50 p-3 text-sm leading-relaxed">{chunk.data.content}</p>}
    </aside>
  );
}
