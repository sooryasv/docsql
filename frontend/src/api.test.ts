import { afterEach, describe, expect, it, vi } from "vitest";
import { ApiError, SSEParser, getMe, listDocuments, setUnauthorizedHandler, streamQuery, type QueryResponse } from "./api";

const enc = new TextEncoder();
const final: QueryResponse = {
  query_id: "q1", route: "SQL", answer_text: "ok", citations: [], executed_sql: "SELECT 1", sql_columns: [],
  sql_rows: [], sql_blocked: false, refused: false, chart_spec: null, latency_ms: 5, tokens_used: 3,
};

function sseResponse(chunks: string[], status = 200): Response {
  const stream = new ReadableStream<Uint8Array>({
    start(controller) {
      for (const c of chunks) controller.enqueue(enc.encode(c));
      controller.close();
    },
  });
  return new Response(stream, { status, headers: { "Content-Type": "text/event-stream" } });
}

afterEach(() => vi.unstubAllGlobals());

describe("SSEParser", () => {
  it("handles events split across arbitrary chunk boundaries", () => {
    const p = new SSEParser();
    expect(p.push("event: sta")).toEqual([]);
    expect(p.push('ge\ndata: {"stage":"routing"}\n')).toEqual([]);
    expect(p.push("\n")).toEqual([{ event: "stage", data: '{"stage":"routing"}' }]);
  });

  it("ignores ': ping' heartbeats, including ones between events", () => {
    const p = new SSEParser();
    const out = p.push(': ping\n\nevent: stage\ndata: 1\n\n: ping\n\nevent: final_answer\ndata: 2\n\n');
    expect(out.map((m) => m.event)).toEqual(["stage", "final_answer"]);
  });

  it("supports CRLF line endings and multi-line data", () => {
    const out = new SSEParser().push("event: x\r\ndata: a\r\ndata: b\r\n\r\n");
    expect(out).toEqual([{ event: "x", data: "a\nb" }]);
  });

  it("emits several events from one chunk and keeps the partial tail buffered", () => {
    const p = new SSEParser();
    const out = p.push("event: a\ndata: 1\n\nevent: b\ndata: 2\n\nevent: c\ndata: 3");
    expect(out).toHaveLength(2);
    expect(p.push("\n\n")).toEqual([{ event: "c", data: "3" }]);
  });
});

describe("streamQuery", () => {
  it("reports stages and resolves with the final answer", async () => {
    vi.stubGlobal("fetch", vi.fn().mockResolvedValue(sseResponse([
      'event: stage\ndata: {"stage":"routing","detail":"x"}\n\n: ping\n\n',
      `event: stage\ndata: {"stage":"executing_sql","detail":"y"}\n\nevent: final_answer\ndata: ${JSON.stringify(final)}\n\n`,
    ])));
    const stages: string[] = [];
    const result = await streamQuery("q", new AbortController().signal, (s) => stages.push(s.stage));
    expect(stages).toEqual(["routing", "executing_sql"]);
    expect(result.answer_text).toBe("ok");
  });

  it("turns an `error` event into an ApiError", async () => {
    const body = JSON.stringify({ error: { code: "LLM_UNAVAILABLE", message: "down", request_id: "req_1" } });
    vi.stubGlobal("fetch", vi.fn().mockResolvedValue(sseResponse([`event: error\ndata: ${body}\n\n`])));
    await expect(streamQuery("q", new AbortController().signal, () => {})).rejects.toMatchObject({
      code: "LLM_UNAVAILABLE",
      requestId: "req_1",
    });
  });

  it("surfaces a pre-stream 429 as an ApiError with the envelope code", async () => {
    const body = JSON.stringify({ error: { code: "DAILY_QUOTA_EXCEEDED", message: "used up", request_id: "r" } });
    vi.stubGlobal("fetch", vi.fn().mockResolvedValue(new Response(body, { status: 429 })));
    const err = await streamQuery("q", new AbortController().signal, () => {}).catch((e: unknown) => e);
    expect(err).toBeInstanceOf(ApiError);
    expect((err as ApiError).status).toBe(429);
    expect((err as ApiError).code).toBe("DAILY_QUOTA_EXCEEDED");
  });

  it("rejects when the stream ends without a final answer", async () => {
    vi.stubGlobal("fetch", vi.fn().mockResolvedValue(sseResponse(['event: stage\ndata: {"stage":"routing","detail":""}\n\n'])));
    await expect(streamQuery("q", new AbortController().signal, () => {})).rejects.toMatchObject({ code: "STREAM_ENDED" });
  });

  it("passes the abort signal to fetch so a cancel aborts the request", async () => {
    const fetchMock = vi.fn((_url: string, init: RequestInit) =>
      new Promise<Response>((_resolve, reject) => {
        init.signal?.addEventListener("abort", () => reject(new DOMException("Aborted", "AbortError")));
      }),
    );
    vi.stubGlobal("fetch", fetchMock);
    const controller = new AbortController();
    const pending = streamQuery("q", controller.signal, () => {});
    controller.abort();
    await expect(pending).rejects.toMatchObject({ name: "AbortError" });
    expect(fetchMock.mock.calls[0][1].credentials).toBe("include");
  });
});

describe("request wrapper", () => {
  it("does not trigger the unauthorized handler on 401 from /auth/me", async () => {
    const handler = vi.fn();
    setUnauthorizedHandler(handler);
    const unauth = JSON.stringify({ error: { code: "UNAUTHENTICATED", message: "no", request_id: "r" } });
    vi.stubGlobal("fetch", vi.fn().mockResolvedValue(new Response(unauth, { status: 401 })));
    await expect(getMe()).rejects.toBeInstanceOf(ApiError);
    expect(handler).not.toHaveBeenCalled();
  });

  it("calls the unauthorized handler on 401 from a protected endpoint", async () => {
    const handler = vi.fn();
    setUnauthorizedHandler(handler);
    const unauth = JSON.stringify({ error: { code: "UNAUTHENTICATED", message: "no", request_id: "r" } });
    vi.stubGlobal("fetch", vi.fn().mockResolvedValue(new Response(unauth, { status: 401 })));
    await expect(listDocuments()).rejects.toBeInstanceOf(ApiError);
    expect(handler).toHaveBeenCalledTimes(1);
  });
});
