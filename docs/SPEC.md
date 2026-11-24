# DocSQL — Technical Specification (v2, standalone)

> Title: **DocSQL — Guardrailed Document + SQL Question Answering on a Single PostgreSQL**
> One-liner: an analyst asks a question in English; a bounded LangGraph pipeline decides whether to answer from PDFs,
> from a governed read-only SQL schema, or both, and returns an answer whose citations and chart columns are
> deterministically verified before the user ever sees them.

---

## 1. Vision, Honest Claims, Honest Non-Claims

**What this project demonstrates (each backed by a test or a measured eval, never by adjectives):**

| # | Claim | Evidence artifact |
|---|---|---|
| C1 | LLM-generated SQL cannot write, escape the `analytics` schema, call non-allowlisted functions, or run unbounded. Enforced in depth: AST allowlist + separate read-only DB role + read-only transaction + timeout + row cap. | `tests/test_sql_security.py` (25 adversarial payloads + direct role-bypass proof) |
| C2 | Multi-tenant vector search returns the right user's chunks even when HNSW post-filtering would starve the result. | `tests/test_rag_graph_sse.py` recall test **with a negative control** |
| C3 | Hybrid retrieval (vector + full-text, RRF) with a calibrated similarity floor and a deterministic grounded refusal. | `evals/` live report |
| C4 | Citations are *referentially* sound: every cited chunk ID was actually retrieved for this request; chart columns exist in the executed result. | `tests/test_rag_graph_sse.py` + eval metric |
| C5 | Failure behaviour is designed: bounded retry, timeouts, quota, graceful LLM outage, client disconnect. | `tests/test_rag_graph_sse.py`, runbook |
| C6 | Quality is measured, not asserted: routing accuracy, SQL execution accuracy, retrieval hit@5, refusal accuracy. | `docs/EVAL_REPORT.md` (live run, model + date recorded) |

**Explicit non-claims (also printed in README "Known Limitations"):** it does not verify that answer *text* is semantically
supported by the cited passage; nonce delimiters do not "prevent" prompt injection; PDF extraction is text-only
(no OCR, no tables/layout understanding); the eval set is small (n=55) and single-run; HNSW is not needed at this data
size — it is used to demonstrate correct filtered-ANN behaviour, and the ADR says so.

---

## 2. Users & RBAC

| Role | Can do | Cannot do |
|---|---|---|
| `ANALYST` | register/login/demo-login; list seed + **own** PDFs; upload (≤5 MB, ≤15 pages, ≤`MAX_DOCS_PER_USER`) and delete **own** PDFs; read chunks of visible docs; inspect `analytics` schema; run queries; view **own** history | delete seed docs (`403 SEED_DOCUMENT_PROTECTED`); see/delete others' docs (**`404`**, not 403 — no enumeration); see others' history; view `/metrics` (`403`) |
| `ADMIN` | everything an analyst can + `GET /metrics` | create via `/auth/register` (never); delete others' docs (**intentionally not provided** — a publicly reachable demo-admin must not hold destructive power) |

Authorization = **role gate** (metrics) + **ownership predicate on every document/log query** (the real RBAC story; IDOR tests required).

---

## 3. User Journeys
1. **Entry (`/`)**: login/register modal, or **1-click Demo Analyst / Demo Admin** (`ENABLE_DEMO_TOOLS`). Demo users have unusable password hashes; demo-login issues a cookie by email lookup only.
2. **Upload**: PDF → validated → text per page (thread) → page-aware chunks → embeddings (bounded concurrency) → single atomic insert. Duplicate (same user + SHA-256) → `409 DUPLICATE_DOCUMENT`.
3. **Ask (`POST /api/v1/query`, SSE over fetch)**: stage events stream live; `final_answer` is emitted only after verification.
4. **Verify**: citation badge `[Doc Title, p.X]` → source drawer showing the exact stored chunk; "Executed SQL & Rows" inspector; auto Recharts chart when the verified `chart_spec` exists; explicit banners for *blocked SQL*, *refusal*, *quota*, *LLM unavailable*.
5. **Audit**: `/history` (own), `/metrics` (admin).

---

## 4. In Scope
Cookie-JWT auth + CSRF Origin guard + demo login · seed corpus (2 PDFs + 500-row analytics DB) · user-isolated PDF upload ·
dual-engine DB isolation · 6-node LangGraph with one bounded retry · AST SQL guard · hybrid RAG · structured synthesis with
referential-integrity verification · Stage-Progress SSE · atomic quotas · query audit log + admin metrics · live eval harness ·
multi-stage Docker (same-origin) · GitHub Actions CI · production docs.

## 5. Non-Goals
Redis/Celery/external vector DB · raw token streaming · OCR · multi-turn memory · user-supplied DB connection strings ·
MCP server / scraping agent · `localStorage` tokens · admin deletion of others' docs · window functions in generated SQL ·
LLM-as-judge evals · password reset / email verification · in-app rate-limiting middleware (handled by atomic DB quotas and
Cloudflare edge WAF) · WebSockets · i18n · dark-mode toggle.

---

## 6. Runtime, Cloud Portability, Environment

- **Same-origin:** dev → Vite proxies `/api`, `/healthz` to `:8000`; prod → FastAPI serves `frontend/dist` + `/api/v1/*` on one origin.
- **`app/db.py`** builds two async engines: `primary_engine` (`DATABASE_URL`, app role, `public.*`, pool 5) and
  `readonly_engine` (`ANALYTICS_READONLY_DATABASE_URL`, `docsql_readonly`, pool 3). Both: normalize `postgres://`/`postgresql://` →
  `postgresql+asyncpg://`, strip `sslmode=require`, `connect_args={"statement_cache_size":0,"ssl":"require"}` when SSL on
  (else `{"statement_cache_size":0}`), `pool_pre_ping=True`, `USE_NULL_POOL=true` → `NullPool`.
- **Container:** `uvicorn app.main:app --host 0.0.0.0 --port ${PORT:-8000} --proxy-headers --forwarded-allow-ips="*"`.
  `alembic upgrade head` = release step (never in `lifespan`). Seed runs in `lifespan` only if `RUN_SEED_ON_STARTUP=true`
  (`false` in tests); seed is idempotent.
- **LLM:** `EMBEDDING_DIM=768` everywhere. Chat and embedding model IDs are environment-driven and verified against
  Google's official Gemini model/deprecation pages (retired model IDs are blocked by CI).
- **`.env.example`:**
  `DATABASE_URL, ANALYTICS_READONLY_DATABASE_URL, TEST_DATABASE_URL, TEST_ANALYTICS_READONLY_DATABASE_URL, JWT_SECRET, JWT_EXPIRE_MINUTES=60,
  COOKIE_SECURE=false, ALLOWED_ORIGINS=http://localhost:5173,http://localhost:8000, USE_NULL_POOL=false, ENABLE_DEMO_TOOLS=true,
  RUN_SEED_ON_STARTUP=true, MOCK_LLM=true, GEMINI_API_KEY=, GEMINI_CHAT_MODEL=gemini-3.5-flash-lite, GEMINI_EMBED_MODEL=gemini-embedding-001,
  MIN_COSINE_SIMILARITY=0.50 (calibrated via live eval sweep), DAILY_QUERY_LIMIT=20, GLOBAL_DAILY_QUERY_LIMIT=200, MAX_DOCS_PER_USER=5,
  SQL_TIMEOUT_MS=3000, SQL_MAX_ROWS=100, LLM_TIMEOUT_S=20, EMBED_TIMEOUT_S=15, REQUEST_DEADLINE_S=45`

---

## 7. Data Model (8 tables, Alembic `0001_initial_schema.py`)

```sql
CREATE EXTENSION IF NOT EXISTS vector;        -- must be >= 0.8.0 (checked by /healthz and a startup assertion in tests)
CREATE SCHEMA IF NOT EXISTS analytics;

-- ===== public (app + RAG) =====
CREATE TABLE public.users (
  id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  email VARCHAR(255) NOT NULL UNIQUE,
  full_name VARCHAR(120) NOT NULL,
  password_hash VARCHAR(255) NOT NULL,
  role VARCHAR(20) NOT NULL CHECK (role IN ('ANALYST','ADMIN')),
  created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  CONSTRAINT ck_users_email_lowercase CHECK (email = LOWER(email))
);

CREATE TABLE public.documents (
  id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  uploaded_by UUID REFERENCES public.users(id) ON DELETE CASCADE,
  title VARCHAR(200) NOT NULL,
  filename VARCHAR(255) NOT NULL,
  file_sha256 CHAR(64) NOT NULL,
  page_count INTEGER NOT NULL CHECK (page_count > 0 AND page_count <= 15),
  chunk_count INTEGER NOT NULL DEFAULT 0 CHECK (chunk_count >= 0),
  embedding_model VARCHAR(80) NOT NULL,            -- 'mock-hash-768' | 'gemini-embedding-001'; prevents silently mixing vector spaces
  is_system_seed BOOLEAN NOT NULL DEFAULT FALSE,
  created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  CONSTRAINT ck_documents_owner_or_seed CHECK (is_system_seed OR uploaded_by IS NOT NULL)
);
CREATE INDEX ix_documents_owner_seed ON public.documents (is_system_seed, uploaded_by, created_at DESC);
CREATE UNIQUE INDEX uq_documents_seed_filename ON public.documents (filename) WHERE is_system_seed;
CREATE UNIQUE INDEX uq_documents_owner_sha ON public.documents (uploaded_by, file_sha256) WHERE uploaded_by IS NOT NULL;

CREATE TABLE public.document_chunks (
  id BIGSERIAL PRIMARY KEY,
  document_id UUID NOT NULL REFERENCES public.documents(id) ON DELETE CASCADE,
  page_number INTEGER NOT NULL CHECK (page_number > 0),
  chunk_index INTEGER NOT NULL CHECK (chunk_index >= 0),
  content TEXT NOT NULL,
  embedding vector(768) NOT NULL,
  search_tsv TSVECTOR GENERATED ALWAYS AS (to_tsvector('english', content)) STORED,
  CONSTRAINT uq_chunks_doc_index UNIQUE (document_id, chunk_index)
);
CREATE INDEX ix_chunks_doc_page ON public.document_chunks (document_id, page_number);
CREATE INDEX ix_chunks_tsv_gin ON public.document_chunks USING GIN (search_tsv);
CREATE INDEX ix_chunks_embedding_hnsw ON public.document_chunks USING hnsw (embedding vector_cosine_ops) WITH (m = 16, ef_construction = 64);

CREATE TABLE public.query_logs (
  id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  user_id UUID NOT NULL REFERENCES public.users(id) ON DELETE CASCADE,
  question TEXT NOT NULL,
  route VARCHAR(20) CHECK (route IN ('DOC','SQL','HYBRID','OUT_OF_SCOPE')),   -- NULL if failed before routing
  generated_sql TEXT,
  sql_blocked BOOLEAN NOT NULL DEFAULT FALSE,
  sql_retry_count INTEGER NOT NULL DEFAULT 0 CHECK (sql_retry_count BETWEEN 0 AND 1),
  refused BOOLEAN NOT NULL DEFAULT FALSE,
  error_code VARCHAR(40),                                                      -- e.g. LLM_UNAVAILABLE, QUERY_TIMEOUT
  cited_chunk_ids JSONB NOT NULL DEFAULT '[]'::jsonb,
  chart_spec JSONB,
  answer_text TEXT NOT NULL DEFAULT '',
  latency_ms INTEGER NOT NULL CHECK (latency_ms >= 0),
  tokens_used INTEGER NOT NULL DEFAULT 0 CHECK (tokens_used >= 0),
  created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
CREATE INDEX ix_query_logs_user_created ON public.query_logs (user_id, created_at DESC);
CREATE INDEX ix_query_logs_created_route ON public.query_logs (created_at DESC, route);

CREATE TABLE public.usage_counters (                       -- atomic quota (see §8.9)
  scope VARCHAR(80) NOT NULL,                              -- 'user:<uuid>' | 'global'
  usage_date DATE NOT NULL,
  used INTEGER NOT NULL DEFAULT 0 CHECK (used >= 0),
  PRIMARY KEY (scope, usage_date)
);

-- ===== analytics (read-only SQL target) =====
CREATE TABLE analytics.regions (
  id SERIAL PRIMARY KEY, region_code VARCHAR(16) NOT NULL UNIQUE, city VARCHAR(80) NOT NULL, state VARCHAR(80) NOT NULL,
  zone VARCHAR(32) NOT NULL CHECK (zone IN ('SOUTH','WEST','NORTH','EAST')),
  q3_target_inr BIGINT NOT NULL CHECK (q3_target_inr > 0));
CREATE TABLE analytics.products (
  id SERIAL PRIMARY KEY, sku VARCHAR(32) NOT NULL UNIQUE, name VARCHAR(140) NOT NULL,
  category VARCHAR(60) NOT NULL CHECK (category IN ('CLOUD_OPS','DATA_PLATFORM','SECURITY','AI_SUITE')),
  unit_price_inr INTEGER NOT NULL CHECK (unit_price_inr > 0),
  max_discount_pct INTEGER NOT NULL CHECK (max_discount_pct BETWEEN 0 AND 50));
CREATE TABLE analytics.orders (
  id SERIAL PRIMARY KEY, order_code VARCHAR(32) NOT NULL UNIQUE,
  region_id INTEGER NOT NULL REFERENCES analytics.regions(id) ON DELETE RESTRICT,
  product_id INTEGER NOT NULL REFERENCES analytics.products(id) ON DELETE RESTRICT,
  order_date DATE NOT NULL,
  quarter VARCHAR(10) NOT NULL CHECK (quarter IN ('2026-Q1','2026-Q2','2026-Q3','2026-Q4')),
  quantity INTEGER NOT NULL CHECK (quantity > 0),
  discount_pct INTEGER NOT NULL CHECK (discount_pct BETWEEN 0 AND 50),
  revenue_inr BIGINT NOT NULL CHECK (revenue_inr > 0),
  customer_tier VARCHAR(20) NOT NULL CHECK (customer_tier IN ('STARTUP','MID_MARKET','ENTERPRISE')));
CREATE INDEX ix_orders_quarter_region ON analytics.orders (quarter, region_id);
CREATE INDEX ix_orders_product_date ON analytics.orders (product_id, order_date);
```

**Role setup** (`docker/init-roles.sql` for dev/test/CI; same statements in `docs/runbook.md` for Neon/RDS). The migration only GRANTs if the role exists.
```sql
CREATE ROLE docsql_readonly LOGIN PASSWORD '...';     -- idempotent via DO $$ IF NOT EXISTS $$
ALTER ROLE docsql_readonly SET default_transaction_read_only = on;
ALTER ROLE docsql_readonly SET statement_timeout = '3000ms';
ALTER ROLE docsql_readonly SET search_path = analytics;
REVOKE ALL ON SCHEMA public FROM docsql_readonly;  REVOKE ALL ON ALL TABLES IN SCHEMA public FROM docsql_readonly;
GRANT USAGE ON SCHEMA analytics TO docsql_readonly;  GRANT SELECT ON ALL TABLES IN SCHEMA analytics TO docsql_readonly;
ALTER DEFAULT PRIVILEGES IN SCHEMA analytics GRANT SELECT ON TABLES TO docsql_readonly;
```
Honest note for ADR-002: Postgres cannot stop any role from reading `pg_catalog`; privileges granted to `PUBLIC` are not removed by
`REVOKE ... FROM docsql_readonly`. That residual surface is exactly why the AST layer exists and rejects `pg_catalog`/`information_schema`.

---

## 8. Component Specifications

### 8.1 LangGraph (`agent_graph.py`)
State: Pydantic/TypedDict `GraphState` (question, user_id, route, chunks, sql, sql_error, rows, columns, retries, blocked, refused, answer, citations, chart_spec, tokens, stage log).
Nodes: `router` → (`refuse` | `hybrid_rag` | `generate_sql`); `hybrid_rag` → (`synthesize_and_verify` if DOC | `generate_sql` if HYBRID); `generate_sql` → `validate_and_execute_sql`;
`validate_and_execute_sql` → conditional: `SQLSyntaxOrSchemaError` and `retries < 1` → `generate_sql` (error text fed back, `retries += 1`);
success, `SecurityViolationError`, or timeout → `synthesize_and_verify`.
- Router: LLM structured output `{route, reason}`; unknown/invalid → `OUT_OF_SCOPE`. `OUT_OF_SCOPE` → deterministic refusal, **no further LLM call**.
- `generate_sql` receives **question + analytics schema description only** — never chunks (HYBRID isolation; tested with a hostile PDF).
- Blocked SQL → deterministic "blocked by policy" answer, **no LLM synthesis call**, error text never fed back.
- Stage events are derived from `graph.astream(..., stream_mode="updates")` node names; `retrying_sql` emitted on the loop edge.
- Architectural trade-off (see ADR-004): the graph earns its place via a conditional edge, a bounded cycle, and typed shared state; a plain async function would also work (trade-off: extra dependency and typing complexity).

### 8.2 SQL Guard (`sql_guard.py`) — pure function `validate_sql(sql) -> ValidatedSQL(sql, tables, limit)`; raises `SecurityViolationError(reason)`
Parse `sqlglot.parse(sql, read="postgres")`. Reject if: input > 2,000 chars; parse error → `SQLSyntaxOrSchemaError` (benign, retryable);
statements ≠ 1 or contains `None`; root not `exp.Select`; AST node count > 400. Then walk the **entire** AST and reject:
- **Write/DDL/command nodes:** Insert, Update, Delete, Drop, Alter, Create, Command, Grant, Copy, Truncate, Into, Merge, Lock (`FOR UPDATE`), Fetch.
- **Set operations** (Union/Intersect/Except) and **`exp.Window`** (v1 decision, ADR).
- **Parameters/session refs:** `exp.Placeholder`, `exp.Parameter`, `exp.SessionParameter`.
- **`WITH RECURSIVE`**; **comma-joins** (`FROM` with >1 source); `CROSS JOIN`; any `exp.Join` without `ON`; **literal-only/tautological `ON`** (`ON TRUE`, `ON 1=1` — predicate must contain ≥1 `exp.Column`).
- **Tables:** every physical `exp.Table` (excluding declared CTE aliases) must have `this` = plain `exp.Identifier` (rejects table functions like `generate_series`), name ∈ `{regions, products, orders}`, schema ∈ `{"", "analytics"}`. `pg_catalog`/`information_schema`/`public` → reject. Total physical table references ≤ 4.
- **Functions — allowlist by sqlglot CLASS, not name string:** `Count, Sum, Avg, Min, Max, Coalesce, Round, DateTrunc, TimestampTrunc, Extract, Lower, Upper, Abs, Nullif, Cast, Case, And, Or` (plus non-function operators/columns). Every other `exp.Func` or `exp.Anonymous` → reject. (Must pass: `COUNT(*)`, `COUNT(DISTINCT x)`, `x::numeric`, `CASE WHEN`, `EXTRACT(month FROM d)`, `AND`/`OR`.)
- **LIMIT:** operate on the **root** select only; inject `LIMIT 100` if missing; clamp if `> SQL_MAX_ROWS`; non-literal limit → reject. Subquery limits are irrelevant to the cap.
- Output `ast.sql(dialect="postgres")` — **the executor runs only this re-emitted string** (kills parser-differential tricks).
- Residual risk documented in ADR-002: a weak-but-column-bearing join predicate on a self-join is bounded by the 4-table cap, the 3 s timeout and the 100-row cap.

### 8.3 SQL Executor (`sql_executor.py`)
On `readonly_engine`, one connection, one explicit transaction. **First statement:** `SET TRANSACTION READ ONLY` (transaction-scoped and PgBouncer-safe).
Then `SELECT set_config('statement_timeout', :ms, true), set_config('search_path','analytics', true), set_config('lock_timeout','1000ms', true)`.
Execute the re-emitted SQL through `exec_driver_sql` (avoids SQLAlchemy `text()` parsing `:x` inside string literals — covered by a test). Map errors:
`UndefinedColumn/UndefinedTable/SyntaxError` → `SQLSyntaxOrSchemaError`; `QueryCanceled` → `SQLTimeoutError` (terminal, not "blocked"); `InsufficientPrivilege`/`ReadOnlySqlTransaction` → `SecurityViolationError`.
Also exposes `describe_schema()` (column metadata + row counts for `GET /schema` and the SQL prompt).

### 8.4 Hybrid Retrieval (`rag_service.py`)
In one transaction: `set_config('hnsw.iterative_scan','strict_order', true)`. Visibility predicate on **every** query:
`(d.is_system_seed OR d.uploaded_by = :uid) AND d.embedding_model = :current_model`.
- **Vector branch:** `ORDER BY c.embedding <=> :qvec LIMIT 20`, then apply cosine floor `1 - dist >= MIN_COSINE_SIMILARITY` after fetching (keeps the ANN order clause index-friendly).
- **FTS branch:** natural-language questions are **OR-ed**, not AND-ed (`websearch_to_tsquery`/`plainto_tsquery` AND every word and starve the branch): tokens = `[A-Za-z0-9]{2,}` from the question, de-duplicated, joined with ` | ` and passed as a bind param to `to_tsquery('english', :q)` (sanitized charset ⇒ no tsquery-syntax injection); `ts_rank_cd > 0`, `LIMIT 20`.
- Branches are **independent** (never ANDed). `rrf_fuse(vec_ids, fts_ids, k=60, top_n=5)` is a pure function with unit tests. 0 survivors → deterministic grounded refusal (DOC) / doc-part marked "no supporting passage" (HYBRID).
- Multi-user filtered HNSW recall is verified against real PostgreSQL + `pgvector >= 0.8.0` with a negative control (`enable_seqscan=off`, `enable_sort=off`, `hnsw.iterative_scan=off` vs `strict_order`).
- Query embedding uses `RETRIEVAL_QUERY`; chunk embeddings used `RETRIEVAL_DOCUMENT`.

### 8.5 Ingestion (`document_service.py`)
Read upload in bounded chunks (reject at 5 MB + 1 byte → `413 FILE_TOO_LARGE`; never trust `Content-Length`); magic bytes `%PDF-` → else `422 INVALID_PDF`;
encrypted → `422 INVALID_PDF`; page count > 15 → `422 TOO_MANY_PAGES` (checked **before** text extraction); user already has `MAX_DOCS_PER_USER` → `409 DOCUMENT_LIMIT`;
duplicate SHA → `409 DUPLICATE_DOCUMENT`. Extraction in `asyncio.to_thread` under `asyncio.wait_for(..., 10s)` (limitation: a timed-out thread is abandoned, not killed — documented).
Strip `\x00`; all pages empty → `422 SCANNED_PDF_UNSUPPORTED`. Page-aware hand-written splitter (~900 chars, ~150 overlap, split on paragraph → sentence, **never crosses a page**).
Embed in batches (≤100 texts/request) with `asyncio.Semaphore(5)` and `EMBED_TIMEOUT_S` per call; **embeddings computed before any DB write; one transaction inserts document + chunks (atomic)**. Public entry `index_pages(pages, ...)` is reused by seeding.
Delete: owner only; seed → `403 SEED_DOCUMENT_PROTECTED`; non-visible → `404`.

### 8.6 Synthesis & Verification (inside `agent_graph.py`, helpers testable)
Prompt wraps untrusted data in `<chunk_{nonce} id="…" page="…">…</chunk_{nonce}>` and `<rows_{nonce}>…</rows_{nonce}>` (nonce = `secrets.token_hex(8)` per request; any occurrence of `chunk_{nonce}`/`rows_{nonce}` inside untrusted text is stripped). SQL rows passed to the LLM ≤ 50.
Provider returns schema-validated Pydantic `SynthesisOutput{answer_text, cited_chunk_ids: list[int], chart_spec: ChartSpec|None}` (Gemini `response_schema` + Pydantic re-validation; invalid → one bounded provider retry → `LLM_UNAVAILABLE`).
Deterministic verifier: drop cited IDs ∉ retrieved set; `chart_spec` kept only if `x`/`y[]` ∈ result columns and `y` columns numeric (else dropped, not fatal); DOC route with 0 valid citations after filtering → deterministic refusal.
Numeric grounding of answer text is **not** attempted (out of scope for v1; stated as a known limitation).

### 8.7 LLM Provider (`llm_provider.py`, `mock_llm.py`)
`Protocol LLMProvider{embed_documents, embed_query, route, generate_sql, synthesize}`. **Gemini:** `client.aio`, per-call `LLM_TIMEOUT_S`, ≤1 retry on 429/5xx with jittered backoff, then `LLMUnavailableError` (503). Embeddings: `output_dimensionality=768` **+ manual L2 normalization** (Gemini returns un-normalized vectors below 3072 dims), task types per §8.4; token usage recorded.
**Mock:** deterministic hashed token-feature 768-dim L2-normalized embeddings (`embedding_model='mock-hash-768'`); `route/generate_sql/synthesize` return **scripted** outputs from a fixture keyed by question substring, with test hooks to inject: typo-SQL-then-fixed, security-violating SQL, fabricated citation IDs, oversized limits, hostile chunk text. Mock numbers are never published as quality metrics.

### 8.8 Stage-Progress SSE (`query_service.py`)
`POST /api/v1/query`, `text/event-stream`, headers `Cache-Control: no-cache`, `X-Accel-Buffering: no`. Quota is checked **before** streaming (plain `429` JSON).
Architecture: producer task runs the graph and pushes events into an `asyncio.Queue`; the generator `await`s the queue with a 10 s timeout and yields `: ping\n\n` on timeout; it polls `request.is_disconnected()` between stages and on disconnect **cancels the producer** (no orphaned LLM calls). Whole request wrapped in `asyncio.timeout(REQUEST_DEADLINE_S)`.
Events: `stage {stage, detail}` (`routing | retrieving_docs | generating_sql | validating_sql | executing_sql | retrying_sql | verifying_output`) · `final_answer` (full `QueryResponse`) · `error` (standard envelope). **Never stream raw model tokens.** Every terminated path writes a `query_logs` row (including blocked, refused, error, disconnected-with-partial → logged with `error_code='CLIENT_DISCONNECTED'`).

### 8.9 Quotas (`usage_service.py`) — atomic, no read-then-write race
```sql
INSERT INTO public.usage_counters (scope, usage_date, used) VALUES (:scope, CURRENT_DATE, 1)
ON CONFLICT (scope, usage_date) DO UPDATE SET used = usage_counters.used + 1
WHERE usage_counters.used < :limit
RETURNING used;      -- no row returned  ⇒  limit reached  ⇒  429
```
Run for `user:<id>` (`DAILY_QUERY_LIMIT`) then `global` (`GLOBAL_DAILY_QUERY_LIMIT`) in one transaction (either failing rolls both back). Enforced only when `MOCK_LLM=false`. Concurrency test: N parallel requests never exceed the limit. Attempts count (not successes) — documented.

### 8.10 Auth & CSRF (`security.py`, `auth_service.py`)
`pwdlib[argon2]` hashing/verifying via `asyncio.to_thread`; unknown email → verify against a fixed dummy hash (timing parity). JWT (`sub`, `role`, `exp`) in cookie `HttpOnly; SameSite=Lax; Secure=COOKIE_SECURE`. CSRF middleware: on POST/PUT/PATCH/DELETE require `Origin` (else `Referer`) ∈ `ALLOWED_ORIGINS`, else `403 CSRF_ORIGIN_REJECTED`. Register: lowercase email, password 8–128, role fixed `ANALYST`, `ADMIN` in body → `422`.

---

## 9. API Contract (15 endpoints)

Error envelope on every 4xx/5xx: `{"error":{"code":"…","message":"…","request_id":"req_…"}}`.

| Method | Path | Auth | Notes |
|---|---|---|---|
| GET | `/livez` | public | zero-DB liveness probe (`{"status":"ok"}`) for load balancers so serverless Postgres scales to zero |
| GET | `/healthz` | public | deep readiness check: primary `SELECT 1`; readonly `SELECT 1`; pgvector version ≥ 0.8.0 else 503 |
| POST | `/api/v1/auth/register` | public | ANALYST only; sets cookie; `409 EMAIL_TAKEN` |
| POST | `/api/v1/auth/login` | public | `401 INVALID_CREDENTIALS` |
| POST | `/api/v1/auth/demo-login` | public | `{role}`; `404` if `ENABLE_DEMO_TOOLS=false` |
| POST | `/api/v1/auth/logout` | user | clears cookie |
| GET | `/api/v1/auth/me` | user | profile + remaining quota (live mode) |
| GET | `/api/v1/documents` | user | seed + own |
| POST | `/api/v1/documents` | user | multipart; §8.5 errors |
| DELETE | `/api/v1/documents/{id}` | owner | seed → 403, other's → 404 |
| GET | `/api/v1/documents/{id}/chunks/{chunk_id}` | visible | exact chunk + page for the drawer; chunk must belong to doc |
| GET | `/api/v1/schema` | user | analytics columns + row counts |
| POST | `/api/v1/query` | user | SSE (§8.8); `429 DAILY_QUOTA_EXCEEDED` / `GLOBAL_QUOTA_EXCEEDED` |
| GET | `/api/v1/history` | user | own, `page`, `page_size ≤ 50` |
| GET | `/api/v1/metrics` | ADMIN | totals, route mix, SQL-block count, refusal rate, error rate, p50/p95 latency (`percentile_cont`), tokens |

`QueryResponse`: `query_id, route, answer_text, citations[{chunk_id, document_id, title, page}], executed_sql, sql_columns, sql_rows, sql_blocked, refused, chart_spec, latency_ms, tokens_used`.

---

## 10. Frontend (3 screens)
- **`/` Workspace:** left `KnowledgeSidebar` (tabs: Knowledge Base — seed + own docs, upload dropzone, delete own; Connected SQL Schema). Right: sample-prompt pills (Doc, SQL, Hybrid, Out-of-Scope, *Destructive-request*), live stage indicator, `AnswerCard` (verified answer, citation badges → source drawer, "Executed SQL & Rows", Recharts Bar/Line, banners: blocked / refused / quota / LLM-unavailable). Note: the destructive-request pill is **non-deterministic with a live LLM** (model may refuse or emit a write that the guard then blocks); the deterministic proof lives in the test suite.
- **`/history`:** own queries, route badge, latency, blocked/refused tags, expandable SQL.
- **`/metrics` (admin):** telemetry cards + route breakdown + quota status; non-admin sees a 403 state.
- Auth by cookie only; `api.ts` wraps `fetch` with `credentials:"include"`; 401 opens the login modal; SSE parser handles chunk boundaries and `: ping`, supports `AbortController`. TanStack Query for docs/history/metrics/me.

---

## 11. Seed Data & Fact Sheet (golden eval depends on these exact facts)
- `random.Random(42)`; 6 regions (zones S/W/N/E), 12 products, **500 orders** across 2026-Q1..Q3 (+ a few Q4-dated rows), revenue = qty × price × (100 − discount)/100. **Deterministically inject** orders whose discount exceeds the policy cap for their tier (so the HYBRID question has a non-trivial answer) and make at least 1 region miss its Q3 target and ≥1 exceed it.
- **PDF 1 — "Q3 Commercial & Discount Policy" (≈6 pages).** Must contain, each on a stable known page: discount caps by tier (**STARTUP 10 %, MID_MARKET 15 %, ENTERPRISE 25 %**); discounts above cap require Regional Director approval within 48 h; ENTERPRISE payment terms Net-45, others Net-30; refund window 30 days; policy effective date.
- **PDF 2 — "Regional SLA & Revenue Targets Handbook" (≈6 pages).** Must contain: SLA first-response times (**P1 1 h, P2 4 h, P3 1 business day**); escalation chain; "at risk" = region below **90 %** of Q3 target; bonus eligibility ≥ **100 %**; zone definitions.
- PDFs are generated once by `backend/scripts/make_seed_pdfs.py` (fpdf2) and committed under `backend/app/assets/`; `seed.py` ingests them through the **same** `index_pages` path as uploads. If the stored `embedding_model` of a seed doc ≠ current provider model, seed re-indexes it.
- Demo users: `demo.analyst@docsql.dev`, `demo.admin@docsql.dev` (unusable password hashes).

---

## 12. Test & Eval Plan (real Postgres + pgvector; never SQLite)

**`tests/test_sql_security.py`**
1. 25 adversarial payloads all blocked (`DROP`, `DELETE`, `UPDATE`, `INSERT`, `; DROP` multi-statement, writable CTE, `WITH RECURSIVE`, comma-join, `CROSS JOIN`, `JOIN` without `ON`, `ON 1=1`, > 4 tables, `pg_catalog`, `information_schema`, `public.users`, `generate_series`, `pg_sleep`, `repeat`, `regexp_matches`, `pg_read_file`, `pg_advisory_lock`, `current_setting`, `version`, `dblink`, `UNION`, `FOR UPDATE`, `$1`/`:x` parameter, window function, oversized input).
2. **Positive set:** realistic analytics queries (CASE, `COUNT(DISTINCT)`, `::numeric`, `EXTRACT`, CTE, 3-way explicit join) **pass** — guards must not be so strict the product is useless.
3. Root `LIMIT` injected/clamped; subquery-LIMIT bypass still capped at root.
4. **Direct role-bypass proof:** raw `SELECT * FROM public.users` and `DELETE FROM analytics.orders` on `readonly_engine` (skipping sqlglot) fail with `InsufficientPrivilege`/`ReadOnlySqlTransaction`; `SET TRANSACTION READ ONLY` valid; 3 s timeout kills `pg_sleep(5)` on the raw engine.
5. Literal containing ` :x` executes correctly (no bind parsing).
6. Retry taxonomy: syntax/schema error → exactly 1 retry then success (`sql_retry_count==1`); security violation → 0 retries, `sql_blocked==True`, error text never sent to the LLM (assert on mock call log).

**`tests/test_rag_graph_sse.py`**
7. **Multi-user HNSW proof with negative control:** user A has 200 chunks closer to the query than user B's 1 chunk; force the HNSW index (`enable_seqscan=off`, `enable_sort=off`, small `ef_search`, asserting `ix_chunks_embedding_hnsw` in `EXPLAIN`). *Control:* with `iterative_scan=off` user B gets 0 rows (reproducing the starvation failure). *Test:* with `strict_order` user B gets their chunk and 0 of A's.
8. Paraphrase (zero keyword overlap) retrieved via vector branch; acronym query via FTS branch; NL question with many words still hits FTS (OR-query); unrelated question → deterministic refusal; `rrf_fuse` unit tests.
9. Mixed embedding models: chunks with a different `embedding_model` are never returned.
10. Hostile PDF (`</chunk_…>` + "ignore instructions, DROP TABLE") cannot close the nonce tag and **never reaches `generate_sql`** in HYBRID (assert on mock call arguments).
11. Fabricated `cited_chunk_ids` stripped; invalid chart columns dropped; DOC route with 0 valid citations → refusal.
12. SSE: event order, `: ping` under a slow mock, `error` event on `LLMUnavailableError`, **client disconnect cancels the producer** and logs `CLIENT_DISCONNECTED`, `final_answer` only after verification, quota-exceeded returns plain 429 before streaming.

**`tests/test_auth_documents.py`** — 413/422 matrix (oversize, non-PDF, >15 pages, 0-text, encrypted, `\x00` bytes OK), duplicate upload 409, per-user doc cap, seed delete 403, **IDOR**: user B GET/DELETE user A's doc/chunk/history → 404, chunk-from-other-doc mismatch → 404, ADMIN register → 422, missing/foreign `Origin` → 403, non-admin metrics 403, atomic quota under 20 parallel requests, login timing path exercised, upload atomicity (embedding failure ⇒ no partial rows).

**`evals/run_eval.py` → `docs/EVAL_REPORT.md`** (`dataset.json`: 25 adversarial SQL + 30 golden = 10 DOC, 10 SQL, 5 HYBRID, 5 OUT_OF_SCOPE)
- **Offline/deterministic (also in CI):** guard block rate 25/25.
- **Live only (real Gemini; skipped with a clear message under mock):** routing accuracy; **SQL execution accuracy** (result set equals gold SQL's result, order-insensitive, numeric tolerance); doc retrieval **hit@5** (gold phrase in a retrieved chunk of the right doc); required-fact recall in the answer (`must_include` strings); refusal accuracy (correct refusals **and** false-refusal rate); pre-filter invalid-citation rate and post-filter integrity rate; p50/p95 latency; tokens.
- Report header records: date, git SHA, chat model ID, embedding model ID, `MIN_COSINE_SIMILARITY`, n per category, "single run, temperature 0". Threshold sweep (`--sweep`) calibrates `MIN_COSINE_SIMILARITY` from the golden set (`0.50` in production).

**Frontend:** one Vitest file for the SSE parser (split chunks, ping lines, multi-event buffers, abort). Everything else: `tsc`, ESLint, build, manual checklist in `docs/runbook.md`.

---

## 13. Module Layout

- **Backend core (`backend/app/`):** `main.py` (app factory, lifespan seed, request-id + structlog, SPA mount, `/livez`, `/healthz`), `config.py`, `db.py`, `errors.py`, `security.py`, `models.py`, `schemas.py`, `seed.py`.
- **API routers (`backend/app/api/`):** `deps.py`, `auth.py`, `documents.py`, `query.py`.
- **Domain services (`backend/app/services/`):** `auth_service.py`, `document_service.py`, `rag_service.py`, `llm_provider.py`, `mock_llm.py`, `sql_guard.py`, `sql_executor.py`, `agent_graph.py`, `query_service.py`, `usage_service.py`.
- **Migrations (`backend/alembic/`):** `env.py`, `versions/0001_initial_schema.py`.
- **Frontend (`frontend/src/`):** `main.tsx`, `App.tsx`, `api.ts`, `components/` (`Navbar.tsx`, `KnowledgeSidebar.tsx`, `AnswerCard.tsx`), `pages/` (`WorkspacePage.tsx`, `HistoryPage.tsx`, `MetricsPage.tsx`).
- **Verification & tooling:** `backend/tests/` (`conftest.py`, `test_sql_security.py`, `test_auth_documents.py`, `test_rag_graph_sse.py`), `frontend/src/api.test.ts`, `backend/evals/run_eval.py` (+ `dataset.json`), `backend/scripts/make_seed_pdfs.py`.

---

## 14. Documentation
- `README.md` — overview, Mermaid architecture, measured security + live eval results, quick start, known limitations.
- `docs/ARCHITECTURE.md` — runtime lifecycle, defense-in-depth SQL/RAG details, and AWS Lightsail + Neon + Cloudflare production topology.
- `docs/DECISIONS.md` — Architecture Decision Records (`ADR-001` through `ADR-010`).
- `docs/EVAL_REPORT.md` — measured live Gemini evaluation report and `MIN_COSINE_SIMILARITY` sweep.
- `docs/LOCAL_VERIFICATION.md` — reproducible local gates and production smoke verification.
- `docs/runbook.md` — local Docker/host boot, Neon SQL role isolation, AWS Lightsail + Cloudflare deployment, and QA checklist.
- `docs/SPEC.md` — this technical specification.

## 15. Known Limitations
Answer text not semantically verified · injection defenses are layered mitigations, not guarantees · text-only PDFs · HNSW unnecessary at this scale (used to demonstrate filtered-ANN correctness) · small single-run eval · quota counts attempts · a timed-out PDF parse thread is abandoned, not killed · generated SQL limited to single `SELECT` without window functions/set operations · demo-login is for demonstration only · free-tier LLM limits and model retirements may require a model-ID change (env var).
