import { useQuery } from "@tanstack/react-query";
import { useState } from "react";
import { getHistory, type Me, type Route } from "../api";
import { Badge } from "../components/ui/badge";
import { Button } from "../components/ui/button";
import { Card } from "../components/ui/card";

const ROUTE_TONE = { DOC: "indigo", SQL: "green", HYBRID: "amber", OUT_OF_SCOPE: "slate" } as const;

export function HistoryPage({ me, onAuthOpen }: { me: Me | null; onAuthOpen: () => void }) {
  const [page, setPage] = useState(1);
  const [expanded, setExpanded] = useState<Record<string, boolean>>({});
  const history = useQuery({ queryKey: ["history", page], queryFn: () => getHistory(page), enabled: me !== null });

  if (!me) {
    return (
      <div className="mx-auto max-w-3xl p-8 text-center">
        <p className="mb-3 text-slate-600">Sign in to see your query history.</p>
        <Button onClick={onAuthOpen}>Sign in</Button>
      </div>
    );
  }
  const data = history.data;
  const pages = data ? Math.max(1, Math.ceil(data.total / data.page_size)) : 1;

  return (
    <div className="mx-auto max-w-4xl space-y-3 p-4">
      <h1 className="text-lg font-semibold">Query history</h1>
      {history.isLoading && <p className="text-sm text-slate-500">Loading…</p>}
      {history.isError && <p className="text-sm text-red-600">Could not load history.</p>}
      {data && data.items.length === 0 && <p className="text-sm text-slate-500">No queries yet. Ask something in the workspace.</p>}
      {data?.items.map((item) => {
        const isOpen = Boolean(expanded[item.id]);
        return (
          <Card key={item.id} className="min-w-0 space-y-2 p-3">
            <div className="flex flex-wrap items-center gap-2">
              {item.route && <Badge tone={ROUTE_TONE[item.route as Route]}>{item.route}</Badge>}
              {item.sql_blocked && <Badge tone="red">SQL blocked</Badge>}
              {item.refused && <Badge tone="amber">Refused</Badge>}
              {item.error_code && <Badge tone="red">{item.error_code}</Badge>}
              {item.sql_retry_count > 0 && <Badge>retried SQL</Badge>}
              <span className="ml-auto text-xs text-slate-500">
                {item.latency_ms} ms · {new Date(item.created_at).toLocaleString()}
              </span>
            </div>
            <p className="text-sm font-medium">{item.question}</p>
            {item.answer_text && (
              <div>
                <p className={`${isOpen ? "whitespace-pre-wrap" : "line-clamp-3"} text-sm text-slate-600`}>
                  {item.answer_text}
                </p>
                {(item.answer_text.length > 200 || item.answer_text.includes("\n")) && (
                  <button
                    type="button"
                    onClick={() => setExpanded((prev) => ({ ...prev, [item.id]: !prev[item.id] }))}
                    className="mt-1 text-xs font-medium text-indigo-600 hover:underline"
                  >
                    {isOpen ? "Show less" : "Show full answer"}
                  </button>
                )}
              </div>
            )}
            {item.generated_sql && (
              <details className="min-w-0 text-xs">
                <summary className="cursor-pointer text-slate-500">Generated SQL</summary>
                <pre className="mt-1 max-w-full overflow-x-auto rounded bg-slate-900 p-2 text-slate-100"><code>{item.generated_sql}</code></pre>
              </details>
            )}
          </Card>
        );
      })}
      {data && data.total > data.page_size && (
        <div className="flex items-center justify-between pt-2">
          <Button variant="outline" size="sm" disabled={page <= 1} onClick={() => setPage(page - 1)}>Previous</Button>
          <span className="text-sm text-slate-500">Page {page} of {pages}</span>
          <Button variant="outline" size="sm" disabled={page >= pages} onClick={() => setPage(page + 1)}>Next</Button>
        </div>
      )}
    </div>
  );
}
