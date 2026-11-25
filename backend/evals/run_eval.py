"""Eval harness. Offline part (guard block rate) always runs; quality metrics need a LIVE model.

Usage (from backend/):  python -m evals.run_eval [--out ../docs/EVAL_REPORT.md] [--sweep]
Mock-mode runs prove plumbing only and are labelled as such; they are never quality numbers.
"""

import argparse
import asyncio
import json
import statistics
import subprocess
import sys
import uuid
from datetime import UTC, datetime
from pathlib import Path

from app.config import Settings, get_settings
from app.db import Database
from app.errors import AppError, SecurityViolationError, SQLSyntaxOrSchemaError
from app.schemas import SynthesisOutput
from app.seed import SEED_DOCS, run_seed
from app.services import rag_service
from app.services.agent_graph import GraphDeps, build_graph
from app.services.llm_provider import LLMProvider, SynthesisRequest, create_provider
from app.services.sql_executor import execute_validated
from app.services.sql_guard import validate_sql

DATASET = json.loads((Path(__file__).parent / "dataset.json").read_text())
TITLES = dict(SEED_DOCS)
SWEEP = [0.20, 0.25, 0.30, 0.35, 0.40, 0.45, 0.50]


class RecordingProvider:
    """Delegates to the real provider and records the PRE-verification cited chunk IDs."""

    def __init__(self, inner: LLMProvider) -> None:
        self.inner = inner
        self.embedding_model = inner.embedding_model
        self.raw_citations: list[tuple[list[int], set[int]]] = []

    async def embed_documents(self, texts: list[str]) -> list[list[float]]:
        return await self.inner.embed_documents(texts)

    async def embed_query(self, text: str) -> list[float]:
        return await self.inner.embed_query(text)

    async def route(self, question: str, doc_titles: tuple[str, ...] = ()):  # type: ignore[no-untyped-def]
        return await self.inner.route(question, doc_titles)

    async def generate_sql(self, question: str, schema_text: str, error: str | None):  # type: ignore[no-untyped-def]
        return await self.inner.generate_sql(question, schema_text, error)

    async def synthesize(self, request: SynthesisRequest) -> tuple[SynthesisOutput, int]:
        output, tokens = await self.inner.synthesize(request)
        self.raw_citations.append((list(output.cited_chunk_ids), set(request.chunk_ids)))
        return output, tokens


def normalize(rows: list[list[object]]) -> list[str]:
    cleaned = [[round(v, 2) if isinstance(v, float) else v for v in row] for row in rows]
    return sorted(json.dumps(r, default=str) for r in cleaned)


def guard_block_rate() -> tuple[int, int]:
    blocked = 0
    for item in DATASET["adversarial_sql"]:
        try:
            validate_sql(item["sql"])
        except SecurityViolationError:
            blocked += 1
        except SQLSyntaxOrSchemaError:
            pass
    return blocked, len(DATASET["adversarial_sql"])


def pct(num: int, den: int) -> str:
    return f"{num}/{den} ({100 * num / den:.0f}%)" if den else "n/a"


def percentile(values: list[float], q: float) -> float:
    ordered = sorted(values)
    return ordered[min(len(ordered) - 1, int(q * len(ordered)))] if ordered else 0.0


async def run_live(
    db: Database, settings: Settings, provider: RecordingProvider, delay_s: float = 0.0
) -> dict[str, object]:
    graph = build_graph(GraphDeps(provider=provider, db=db, settings=settings))
    user_id = uuid.uuid4()
    from sqlalchemy import text

    async with db.primary.begin() as conn:  # throwaway eval user (cascade-deleted in finally)
        await conn.execute(
            text(
                "INSERT INTO users (id, email, full_name, password_hash, role) VALUES (:i, :e, 'eval', '!', 'ANALYST')"
            ),
            {"i": user_id, "e": f"eval-{user_id.hex[:8]}@docsql.dev"},
        )
    integrity: list[bool] = []
    stats: dict[str, list[bool]] = {
        k: [] for k in ("route", "sql", "hit", "refusal", "false_refusal", "recall")
    }
    latencies: list[float] = []
    tokens = 0
    try:
        for idx, item in enumerate(DATASET["golden"]):
            if delay_s > 0 and idx > 0:
                await asyncio.sleep(delay_s)
            state = None
            for attempt in (0, 1):
                started = datetime.now(UTC)
                try:
                    state = await graph.ainvoke(
                        {"question": item["question"], "user_id": user_id, "retries": 0}
                    )
                    break
                except AppError as exc:
                    if exc.code == "LLM_UNAVAILABLE" and delay_s > 0 and attempt == 0:
                        await asyncio.sleep(max(delay_s, 15.0))
                        continue
                    print(f"  {item['id']}: {exc.code}", file=sys.stderr)
                    state = {"route": None, "refused": False}
                    break
            assert state is not None
            latencies.append((datetime.now(UTC) - started).total_seconds() * 1000)
            tokens += int(state.get("tokens", 0))
            ids = {c.id for c in state.get("chunks", [])}
            integrity.append(all(i in ids for i in state.get("citations", [])))
            stats["route"].append(state.get("route") == item["route"])
            if item["route"] == "OUT_OF_SCOPE":
                stats["refusal"].append(bool(state.get("refused")))
                continue
            stats["false_refusal"].append(bool(state.get("refused")))
            if "gold_sql" in item:
                gold = await execute_validated(
                    db.readonly, validate_sql(item["gold_sql"]), settings.sql_timeout_ms
                )
                got = state.get("rows")
                matched = got is not None and normalize(got) == normalize(gold.rows)
                stats["sql"].append(matched)
                if not matched:
                    print(
                        f"  [SQL MISS] {item['id']}: route={state.get('route')} "
                        f"sql={state.get('executed_sql')!r} got={got} expected={gold.rows}",
                        file=sys.stderr,
                    )
            if "gold_phrase" in item:
                title = TITLES[item["gold_doc"]]
                phrase = " ".join(item["gold_phrase"].lower().split())
                chunks = state.get("chunks", [])
                stats["hit"].append(
                    any(
                        c.title == title and phrase in " ".join(c.content.lower().split())
                        for c in chunks
                    )
                )
            if item.get("must_include"):
                answer = str(state.get("answer", "")).lower()
                stats["recall"].append(all(s.lower() in answer for s in item["must_include"]))
    finally:
        async with db.primary.begin() as conn:
            await conn.execute(text("DELETE FROM public.users WHERE id = :i"), {"i": user_id})
    cited = sum(len(raw) for raw, _ in provider.raw_citations)
    invalid = sum(1 for raw, ok in provider.raw_citations for i in raw if i not in ok)
    return {
        "stats": stats, "latencies": latencies, "tokens": tokens, "cited": cited,
        "invalid": invalid, "integrity": integrity,
    }  # fmt: skip


async def sweep(
    db: Database, settings: Settings, provider: LLMProvider, delay_s: float = 0.0
) -> list[str]:
    """Calibrate MIN_COSINE_SIMILARITY: hit@5 on DOC/HYBRID vs. spurious retrieval on OUT_OF_SCOPE."""
    lines = ["| threshold | hit@5 (DOC+HYBRID) | OOS questions with any passage |", "|---|---|---|"]
    owner = uuid.uuid4()
    qvecs: list[list[float]] = []
    for idx, item in enumerate(DATASET["golden"]):
        if delay_s > 0 and idx > 0:
            await asyncio.sleep(min(delay_s, 1.0))
        qvecs.append(await provider.embed_query(item["question"]))
    for threshold in SWEEP:
        cfg = settings.model_copy(update={"min_cosine_similarity": threshold})
        hits = total = spurious = oos = 0
        for item, qvec in zip(DATASET["golden"], qvecs, strict=True):
            async with db.session_factory() as session, session.begin():
                chunks = await rag_service.retrieve_with_vector(
                    session, item["question"], qvec, owner, cfg, provider.embedding_model
                )
            if item["route"] == "OUT_OF_SCOPE":
                oos += 1
                spurious += bool(chunks)
            elif "gold_phrase" in item:
                total += 1
                phrase = " ".join(item["gold_phrase"].lower().split())
                hits += any(" ".join(c.content.lower().split()).find(phrase) >= 0 for c in chunks)
        lines.append(f"| {threshold:.2f} | {pct(hits, total)} | {pct(spurious, oos)} |")
    return lines


def git_sha() -> str:
    try:
        return subprocess.check_output(["git", "rev-parse", "--short", "HEAD"], text=True).strip()
    except (OSError, subprocess.CalledProcessError):
        return "unknown"


async def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--out", type=Path)
    parser.add_argument("--sweep", action="store_true")
    parser.add_argument("--delay", type=float, default=0.0)
    args = parser.parse_args()
    settings = get_settings()
    blocked, total = guard_block_rate()
    lines = [
        "# DocSQL Eval Report", "",
        f"- Date: {datetime.now(UTC).isoformat(timespec='seconds')}  \n- Git SHA: {git_sha()}",
        f"- Chat model: `{settings.gemini_chat_model}` | Embedding model: `{settings.gemini_embed_model}`",
        f"- MIN_COSINE_SIMILARITY: {settings.min_cosine_similarity}",
        "- Protocol: single run, temperature 0. n per category: 10 DOC, 10 SQL, 5 HYBRID, 5 OUT_OF_SCOPE; 25 adversarial SQL.",
        "", "## Offline / deterministic", "", f"- Guard block rate: **{blocked}/{total}** adversarial payloads blocked",
    ]  # fmt: skip
    if settings.mock_llm:
        print(
            "MOCK_LLM=true: skipping live quality metrics (set MOCK_LLM=false and GEMINI_API_KEY)."
        )
        lines += [
            "",
            "## Live metrics",
            "",
            "**Skipped: ran in MOCK_LLM mode (plumbing only, not quality).**",
        ]
    else:
        db = Database.create(settings)
        provider = RecordingProvider(create_provider(settings))
        try:
            await run_seed(db, provider, settings)
            result = await run_live(db, settings, provider, delay_s=args.delay)
            if args.sweep:
                lines += [
                    "",
                    "## MIN_COSINE_SIMILARITY sweep",
                    "",
                    *await sweep(db, settings, provider, delay_s=args.delay),
                ]
        finally:
            await db.dispose()
        s = result["stats"]  # type: ignore[index]
        lat = result["latencies"]  # type: ignore[assignment]
        lines += [
            "", "## Live (real Gemini)", "", "| Metric | Result |", "|---|---|",
            f"| Routing accuracy | {pct(sum(s['route']), len(s['route']))} |",
            f"| SQL execution accuracy | {pct(sum(s['sql']), len(s['sql']))} |",
            f"| Doc retrieval hit@5 | {pct(sum(s['hit']), len(s['hit']))} |",
            f"| Required-fact recall in answer | {pct(sum(s['recall']), len(s['recall']))} |",
            f"| Refusal accuracy (OUT_OF_SCOPE) | {pct(sum(s['refusal']), len(s['refusal']))} |",
            f"| False-refusal rate (in-scope) | {pct(sum(s['false_refusal']), len(s['false_refusal']))} |",
            f"| Pre-filter invalid-citation rate | {pct(int(result['invalid']), int(result['cited']))} |",
            f"| Post-filter citation integrity | {pct(sum(result['integrity']), len(result['integrity']))} |",
            f"| Latency p50 / p95 (ms) | {statistics.median(lat):.0f} / {percentile(lat, 0.95):.0f} |",
            f"| Tokens used | {result['tokens']} |",
        ]  # fmt: skip
    report = "\n".join(lines) + "\n"
    print(report)
    if args.out:
        args.out.write_text(report)
    return 0 if blocked == total else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
