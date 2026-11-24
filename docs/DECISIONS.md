# Architecture Decision Records

## ADR-001 — Single PostgreSQL instead of a dedicated vector DB
**Decision:** relational data, `pgvector` embeddings and FTS live in one Postgres. **Why:** one transactional boundary (a document
and its chunks commit atomically), joins for tenant filtering, one thing to operate. **Rejected:** Pinecone/Qdrant — extra moving
part and no cross-store transactions at this scale. **Trade-off:** filtered ANN needs care (`hnsw.iterative_scan = 'strict_order'`, verified with a negative-control test);
not a statement about billion-vector scale.

## ADR-002 — AST allowlist + DB role + known residual risk
**Decision:** two independent layers (sqlglot class allowlist executing only re-emitted SQL; read-only role with
`SELECT` on `analytics.*`, read-only transaction, statement timeout). **Why:** a parser can be wrong; a role cannot be talked out
of its grants. Tests exercise each layer without the other. **Rejected:** regex filtering; trusting the model; the guard alone.
**Residual risk:** a join predicate that contains a column but is still weak (e.g. `ON a.id = a.id`) can create a large
intermediate result — bounded by the 3 s timeout, the 100-row cap and ≤ 4 table references. Rejections that signal attacks are
terminal (no retry); only plain syntax/unknown-table problems get one correction attempt.

## ADR-003 — Stage-progress SSE instead of token streaming
**Decision:** stream pipeline *stages*, then one verified final answer. **Why:** the answer is only trustworthy after citation and
chart verification, so streaming unverified tokens would show text that may later be retracted. Stages give honest progress.
**Trade-off:** slower perceived first token. **Detail:** `: ping` heartbeats, request deadline, cancel-on-disconnect.

## ADR-004 — LangGraph: an honest trade-off
**Decision:** a six-node LangGraph with one conditional retry loop. **Why:** the control flow (route, retry-once, terminal paths)
is explicit and the node-update stream maps directly to UI stages. **Honest trade-off:** this graph could be ~100 lines of plain
`async` functions; LangGraph adds a dependency and a typing wrinkle (one justified `type: ignore` on `add_node`). It was chosen for
explicit state transitions, inspectability, and clean node-to-stage SSE mapping, not because a framework is strictly required.

## ADR-005 — Atomic quota via a single upsert
**Decision:** `INSERT … ON CONFLICT DO UPDATE … WHERE used < :limit RETURNING used`; no returned row ⇒ limit reached. Per-user and
global counters run in **one transaction**, so a failed global check rolls back the user's increment. **Why:** no read-then-write
race (tested with 20 parallel calls vs. a limit of 5). **Trade-off:** counts attempts, not successes.

## ADR-006 — Eval philosophy: live numbers vs. mock plumbing
**Decision:** quality metrics are only reported from a live model run; mock runs prove plumbing and are labelled as such.
**Why:** a mock cannot measure retrieval quality or SQL accuracy, and publishing its output would be misleading. **Consequence:**
`docs/EVAL_REPORT.md` publishes single-run live Gemini metrics (`gemini-3.5-flash-lite` + `gemini-embedding-001`), including
transient upstream `503` misses and the `26%` pre-filter invalid-citation rate caught `100%` by deterministic verification.
The `--sweep` calibration set production `MIN_COSINE_SIMILARITY=0.50` (`15/15` recall preserved).

## ADR-007 — Embedding-model tagging
**Decision:** `documents.embedding_model` is stored, every retrieval query filters on the current model, and the seed script
re-indexes seed documents whose model differs. **Why:** vectors from different models are not comparable; silently mixing them
returns plausible-looking garbage. **Alternative rejected:** a per-model column/table (over-engineering for v1).

## ADR-008 — No admin delete
**Decision:** ADMIN can read telemetry but cannot read or delete other users' documents (cross-tenant access is 404 for everyone).
**Why:** least privilege; admin is an operations role, not a data-access role. **Trade-off:** support cases need DB access.

## ADR-009 — Separate `/livez` liveness and `/healthz` readiness probes
**Decision:** expose `/livez` (zero DB queries) for container load-balancer health checks while keeping `/healthz` as the deep
DB/`pgvector`/role readiness check. **Why:** AWS Lightsail continuously pings the health-check path; hitting `/healthz` every
few seconds prevents Neon Serverless Postgres from suspending after 5 minutes of inactivity, exhausting the 100 CU-hours/month
free tier mid-month. **Trade-off:** `/livez` checks process health only; DB health is verified via `/healthz` on deploy and
monitoring checks.

## ADR-010 — Layered protection for the public portfolio demo
**Decision:** keep `ENABLE_DEMO_TOOLS=true` on the public portfolio URL (`docsql.sooryasv.me`) while documenting
`ENABLE_DEMO_TOOLS=false` for private deployments. **Why:** forcing signup blocks quick reviewer inspection, whereas unguarded
demo endpoints risk API quota abuse. **Mitigation:** four layers—atomic PostgreSQL per-user (`10/day`) and global (`80/day`)
quotas, read-only admin telemetry (ADR-008), Cloudflare Bot Fight Mode + WAF rate limit (`15 req / 10s` per IP on cost-bearing
API paths), and a zero-spend free-tier Gemini API key ceiling.
