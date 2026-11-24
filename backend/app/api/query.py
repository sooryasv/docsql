"""Query (SSE), schema, history and admin metrics endpoints."""

from typing import Annotated

from fastapi import APIRouter, Depends, Query, Request
from fastapi.responses import StreamingResponse
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import (
    get_current_user,
    get_db,
    get_provider,
    get_session,
    get_settings_dep,
    require_admin,
)
from app.config import Settings
from app.db import Database
from app.models import User
from app.schemas import HistoryPage, MetricsOut, QueryRequest, SchemaOut
from app.services import query_service, sql_executor, usage_service
from app.services.agent_graph import GraphDeps
from app.services.llm_provider import LLMProvider

router = APIRouter(tags=["query"])
Session = Annotated[AsyncSession, Depends(get_session)]
CurrentUser = Annotated[User, Depends(get_current_user)]
Cfg = Annotated[Settings, Depends(get_settings_dep)]
SSE_HEADERS = {"Cache-Control": "no-cache", "X-Accel-Buffering": "no", "Connection": "keep-alive"}


@router.post("/query")
async def query(
    payload: QueryRequest,
    request: Request,
    user: CurrentUser,
    db: Annotated[Database, Depends(get_db)],
    provider: Annotated[LLMProvider, Depends(get_provider)],
    settings: Cfg,
) -> StreamingResponse:
    # Quota is checked BEFORE streaming so exhaustion is a plain 429 JSON response.
    await usage_service.consume_quota(db.session_factory, user.id, settings)
    deps = GraphDeps(provider=provider, db=db, settings=settings)
    request_id = str(request.state.request_id)
    events = query_service.stream_query(
        request, deps, request.app.state.graph, user.id, payload.question, request_id
    )
    return StreamingResponse(events, media_type="text/event-stream", headers=SSE_HEADERS)


@router.get("/schema")
async def schema(_user: CurrentUser, db: Annotated[Database, Depends(get_db)]) -> SchemaOut:
    return await sql_executor.describe_schema(db.readonly)


@router.get("/history")
async def history(
    session: Session,
    user: CurrentUser,
    page: Annotated[int, Query(ge=1)] = 1,
    page_size: Annotated[int, Query(ge=1, le=50)] = 20,
) -> HistoryPage:
    return await usage_service.list_history(session, user.id, page, page_size)


@router.get("/metrics")
async def metrics(
    session: Session, _admin: Annotated[User, Depends(require_admin)], settings: Cfg
) -> MetricsOut:
    return await usage_service.compute_metrics(session, settings)
