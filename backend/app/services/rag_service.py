"""Hybrid retrieval: independent vector + OR-ed full-text branches fused by RRF."""

import re
import uuid
from dataclasses import dataclass

import structlog
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.config import Settings
from app.services.llm_provider import LLMProvider

log = structlog.get_logger()
BRANCH_LIMIT = 20
TOP_N = 5
MAX_FTS_TOKENS = 40

# Visibility predicate on EVERY query: seed or own documents, current embedding model only.
VISIBLE = "(d.is_system_seed OR d.uploaded_by = :uid) AND d.embedding_model = :model"
VECTOR_SQL = text(
    "SELECT c.id, c.document_id, d.title, c.page_number, c.content, "
    "c.embedding <=> CAST(:qvec AS vector) AS dist "
    "FROM public.document_chunks c JOIN public.documents d ON d.id = c.document_id "
    f"WHERE {VISIBLE} ORDER BY c.embedding <=> CAST(:qvec AS vector) LIMIT {BRANCH_LIMIT}"
)
FTS_SQL = text(
    "WITH q AS (SELECT to_tsquery('english', :q) AS tsq) "
    "SELECT c.id, c.document_id, d.title, c.page_number, c.content, "
    "ts_rank_cd(c.search_tsv, q.tsq) AS rank "
    "FROM q CROSS JOIN public.document_chunks c JOIN public.documents d ON d.id = c.document_id "
    f"WHERE {VISIBLE} AND c.search_tsv @@ q.tsq AND ts_rank_cd(c.search_tsv, q.tsq) > 0 "
    f"ORDER BY rank DESC, c.id LIMIT {BRANCH_LIMIT}"
)


@dataclass(frozen=True)
class RetrievedChunk:
    id: int
    document_id: uuid.UUID
    title: str
    page: int
    content: str


def rrf_fuse(vec_ids: list[int], fts_ids: list[int], k: int = 60, top_n: int = TOP_N) -> list[int]:
    """Reciprocal Rank Fusion over two independent rankings (pure function)."""
    scores: dict[int, float] = {}
    for ranking in (vec_ids, fts_ids):
        for rank, chunk_id in enumerate(ranking, start=1):
            scores[chunk_id] = scores.get(chunk_id, 0.0) + 1.0 / (k + rank)
    ordered = sorted(scores, key=lambda cid: (-scores[cid], cid))
    return ordered[:top_n]


def build_tsquery(question: str) -> str | None:
    """OR-ed tokens from a sanitized charset ([A-Za-z0-9]{2,}), so no tsquery syntax can be injected."""
    tokens = list(dict.fromkeys(t.lower() for t in re.findall(r"[A-Za-z0-9]{2,}", question)))
    return " | ".join(tokens[:MAX_FTS_TOKENS]) or None


def _vector_literal(vector: list[float]) -> str:
    return "[" + ",".join(f"{v:.8f}" for v in vector) + "]"


async def retrieve_with_vector(
    session: AsyncSession,
    question: str,
    qvec: list[float],
    user_id: uuid.UUID,
    settings: Settings,
    model: str,
) -> list[RetrievedChunk]:
    """One transaction; iterative scan is set before the first vector query."""
    params = {"uid": user_id, "model": model}
    await session.execute(text("SELECT set_config('hnsw.iterative_scan', 'strict_order', true)"))
    vec_rows = (await session.execute(VECTOR_SQL, {**params, "qvec": _vector_literal(qvec)})).all()
    # Cosine floor applied AFTER fetching so the ANN ORDER BY stays index-friendly.
    vec_rows = [r for r in vec_rows if 1.0 - float(r.dist) >= settings.min_cosine_similarity]
    fts_query = build_tsquery(question)
    fts_rows = (
        (await session.execute(FTS_SQL, {**params, "q": fts_query})).all() if fts_query else []
    )
    by_id = {r.id: r for r in [*vec_rows, *fts_rows]}
    fused = rrf_fuse([r.id for r in vec_rows], [r.id for r in fts_rows])
    return [
        RetrievedChunk(
            id=by_id[i].id,
            document_id=by_id[i].document_id,
            title=by_id[i].title,
            page=by_id[i].page_number,
            content=by_id[i].content,
        )
        for i in fused
    ]


async def retrieve(
    session_factory: async_sessionmaker[AsyncSession],
    provider: LLMProvider,
    settings: Settings,
    question: str,
    user_id: uuid.UUID,
) -> list[RetrievedChunk]:
    qvec = await provider.embed_query(question)  # RETRIEVAL_QUERY task type for Gemini
    async with session_factory() as session, session.begin():
        chunks = await retrieve_with_vector(
            session, question, qvec, user_id, settings, provider.embedding_model
        )
    log.info("retrieval_done", chunks=len(chunks))
    return chunks
