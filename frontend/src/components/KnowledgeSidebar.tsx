import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { FileText, Trash2, UploadCloud } from "lucide-react";
import { useRef, useState, type DragEvent } from "react";
import { ApiError, deleteDocument, getSchema, listDocuments, uploadDocument } from "../api";
import { Badge } from "./ui/badge";
import { Button } from "./ui/button";
import { Card } from "./ui/card";
import { cn } from "./ui/cn";

export function KnowledgeSidebar({ signedIn }: { signedIn: boolean }) {
  const [tab, setTab] = useState<"kb" | "schema">("kb");
  return (
    <Card className="flex h-full flex-col overflow-hidden">
      <div className="flex border-b border-slate-200 text-sm font-medium" role="tablist">
        {([["kb", "Knowledge Base"], ["schema", "Connected SQL Schema"]] as const).map(([id, label]) => (
          <button key={id} role="tab" aria-selected={tab === id} onClick={() => setTab(id)}
            className={cn("flex-1 px-2 py-2.5", tab === id ? "border-b-2 border-indigo-600 text-indigo-700" : "text-slate-500")}>
            {label}
          </button>
        ))}
      </div>
      <div className="flex-1 overflow-y-auto p-3">
        {!signedIn ? <p className="text-sm text-slate-500">Sign in to see documents and the connected schema.</p>
          : tab === "kb" ? <KnowledgeBase /> : <SchemaView />}
      </div>
    </Card>
  );
}

function KnowledgeBase() {
  const queryClient = useQueryClient();
  const input = useRef<HTMLInputElement>(null);
  const [dragging, setDragging] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const docs = useQuery({ queryKey: ["documents"], queryFn: listDocuments });
  const refresh = () => queryClient.invalidateQueries({ queryKey: ["documents"] });
  const onError = (e: unknown) => setError(e instanceof ApiError ? e.message : "Upload failed");

  const upload = useMutation({ mutationFn: uploadDocument, onSuccess: () => { setError(null); void refresh(); }, onError });
  const remove = useMutation({ mutationFn: deleteDocument, onSuccess: () => void refresh(), onError });

  const pick = (files: FileList | null) => {
    const file = files?.[0];
    if (!file) return;
    if (!file.name.toLowerCase().endsWith(".pdf")) return setError("Only PDF files are supported");
    if (file.size > 5 * 1024 * 1024) return setError("File exceeds the 5 MB limit");
    upload.mutate(file);
  };
  const onDrop = (e: DragEvent) => {
    e.preventDefault();
    setDragging(false);
    pick(e.dataTransfer.files);
  };

  return (
    <div className="space-y-3">
      <div
        onDragOver={(e) => { e.preventDefault(); setDragging(true); }}
        onDragLeave={() => setDragging(false)}
        onDrop={onDrop}
        className={cn("rounded-lg border-2 border-dashed p-4 text-center text-sm", dragging ? "border-indigo-500 bg-indigo-50" : "border-slate-300")}
      >
        <UploadCloud className="mx-auto mb-1 h-6 w-6 text-slate-400" aria-hidden />
        <p className="text-slate-600">Drop a PDF here (max 5 MB, 15 pages)</p>
        <Button variant="outline" size="sm" className="mt-2" disabled={upload.isPending} onClick={() => input.current?.click()}>
          {upload.isPending ? "Indexing…" : "Choose file"}
        </Button>
        <input ref={input} type="file" accept="application/pdf,.pdf" className="hidden" aria-label="Upload PDF"
          onChange={(e) => { pick(e.target.files); e.target.value = ""; }} />
      </div>
      {error && <p role="alert" className="text-sm text-red-600">{error}</p>}
      {docs.isLoading && <p className="text-sm text-slate-500">Loading…</p>}
      {docs.isError && <p className="text-sm text-red-600">Could not load documents.</p>}
      <ul className="space-y-2">
        {docs.data?.map((doc) => (
          <li key={doc.id} className="flex items-start gap-2 rounded-md border border-slate-200 p-2">
            <FileText className="mt-0.5 h-4 w-4 shrink-0 text-indigo-500" aria-hidden />
            <div className="min-w-0 flex-1">
              <p className="truncate text-sm font-medium" title={doc.title}>{doc.title}</p>
              <p className="text-xs text-slate-500">{doc.page_count} pages · {doc.chunk_count} chunks</p>
              <Badge tone={doc.is_system_seed ? "indigo" : "green"} className="mt-1">{doc.is_system_seed ? "Seed" : "Yours"}</Badge>
            </div>
            {!doc.is_system_seed && (
              <Button variant="ghost" size="icon" aria-label={`Delete ${doc.title}`} disabled={remove.isPending}
                onClick={() => { if (window.confirm(`Delete “${doc.title}”?`)) remove.mutate(doc.id); }}>
                <Trash2 className="h-4 w-4 text-red-500" aria-hidden />
              </Button>
            )}
          </li>
        ))}
      </ul>
    </div>
  );
}

function SchemaView() {
  const schema = useQuery({ queryKey: ["schema"], queryFn: getSchema });
  if (schema.isLoading) return <p className="text-sm text-slate-500">Loading…</p>;
  if (schema.isError) return <p className="text-sm text-red-600">Could not load the schema.</p>;
  return (
    <div className="space-y-3">
      <p className="text-xs text-slate-500">Read-only <code>analytics</code> schema. Generated SQL can only touch these tables.</p>
      {schema.data?.tables.map((table) => (
        <details key={table.name} className="rounded-md border border-slate-200 p-2" open>
          <summary className="text-sm font-medium">
            {table.name} <span className="font-normal text-slate-500">({table.row_count} rows)</span>
          </summary>
          <ul className="mt-1 space-y-0.5 text-xs">
            {table.columns.map((c) => (
              <li key={c.name} className="flex justify-between gap-2">
                <span className="font-mono">{c.name}</span><span className="text-slate-500">{c.data_type}</span>
              </li>
            ))}
          </ul>
        </details>
      ))}
    </div>
  );
}
