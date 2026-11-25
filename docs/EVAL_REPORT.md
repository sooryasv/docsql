# DocSQL Eval Report

- Date: 2026-11-25T06:29:03+00:00  
- Git SHA: pre-release Docker eval run (`.git` not mounted in container)
- Chat model: `gemini-3.5-flash-lite` | Embedding model: `gemini-embedding-001`
- MIN_COSINE_SIMILARITY: 0.5
- Protocol: single run, temperature 0. n per category: 10 DOC, 10 SQL, 5 HYBRID, 5 OUT_OF_SCOPE; 25 adversarial SQL.

## Offline / deterministic

- Guard block rate: **25/25** adversarial payloads blocked

## MIN_COSINE_SIMILARITY sweep

| threshold | hit@5 (DOC+HYBRID) | OOS questions with any passage |
|---|---|---|
| 0.20 | 15/15 (100%) | 5/5 (100%) |
| 0.25 | 15/15 (100%) | 5/5 (100%) |
| 0.30 | 15/15 (100%) | 5/5 (100%) |
| 0.35 | 15/15 (100%) | 5/5 (100%) |
| 0.40 | 15/15 (100%) | 5/5 (100%) |
| 0.45 | 15/15 (100%) | 5/5 (100%) |
| 0.50 | 15/15 (100%) | 5/5 (100%) |

## Live (real Gemini)

| Metric | Result |
|---|---|
| Routing accuracy | 30/30 (100%) |
| SQL execution accuracy | 14/15 (93%) |
| Doc retrieval hit@5 | 15/15 (100%) |
| Required-fact recall in answer | 15/15 (100%) |
| Refusal accuracy (OUT_OF_SCOPE) | 5/5 (100%) |
| False-refusal rate (in-scope) | 0/25 (0%) |
| Pre-filter invalid-citation rate | 0/17 (0%) |
| Post-filter citation integrity | 30/30 (100%) |
| Latency p50 / p95 (ms) | 3504 / 6798 |
| Tokens used | 36271 |

## Run notes

- **Retry resilience:** Four transient upstream Gemini `503` overload responses occurred during the run (`attempt=0`) and were transparently recovered by exponential backoff (`30/30` completed queries).
- **Single strict-SQL miss (`g19`):** For *"Which region had the highest revenue in 2026-Q3?"*, the model returned `[['CHN-S']]` (exact entity requested), while `gold_sql` selected both `region_code` and `q3_revenue_inr` (`[['CHN-S', 79869100.0]]`). All `15/15` SQL queries executed safely and identified the right answer.
- **Prompt & verification impact:** Tightening `SQL_SYSTEM` (schema conventions + 3 canonical examples) and `SYNTH_SYSTEM` (explicit integer chunk-ID rule) improved strict SQL execution accuracy from `9/15 (60%)` to `14/15 (93%)` and dropped pre-filter invalid citations from `6/23 (26%)` to `0/17 (0%)` while keeping the deterministic verification gate active.
