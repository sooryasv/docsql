"""Atomic quotas, the query audit log, per-user history and admin metrics."""

import uuid
from dataclasses import dataclass, field

import structlog
from sqlalchemy import func, select, text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.config import Settings
from app.errors import DailyQuotaExceededError, GlobalQuotaExceededError
from app.models import QueryLog
from app.schemas import HistoryItem, HistoryPage, MetricsOut

log = structlog.get_logger()

# Atomic: no read-then-write race. No row returned => the limit was reached.
CONSUME_SQL = text(
    "INSERT INTO public.usage_counters (scope, usage_date, used) VALUES (:scope, CURRENT_DATE, 1) "
    "ON CONFLICT (scope, usage_date) DO UPDATE SET used = usage_counters.used + 1 "
    "WHERE usage_counters.used < :limit RETURNING used"
)
USED_SQL = text(
    "SELECT COALESCE((SELECT used FROM public.usage_counters "
    "WHERE scope = :scope AND usage_date = CURRENT_DATE), 0)"
)


async def consume_quota(
    session_factory: async_sessionmaker[AsyncSession], user_id: uuid.UUID, settings: Settings
) -> None:
    """Per-user then global, in ONE transaction: either failing rolls both increments back."""
    if settings.mock_llm:
        return  # quotas protect LLM spend; mock mode has none
    async with session_factory() as session, session.begin():
        user_scope = f"user:{user_id}"
        row = (
            await session.execute(
                CONSUME_SQL, {"scope": user_scope, "limit": settings.daily_query_limit}
            )
        ).first()
        if row is None:
            raise DailyQuotaExceededError()
        row = (
            await session.execute(
                CONSUME_SQL, {"scope": "global", "limit": settings.global_daily_query_limit}
            )
        ).first()
        if row is None:
            raise GlobalQuotaExceededError()


async def remaining_quota(
    session_factory: async_sessionmaker[AsyncSession], user_id: uuid.UUID, settings: Settings
) -> int | None:
    if settings.mock_llm:
        return None
    async with session_factory() as session:
        used = (await session.execute(USED_SQL, {"scope": f"user:{user_id}"})).scalar_one()
    return max(0, settings.daily_query_limit - int(used))


@dataclass
class QueryLogEntry:
    id: uuid.UUID
    user_id: uuid.UUID
    question: str
    latency_ms: int
    route: str | None = None
    generated_sql: str | None = None
    sql_blocked: bool = False
    sql_retry_count: int = 0
    refused: bool = False
    error_code: str | None = None
    cited_chunk_ids: list[int] = field(default_factory=list)
    chart_spec: dict[str, object] | None = None
    answer_text: str = ""
    tokens_used: int = 0


async def write_query_log(
    session_factory: async_sessionmaker[AsyncSession], entry: QueryLogEntry
) -> None:
    async with session_factory() as session:
        session.add(QueryLog(**entry.__dict__))
        await session.commit()


async def list_history(
    session: AsyncSession, user_id: uuid.UUID, page: int, page_size: int
) -> HistoryPage:
    base = select(QueryLog).where(QueryLog.user_id == user_id)  # ownership predicate
    total = await session.scalar(
        select(func.count()).select_from(QueryLog).where(QueryLog.user_id == user_id)
    )
    rows = await session.scalars(
        base.order_by(QueryLog.created_at.desc()).offset((page - 1) * page_size).limit(page_size)
    )
    items = [HistoryItem.model_validate(r) for r in rows]
    return HistoryPage(items=items, page=page, page_size=page_size, total=int(total or 0))


async def compute_metrics(session: AsyncSession, settings: Settings) -> MetricsOut:
    totals = (
        await session.execute(
            text(
                "SELECT COUNT(*) AS total, COALESCE(SUM(tokens_used), 0) AS tokens, "
                "COUNT(*) FILTER (WHERE sql_blocked) AS blocked, "
                "COUNT(*) FILTER (WHERE refused) AS refused, "
                "COUNT(*) FILTER (WHERE error_code IS NOT NULL) AS errors, "
                "COALESCE(percentile_cont(0.5) WITHIN GROUP (ORDER BY latency_ms), 0) AS p50, "
                "COALESCE(percentile_cont(0.95) WITHIN GROUP (ORDER BY latency_ms), 0) AS p95 "
                "FROM public.query_logs"
            )
        )
    ).one()
    mix_rows = await session.execute(
        text("SELECT route, COUNT(*) FROM public.query_logs WHERE route IS NOT NULL GROUP BY route")
    )
    total = int(totals.total)
    global_used = (await session.execute(USED_SQL, {"scope": "global"})).scalar_one()
    return MetricsOut(
        total_queries=total,
        route_mix={str(route): int(count) for route, count in mix_rows},
        sql_blocked_count=int(totals.blocked),
        refusal_rate=round(totals.refused / total, 4) if total else 0.0,
        error_rate=round(totals.errors / total, 4) if total else 0.0,
        latency_p50_ms=float(totals.p50),
        latency_p95_ms=float(totals.p95),
        total_tokens=int(totals.tokens),
        quota_mode="disabled_mock" if settings.mock_llm else "enforced",
        global_used_today=int(global_used),
        global_limit=settings.global_daily_query_limit,
    )
