import { useQuery } from "@tanstack/react-query";
import { ApiError, getMetrics, type Me } from "../api";
import { Badge } from "../components/ui/badge";
import { Card } from "../components/ui/card";

function Stat({ label, value, hint }: { label: string; value: string | number; hint?: string }) {
  return (
    <Card className="p-4">
      <p className="text-xs uppercase tracking-wide text-slate-500">{label}</p>
      <p className="mt-1 text-2xl font-semibold">{value}</p>
      {hint && <p className="text-xs text-slate-400">{hint}</p>}
    </Card>
  );
}

const pct = (v: number) => `${(v * 100).toFixed(1)}%`;

export function MetricsPage({ me }: { me: Me | null }) {
  const isAdmin = me?.user.role === "ADMIN";
  const metrics = useQuery({ queryKey: ["metrics"], queryFn: getMetrics, enabled: isAdmin, retry: false });

  if (!me || !isAdmin || (metrics.error instanceof ApiError && metrics.error.status === 403)) {
    return (
      <div className="mx-auto max-w-xl p-8 text-center" role="alert">
        <p className="text-lg font-semibold">403 · Admin only</p>
        <p className="text-sm text-slate-500">Telemetry is available to ADMIN users. Try the “Demo admin” sign-in.</p>
      </div>
    );
  }
  const m = metrics.data;
  if (metrics.isLoading || !m) return <p className="p-8 text-center text-sm text-slate-500">Loading…</p>;
  const total = Math.max(1, Object.values(m.route_mix).reduce((a, b) => a + b, 0));

  return (
    <div className="mx-auto max-w-5xl space-y-4 p-4">
      <h1 className="text-lg font-semibold">Telemetry</h1>
      <div className="grid gap-3 sm:grid-cols-2 lg:grid-cols-4">
        <Stat label="Total queries" value={m.total_queries} />
        <Stat label="SQL blocked" value={m.sql_blocked_count} hint="Rejected by the AST guard" />
        <Stat label="Refusal rate" value={pct(m.refusal_rate)} />
        <Stat label="Error rate" value={pct(m.error_rate)} />
        <Stat label="Latency p50" value={`${Math.round(m.latency_p50_ms)} ms`} />
        <Stat label="Latency p95" value={`${Math.round(m.latency_p95_ms)} ms`} />
        <Stat label="Tokens used" value={m.total_tokens.toLocaleString()} />
        <Stat label="Global quota today" value={m.quota_mode === "enforced" ? `${m.global_used_today} / ${m.global_limit}` : "off"}
          hint={m.quota_mode === "enforced" ? "All users combined" : "Disabled in mock mode"} />
      </div>
      <Card className="space-y-3 p-4">
        <div className="flex items-center gap-2">
          <h2 className="text-sm font-semibold">Route mix</h2>
          <Badge tone={m.quota_mode === "enforced" ? "green" : "amber"}>quota {m.quota_mode === "enforced" ? "enforced" : "disabled (mock)"}</Badge>
        </div>
        {Object.entries(m.route_mix).length === 0 && <p className="text-sm text-slate-500">No queries yet.</p>}
        {Object.entries(m.route_mix).map(([route, count]) => (
          <div key={route} className="text-sm">
            <div className="mb-1 flex justify-between"><span>{route}</span><span className="text-slate-500">{count}</span></div>
            <div className="h-2 rounded bg-slate-100"><div className="h-2 rounded bg-indigo-500" style={{ width: `${(count / total) * 100}%` }} /></div>
          </div>
        ))}
      </Card>
    </div>
  );
}
