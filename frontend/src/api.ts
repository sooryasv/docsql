// Single network layer: cookie auth, typed endpoints, error envelope, and the SSE stream reader.

export type Route = "DOC" | "SQL" | "HYBRID" | "OUT_OF_SCOPE";
export interface User { id: string; email: string; full_name: string; role: "ANALYST" | "ADMIN" }
export interface Me { user: User; quota_limit: number | null; quota_remaining: number | null; demo_tools_enabled: boolean }
export interface DocumentOut {
  id: string; title: string; filename: string; page_count: number; chunk_count: number;
  embedding_model: string; is_system_seed: boolean; created_at: string;
}
export interface ChunkOut { id: number; document_id: string; title: string; page_number: number; content: string }
export interface Citation { chunk_id: number; document_id: string; title: string; page: number }
export interface ChartSpec { type: "bar" | "line"; x: string; y: string[]; title: string }
export interface QueryResponse {
  query_id: string; route: Route | null; answer_text: string; citations: Citation[];
  executed_sql: string | null; sql_columns: string[]; sql_rows: unknown[][]; sql_blocked: boolean;
  refused: boolean; chart_spec: ChartSpec | null; latency_ms: number; tokens_used: number;
}
export interface TableInfo { name: string; row_count: number; columns: { name: string; data_type: string; nullable: boolean }[] }
export interface SchemaOut { schema_name: string; tables: TableInfo[] }
export interface HistoryItem {
  id: string; question: string; route: Route | null; generated_sql: string | null; sql_blocked: boolean;
  sql_retry_count: number; refused: boolean; error_code: string | null; answer_text: string;
  latency_ms: number; tokens_used: number; created_at: string;
}
export interface HistoryPage { items: HistoryItem[]; page: number; page_size: number; total: number }
export interface Metrics {
  total_queries: number; route_mix: Record<string, number>; sql_blocked_count: number; refusal_rate: number;
  error_rate: number; latency_p50_ms: number; latency_p95_ms: number; total_tokens: number;
  quota_mode: "enforced" | "disabled_mock"; global_used_today: number; global_limit: number;
}
export type Stage = { stage: string; detail: string };

export class ApiError extends Error {
  constructor(public status: number, public code: string, message: string, public requestId = "") {
    super(message);
  }
}

let onUnauthorized: () => void = () => {};
export function setUnauthorizedHandler(handler: () => void): void {
  onUnauthorized = handler;
}

async function toApiError(res: Response): Promise<ApiError> {
  try {
    const body = (await res.json()) as { error?: { code: string; message: string; request_id: string } };
    if (body.error) return new ApiError(res.status, body.error.code, body.error.message, body.error.request_id);
  } catch {
    /* non-JSON error body: fall through to a generic error */
  }
  return new ApiError(res.status, `HTTP_${res.status}`, res.statusText || "Request failed");
}

async function request<T>(path: string, init: RequestInit = {}): Promise<T> {
  const headers = new Headers(init.headers);
  if (init.body && !(init.body instanceof FormData)) headers.set("Content-Type", "application/json");
  const res = await fetch(`/api/v1${path}`, { ...init, headers, credentials: "include" });
  if (!res.ok) {
    const err = await toApiError(res);
    if (res.status === 401 && path !== "/auth/me" && path !== "/auth/login") onUnauthorized();
    throw err;
  }
  return res.status === 204 ? (undefined as T) : ((await res.json()) as T);
}

const post = <T>(path: string, body?: unknown) =>
  request<T>(path, { method: "POST", body: body === undefined ? undefined : JSON.stringify(body) });

export const getMe = () => request<Me>("/auth/me");
export const login = (email: string, password: string) => post<User>("/auth/login", { email, password });
export const register = (email: string, password: string, full_name: string) =>
  post<User>("/auth/register", { email, password, full_name });
export const demoLogin = (role: "ANALYST" | "ADMIN") => post<User>("/auth/demo-login", { role });
export const logout = () => post<void>("/auth/logout");
export const listDocuments = () => request<DocumentOut[]>("/documents");
export const uploadDocument = (file: File) => {
  const form = new FormData();
  form.append("file", file);
  return request<DocumentOut>("/documents", { method: "POST", body: form });
};
export const deleteDocument = (id: string) => request<void>(`/documents/${id}`, { method: "DELETE" });
export const getChunk = (documentId: string, chunkId: number) =>
  request<ChunkOut>(`/documents/${documentId}/chunks/${chunkId}`);
export const getSchema = () => request<SchemaOut>("/schema");
export const getHistory = (page: number) => request<HistoryPage>(`/history?page=${page}&page_size=10`);
export const getMetrics = () => request<Metrics>("/metrics");

// ---------- SSE ----------
export interface SSEMessage { event: string; data: string }

/** Incremental parser: tolerates arbitrary chunk boundaries, CRLF, multi-line data and `: ping` comments. */
export class SSEParser {
  private buffer = "";

  push(chunk: string): SSEMessage[] {
    this.buffer += chunk.replace(/\r\n/g, "\n");
    const messages: SSEMessage[] = [];
    let boundary = this.buffer.indexOf("\n\n");
    while (boundary !== -1) {
      const block = this.buffer.slice(0, boundary);
      this.buffer = this.buffer.slice(boundary + 2);
      const message = this.parseBlock(block);
      if (message) messages.push(message);
      boundary = this.buffer.indexOf("\n\n");
    }
    return messages;
  }

  private parseBlock(block: string): SSEMessage | null {
    let event = "message";
    const data: string[] = [];
    for (const line of block.split("\n")) {
      if (line === "" || line.startsWith(":")) continue; // heartbeat/comment
      const colon = line.indexOf(":");
      const field = colon === -1 ? line : line.slice(0, colon);
      const value = colon === -1 ? "" : line.slice(colon + 1).replace(/^ /, "");
      if (field === "event") event = value;
      else if (field === "data") data.push(value);
    }
    return data.length ? { event, data: data.join("\n") } : null;
  }
}

/** Streams a question. Resolves with the verified final answer; rejects with ApiError on `error` events. */
export async function streamQuery(
  question: string,
  signal: AbortSignal,
  onStage: (stage: Stage) => void,
): Promise<QueryResponse> {
  const res = await fetch("/api/v1/query", {
    method: "POST",
    credentials: "include",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ question }),
    signal,
  });
  if (!res.ok || !res.body) {
    const err = await toApiError(res);
    if (res.status === 401) onUnauthorized();
    throw err;
  }
  const reader = res.body.getReader();
  const decoder = new TextDecoder();
  const parser = new SSEParser();
  let final: QueryResponse | null = null;
  for (;;) {
    const { done, value } = await reader.read();
    if (done) break;
    for (const message of parser.push(decoder.decode(value, { stream: true }))) {
      if (message.event === "stage") onStage(JSON.parse(message.data) as Stage);
      else if (message.event === "final_answer") final = JSON.parse(message.data) as QueryResponse;
      else if (message.event === "error") {
        const { error } = JSON.parse(message.data) as { error: { code: string; message: string; request_id: string } };
        throw new ApiError(res.status, error.code, error.message, error.request_id);
      }
    }
  }
  if (!final) throw new ApiError(res.status, "STREAM_ENDED", "The stream ended before a final answer arrived");
  return final;
}
