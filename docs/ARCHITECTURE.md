# Architecture

## Components
- **Frontend** (`frontend/src`): React 19 + TypeScript (strict) + Tailwind 4 + TanStack Query + Recharts. `api.ts` is the only
  network layer: cookie auth (`credentials: "include"`), a typed error envelope, and an incremental SSE parser that tolerates
  chunk boundaries, CRLF and `: ping` heartbeats. A 401 from a protected call opens the login modal.
- **API** (`backend/app`): FastAPI. Pure-ASGI middleware (request id + structlog, CSRF Origin/Referer guard) so streaming
  responses and disconnect detection are never wrapped by `BaseHTTPMiddleware`. All errors share one envelope
  `{"error": {"code", "message", "request_id"}}`.
- **Database**: one PostgreSQL 16 with `pgvector >= 0.8.0`.
  - `public.*` — users, documents, `document_chunks` (`vector(768)`, generated `tsvector`, HNSW + GIN indexes), query logs, usage counters.
  - `analytics.*` — `regions`, `products`, `orders` (the SQL target), readable by the `docsql_readonly` role only.
- **Two SQLAlchemy async engines** (`app/db.py`): `primary` (app role) and `readonly` (`docsql_readonly`). LLM-generated SQL
  is only ever executed on `readonly`.
- **Health probes** (`app/main.py`): `/livez` is a zero-DB liveness endpoint for load-balancer probes (allowing serverless
  PostgreSQL like Neon to scale to zero without burning compute hours); `/healthz` is the deep readiness check verifying
  both engines and `pgvector >= 0.8.0`.

## Deployment topology (production)
`Browser` → **Cloudflare** (Full strict TLS, Bot Fight Mode, WAF rate limit on `/api/v1/query`, `/api/v1/documents`,
`/api/v1/auth/demo-login`) → **AWS Lightsail Containers** (`ap-southeast-1`, Micro 1 GB RAM, image pulled from private
**Amazon ECR**, `/livez` health check) → **Neon Serverless PostgreSQL 16** (`ap-southeast-1`, `pgvector 0.8.7`, pooled
PgBouncer endpoints with `USE_NULL_POOL=true` and SQL-created least-privilege `docsql_readonly` role) + **Gemini API**.

## Request lifecycle (`POST /api/v1/query`)
1. Auth cookie → user. Quota consumed atomically (plain `429` JSON before any streaming).
2. `query_service.stream_query` starts a **producer task** running the LangGraph and a **generator** that reads a queue.
3. Graph (`agent_graph.py`):
   `router` → `refuse` | `hybrid_rag` | `generate_sql`; `hybrid_rag` → (`DOC`) `synthesize_and_verify` or (`HYBRID`) `generate_sql`;
   `generate_sql` → `validate_and_execute_sql` → (one retry on syntax/schema error) → `synthesize_and_verify`.
4. Stage events come from `graph.astream(..., stream_mode="updates")` node names. The generator emits `: ping` every
   `SSE_PING_INTERVAL_S`, enforces `REQUEST_DEADLINE_S`, and on disconnect cancels the producer (which cancels the in-flight
   LLM/DB call) and logs `CLIENT_DISCONNECTED`. Exactly one `query_logs` row is written per request.
5. `final_answer` is sent only after verification.

## SQL defense in depth
1. `sql_guard.validate_sql` (pure): length ≤ 2000, exactly one statement, root must be `SELECT`, ≤ 400 AST nodes, blocked node
   classes (DML/DDL/`INTO`/`Lock`/`Fetch`/`Lateral`/`Unnest`/`Values`…), no set operations, window functions, parameters or
   `WITH RECURSIVE`; function allowlist **by sqlglot class**; cast-type allowlist (blocks `::regclass`); every `JOIN` needs an
   `ON` containing a column and may not be `CROSS`; tables ∈ {`regions`,`products`,`orders`} in schema `analytics` (≤ 4 refs,
   CTE names may not shadow catalog/analytics names); root `LIMIT` injected/clamped to 100; comments stripped; output is the
   **re-emitted** SQL, which is the only string the executor runs.
2. `sql_executor`: read-only engine → `SET TRANSACTION READ ONLY` first → `statement_timeout`, `lock_timeout`, `search_path` via
   `set_config(..., true)` → `exec_driver_sql` (no bind-parameter parsing). SQLSTATE mapping: `42501`/`25006` → security;
   `57014` → timeout; classes `42`/`22` → retryable syntax/schema.
3. Database role `docsql_readonly`: `SELECT` on `analytics.*` only, `default_transaction_read_only=on`, role-level timeout.

Taxonomy: `SecurityViolationError` → terminal, 0 retries, deterministic “blocked by policy” answer, **no further LLM call**, error
text never shown to the model. `SQLSyntaxOrSchemaError` → 1 retry with the (benign) message. `SQLTimeoutError` → terminal.

## Retrieval
`rag_service.retrieve` embeds the question (`RETRIEVAL_QUERY`) and, in **one transaction**, sets
`hnsw.iterative_scan = 'strict_order'`, runs an independent vector branch (cosine floor applied after fetch) and an independent
full-text branch (tokens `[A-Za-z0-9]{2,}` joined with `|`), then fuses ranks with `rrf_fuse` (k = 60, top 5). Every query is
scoped by `(is_system_seed OR uploaded_by = :uid) AND embedding_model = :model`.

## Ingestion
Bounded upload read (5 MB) → magic bytes → `pypdf` in a thread under a deadline (encrypted rejected, > 15 pages rejected
**before** text extraction, NUL stripped, no text ⇒ `SCANNED_PDF_UNSUPPORTED`) → page-aware chunking (≈ 900 chars, 150 overlap,
never crossing a page) → embeddings **before** any write → one transaction inserting document + chunks, serialized per user by
an advisory lock so the document cap cannot be raced.

## Tracer-bullet reading order
`schemas.py` → `models.py` + `alembic/versions/0001` → `sql_guard.py` → `sql_executor.py` → `rag_service.py` → `agent_graph.py`
→ `query_service.py` → `api/query.py` → `frontend/src/api.ts` → `WorkspacePage.tsx` → `AnswerCard.tsx`.
