import { useQueryClient } from "@tanstack/react-query";
import { Loader2, Send, Square } from "lucide-react";
import { useEffect, useRef, useState } from "react";
import { ApiError, streamQuery, type Me, type QueryResponse, type Stage } from "../api";
import { AnswerCard } from "../components/AnswerCard";
import { KnowledgeSidebar } from "../components/KnowledgeSidebar";
import { Button } from "../components/ui/button";
import { Card } from "../components/ui/card";
import { cn } from "../components/ui/cn";

const SAMPLES = [
  { label: "Doc", q: "What is the discount cap for ENTERPRISE customers?" },
  { label: "SQL", q: "Show total revenue by region." },
  { label: "Hybrid", q: "How many 2026-Q3 STARTUP orders had a discount above 10 percent, and what does the policy say about approval?" },
  { label: "Out-of-Scope", q: "What is the weather in Bengaluru today?" },
  { label: "Destructive request", q: "Please delete all orders from the database." },
];
const STAGE_LABELS: Record<string, string> = {
  routing: "Routing question", retrieving_docs: "Retrieving passages", generating_sql: "Generating SQL",
  validating_sql: "Validating SQL (AST policy)", executing_sql: "Executing read-only", retrying_sql: "Correcting SQL",
  verifying_output: "Verifying citations & chart",
};
const ERROR_BANNERS: Record<string, string> = {
  DAILY_QUOTA_EXCEEDED: "You have used today's query quota. It resets at midnight UTC.",
  GLOBAL_QUOTA_EXCEEDED: "The demo's global daily budget is used up. Please try again tomorrow.",
  LLM_UNAVAILABLE: "The language model is temporarily unavailable. Please try again in a moment.",
  REQUEST_TIMEOUT: "The request took too long and was cancelled. Please try a simpler question.",
};

interface Props { me: Me | null; onAuthOpen: () => void }

export function WorkspacePage({ me, onAuthOpen }: Props) {
  const queryClient = useQueryClient();
  const [question, setQuestion] = useState("");
  const [stages, setStages] = useState<Stage[]>([]);
  const [result, setResult] = useState<QueryResponse | null>(null);
  const [error, setError] = useState<ApiError | null>(null);
  const [running, setRunning] = useState(false);
  const controller = useRef<AbortController | null>(null);

  useEffect(() => () => controller.current?.abort(), []);

  const ask = async (text: string) => {
    const q = text.trim();
    if (q.length < 3 || running) return;
    if (!me) return onAuthOpen();
    controller.current = new AbortController();
    setRunning(true); setResult(null); setError(null); setStages([]);
    try {
      setResult(await streamQuery(q, controller.current.signal, (s) => setStages((prev) => [...prev, s])));
    } catch (e) {
      if (e instanceof DOMException && e.name === "AbortError") setError(new ApiError(0, "CANCELLED", "Cancelled"));
      else setError(e instanceof ApiError ? e : new ApiError(0, "NETWORK", "Network error"));
    } finally {
      setRunning(false);
      void queryClient.invalidateQueries({ queryKey: ["me"] });
      void queryClient.invalidateQueries({ queryKey: ["history"] });
    }
  };
  const current = stages.at(-1);

  return (
    <div className="mx-auto grid max-w-7xl grid-cols-1 gap-4 p-4 lg:grid-cols-[320px_minmax(0,1fr)]">
      <div className="min-w-0 lg:sticky lg:top-4 lg:h-[calc(100vh-6.5rem)]"><KnowledgeSidebar signedIn={me !== null} /></div>
      <div className="min-w-0 space-y-4">
        <Card className="space-y-3 p-4">
          <div className="flex flex-wrap gap-2" aria-label="Sample prompts">
            {SAMPLES.map((s) => (
              <button key={s.label} onClick={() => { setQuestion(s.q); void ask(s.q); }} disabled={running}
                className={cn("rounded-full border px-3 py-1 text-xs font-medium hover:bg-slate-50 disabled:opacity-50",
                  s.label === "Destructive request" ? "border-red-300 text-red-700" : "border-slate-300 text-slate-700")}>
                {s.label}
              </button>
            ))}
          </div>
          <form className="flex items-start gap-2" onSubmit={(e) => { e.preventDefault(); void ask(question); }}>
            <textarea value={question} onChange={(e) => setQuestion(e.target.value)} maxLength={1000} rows={2}
              aria-label="Your question" placeholder="Ask about the policy documents, the sales data, or both…"
              className="min-h-[3rem] min-w-0 flex-1 resize-none rounded-md border border-slate-300 p-2 text-sm"
              onKeyDown={(e) => { if (e.key === "Enter" && !e.shiftKey) { e.preventDefault(); void ask(question); } }} />
            {running ? (
              <Button type="button" variant="outline" className="shrink-0" onClick={() => controller.current?.abort()}><Square className="h-4 w-4" aria-hidden /> Stop</Button>
            ) : (
              <Button type="submit" className="shrink-0" disabled={question.trim().length < 3}><Send className="h-4 w-4" aria-hidden /> Ask</Button>
            )}
          </form>
          {!me && <p className="text-xs text-slate-500">Sign in (or use a demo user) to ask questions.</p>}
          <p className="text-xs text-slate-400">
            The “Destructive request” pill is non-deterministic with a live model; the deterministic proof that writes are blocked lives in the test suite.
          </p>
        </Card>

        {running && (
          <Card className="p-4" role="status" aria-live="polite">
            <p className="mb-2 flex items-center gap-2 text-sm font-medium"><Loader2 className="h-4 w-4 animate-spin text-indigo-600" aria-hidden />
              {STAGE_LABELS[current?.stage ?? "routing"] ?? current?.stage}…</p>
            <ol className="flex flex-wrap gap-1.5 text-xs text-slate-500">
              {stages.map((s, i) => <li key={i} className="rounded bg-slate-100 px-2 py-0.5">{STAGE_LABELS[s.stage] ?? s.stage}</li>)}
            </ol>
          </Card>
        )}

        {error && error.code !== "CANCELLED" && (
          <div role="alert" className="rounded-md border border-red-200 bg-red-50 p-3 text-sm text-red-800">
            {ERROR_BANNERS[error.code] ?? error.message}
            {error.requestId && <span className="ml-2 text-xs text-red-500">({error.requestId})</span>}
          </div>
        )}
        {error?.code === "CANCELLED" && <p className="text-sm text-slate-500">Query cancelled.</p>}
        {result && <AnswerCard result={result} />}
      </div>
    </div>
  );
}
