# DocSQL Eval Report

- Date: 2026-11-23T14:32:21+00:00  
- Git SHA: pre-release Docker eval run (`.git` not mounted in container)
- Chat model: `gemini-3.5-flash-lite` | Embedding model: `gemini-embedding-001`
- MIN_COSINE_SIMILARITY: 0.35
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
| Routing accuracy | 28/30 (93%) |
| SQL execution accuracy | 9/15 (60%) |
| Doc retrieval hit@5 | 14/15 (93%) |
| Required-fact recall in answer | 14/15 (93%) |
| Refusal accuracy (OUT_OF_SCOPE) | 5/5 (100%) |
| False-refusal rate (in-scope) | 0/25 (0%) |
| Pre-filter invalid-citation rate | 6/23 (26%) |
| Post-filter citation integrity | 30/30 (100%) |
| Latency p50 / p95 (ms) | 5450 / 14800 |
| Tokens used | 28214 |

## Run notes
- **Transient upstream errors:** Two items (`g17` SQL, `g22` HYBRID) returned `LLM_UNAVAILABLE` due to upstream Gemini `503` overload errors during the run, accounting for both routing misses, the single retrieval/fact-recall miss, and two of the six SQL execution misses.
- **Citation verification proof:** Live `gemini-3.5-flash-lite` produced `6/23` (`26%`) invalid/mismatched citations before verification; deterministic filtering brought post-filter citation integrity to `30/30` (`100%`).
- **Threshold calibration:** Retrieval recall stayed `15/15` (`100%`) across `0.20–0.50` (out-of-scope questions still matched passages via the OR-ed full-text branch while the router still refused `5/5` OOS questions). Production deploys with `MIN_COSINE_SIMILARITY=0.50`.
