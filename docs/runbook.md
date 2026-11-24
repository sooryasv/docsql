# Runbook

## Local boot (Docker)
```bash
cp .env.example .env
docker compose up --build
curl localhost:8000/livez          # {"status":"ok"} (zero-DB liveness check)
curl localhost:8000/healthz        # {"status":"ok","checks":{"primary":"ok","pgvector":"0.8.0","readonly":"ok"}}
```
The compose `command` runs `alembic upgrade head` as a **release step** before `uvicorn`; migrations never run inside the app lifespan.

## Local boot (host Python + Docker Postgres)
```bash
docker compose up -d db
cd backend
python -m venv .venv && . .venv/bin/activate
python - <<'PY' | xargs pip install
import tomllib; p = tomllib.load(open("pyproject.toml","rb"))["project"]
print(" ".join(f"'{d}'" for d in p["dependencies"] + p["optional-dependencies"]["dev"]))
PY
alembic upgrade head
RUN_SEED_ON_STARTUP=true uvicorn app.main:app --reload
cd ../frontend && npm ci && npm run dev        # http://localhost:5173 (proxies /api and /healthz)
```

## Managed Postgres (Neon / RDS)
1. Enable `pgvector >= 0.8.0` (`SELECT extversion FROM pg_extension WHERE extname='vector'` must be ≥ 0.8.0; `/healthz` checks it).
2. Run `alembic upgrade head` against a **direct** (non-pooled) connection string from a one-off release step.
3. **Create the least-privilege `docsql_readonly` role:**
   - **RDS / self-hosted:** Run `docker/init-roles.sql` as admin (change the password first).
   - **Neon (important):** Do **not** create `docsql_readonly` via the Neon Console UI—Console-created roles automatically inherit `neon_superuser`, bypassing schema revokes. Create it via SQL (`CREATE ROLE docsql_readonly WITH LOGIN PASSWORD '...'`), revoke `public`, grant `USAGE`/`SELECT` on `analytics`, set `default_transaction_read_only = on`, `statement_timeout = '3000ms'`, `search_path = analytics`, and verify that `SELECT COUNT(*) FROM public.users` and `DELETE FROM analytics.orders` both fail.
4. Set `DATABASE_URL` (pooled app role) and `ANALYTICS_READONLY_DATABASE_URL` (pooled `docsql_readonly` role). `?sslmode=require` is honored.
5. Behind a pooler (PgBouncer/Neon `-pooler` endpoint), set `USE_NULL_POOL=true`; asyncpg statement caching is already disabled.
6. Boot once with `RUN_SEED_ON_STARTUP=true` to seed demo users, 500 analytics orders, and the 2 system PDFs idempotently.

## AWS Lightsail Containers + Cloudflare
- Push the multi-stage Docker image to private **Amazon ECR** in the same region as Neon (e.g., `ap-southeast-1`) and grant Lightsail ECR pull access under the **Images** tab.
- Set the Lightsail public endpoint **Health check path** to **`/livez`** (never `/healthz`), so Neon scales to zero during idle periods and stays within free compute hours.
- Attach a Lightsail TLS certificate for your custom domain (`docsql.sooryasv.me`), proxy the `CNAME` through Cloudflare with SSL/TLS set to **Full (strict)**, enable **Bot Fight Mode**, and add a free WAF rate-limiting rule (`15 req / 10s` per IP on `/api/v1/query`, `/api/v1/documents`, `/api/v1/auth/demo-login`).

## Gemini
- `MOCK_LLM=false`, `GEMINI_API_KEY=...`. Chat model is `GEMINI_CHAT_MODEL`; embeddings are `gemini-embedding-001` at 768 dims
  (L2-normalized in code). **Verify model IDs against Google's deprecations page before deploying**; changing the embedding
  model re-indexes seed documents at next startup, and users' own documents must be re-uploaded (they are filtered out otherwise).
- Free-tier rate limits apply; the provider retries once on 429/5xx. Per-user and global daily quotas are enforced only when
  `MOCK_LLM=false`.

## Production checklist
`ENABLE_DEMO_TOOLS=false` *(for private enterprise deployments; if `true` for a public portfolio demo, pair with low `DAILY_QUERY_LIMIT`/`GLOBAL_DAILY_QUERY_LIMIT`, Cloudflare edge rate limiting, and a free-tier/capped API key)* · `COOKIE_SECURE=true` · strong 64-hex `JWT_SECRET` · `ALLOWED_ORIGINS` set to exact origin(s) with no trailing slash or spaces · serve behind TLS · Lightsail health check on `/livez` · `MIN_COSINE_SIMILARITY=0.50` *(calibrated via `python -m evals.run_eval --sweep --delay 10`)*.

## Manual QA checklist
- [ ] Sign in as demo analyst; the two seed PDFs appear and cannot be deleted.
- [ ] Upload a small text PDF; it appears as “Yours”; deleting it removes it.
- [ ] Doc / SQL / Hybrid / Out-of-Scope pills each stream stages and end in a verified answer.
- [ ] Citation badge opens the source drawer with the passage.
- [ ] “Executed SQL & Rows” expands; the chart renders for tabular results.
- [ ] “Destructive request” shows the blocked banner (mock) — non-deterministic with a live model.
- [ ] Stop button cancels a running query.
- [ ] Non-admin visiting `/metrics` sees the 403 state; demo admin sees telemetry.
- [ ] Exhaust the quota (live mode): the 429 banner appears before any streaming.
