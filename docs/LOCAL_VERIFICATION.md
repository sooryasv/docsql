# Local verification

Verified locally via Docker Compose (PostgreSQL 16 + `pgvector >= 0.8.0`, Python 3.12, Node 22) and live in production on AWS Lightsail (`ap-southeast-1`) + Neon Serverless PostgreSQL 16 (`pgvector 0.8.7`). Re-run locally with the commands below.

## 1. Database
```bash
docker compose up -d db            # or any Postgres 16 with pgvector >= 0.8.0
export PGPASSWORD=postgres
psql -h localhost -U postgres -d docsql -v ON_ERROR_STOP=1 -f docker/init-roles.sql   # roles + docsql_test
```

## 2. Backend gates (from `backend/`)
```bash
export TEST_DATABASE_URL=postgresql://postgres:postgres@localhost:5432/docsql_test
export TEST_ANALYTICS_READONLY_DATABASE_URL=postgresql://docsql_readonly:docsql_readonly@localhost:5432/docsql_test
ruff check app tests evals scripts && ruff format --check app tests evals scripts
mypy --strict app
pytest -q
python -m evals.run_eval            # offline guard check; live metrics need MOCK_LLM=false + GEMINI_API_KEY
```

## 3. Frontend gates (from `frontend/`)
```bash
npm ci && npm run lint && npm run typecheck && npm test && npm run build
```

## 4. Verified gate outputs
```text
## backend: ruff check & ruff format --check
All checks passed!
34 files already formatted

## backend: mypy --strict app
Success: no issues found in 25 source files

## backend: pytest -q (against real PostgreSQL + pgvector)
109 passed in 25.86s

## frontend: npm run lint && npm run typecheck && npm test && npm run build
✓ src/api.test.ts (10 tests)
Test Files  1 passed (1)
✓ built in 3.56s

## live eval: python -m evals.run_eval --sweep --delay 10 (gemini-3.5-flash-lite)
- Guard block rate: 25/25 (100%)
- Routing accuracy: 28/30 (93%) | Hit@5: 14/15 (93%) | Refusal: 5/5 (100%)
- Citation integrity: 6/23 (26%) pre-filter -> 30/30 (100%) post-filter
```

## 5. Production smoke verification (`https://docsql.sooryasv.me`)
```text
$ curl [https://docsql.sooryasv.me/livez](https://docsql.sooryasv.me/livez)
{"status":"ok"}

$ curl [https://docsql.sooryasv.me/healthz](https://docsql.sooryasv.me/healthz)
{"status":"ok","checks":{"primary":"ok","pgvector":"0.8.7","readonly":"ok"}}
```

- **Neon role isolation verified:** `docsql_readonly` reads `analytics.orders`, fails on `SELECT COUNT(*) FROM public.users` (`InsufficientPrivilegeError`), and fails on `DELETE FROM analytics.orders` (`ReadOnlySQLTransactionError`).
- **Browser end-to-end QA verified (desktop + mobile):** Demo Admin login over HTTPS, seeded PDFs, SQL bar chart, Hybrid citations + SQL rows, Out-of-Scope refusal, responsive mobile navigation, and `/metrics` telemetry with enforced quotas (`10/day` user, `80/day` global).
