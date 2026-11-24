"""Stage-Progress SSE: producer task -> queue -> generator, with heartbeats and disconnect cancel."""

import asyncio
import json
import time
import uuid
from collections.abc import AsyncIterator
from typing import Protocol

import anyio
import structlog
from sqlalchemy.exc import SQLAlchemyError

from app.errors import AppError, RequestTimeoutError, error_body
from app.schemas import Citation, QueryResponse
from app.services import usage_service
from app.services.agent_graph import CompiledGraph, GraphDeps, GraphState
from app.services.usage_service import QueryLogEntry

log = structlog.get_logger()
REFUSAL_STAGE = "verifying_output"


class DisconnectProbe(Protocol):
    async def is_disconnected(self) -> bool: ...


class RunState:
    """Shared between the producer task and the generator; the generator logs exactly once."""

    def __init__(self) -> None:
        self.acc: GraphState = {}
        self.error: AppError | None = None
        self.logged = False


def sse(event: str, data: str) -> str:
    return f"event: {event}\ndata: {data}\n\n"


def stage_event(stage: str, detail: str) -> tuple[str, str]:
    return "stage", json.dumps({"stage": stage, "detail": detail})


def stages_after(node: str, acc: GraphState) -> list[tuple[str, str]]:
    """Stage events derived from the node that just finished (stream_mode='updates')."""
    route = acc.get("route", "")
    if node == "router":
        if route == "OUT_OF_SCOPE":
            return [stage_event(REFUSAL_STAGE, "out of scope")]
        first = "generating_sql" if route == "SQL" else "retrieving_docs"
        return [stage_event(first, f"route={route}")]
    if node == "hybrid_rag":
        if acc.get("refused"):
            return []
        if route == "DOC":
            return [stage_event(REFUSAL_STAGE, f"{len(acc.get('chunks', []))} passages")]
        return [stage_event("generating_sql", f"{len(acc.get('chunks', []))} passages retrieved")]
    if node == "generate_sql":
        return [
            stage_event("validating_sql", "AST policy check"),
            stage_event("executing_sql", "read-only"),
        ]
    if node == "validate_and_execute_sql":
        if acc.get("needs_retry"):
            return [
                stage_event("retrying_sql", "correcting SQL"),
                stage_event("generating_sql", "retry 1/1"),
            ]
        return [stage_event(REFUSAL_STAGE, "checking citations and chart")]
    return []


def build_response(query_id: uuid.UUID, acc: GraphState, started: float) -> QueryResponse:
    by_id = {c.id: c for c in acc.get("chunks", [])}
    citations = [
        Citation(
            chunk_id=i, document_id=by_id[i].document_id, title=by_id[i].title, page=by_id[i].page
        )
        for i in acc.get("citations", [])
        if i in by_id
    ]
    executed = acc.get("executed_sql")
    return QueryResponse(
        query_id=query_id,
        route=acc.get("route"),
        answer_text=acc.get("answer", ""),
        citations=citations,
        executed_sql=executed,
        sql_columns=acc.get("columns", []) if executed else [],
        sql_rows=acc.get("rows", []) if executed else [],
        sql_blocked=acc.get("blocked", False),
        refused=acc.get("refused", False),
        chart_spec=acc.get("chart_spec"),
        latency_ms=int((time.monotonic() - started) * 1000),
        tokens_used=acc.get("tokens", 0),
    )


async def _produce(
    graph: CompiledGraph,
    run: RunState,
    queue: "asyncio.Queue[tuple[str, str]]",
    initial: GraphState,
) -> None:
    try:
        await queue.put(stage_event("routing", "classifying the question"))
        async for update in graph.astream(initial, stream_mode="updates"):
            for node, delta in update.items():
                run.acc.update(delta)
                for event in stages_after(node, run.acc):
                    await queue.put(event)
        await queue.put(("final", ""))
    except AppError as exc:
        run.error = exc
        await queue.put(("error", ""))
    except asyncio.CancelledError:
        raise
    except Exception as exc:  # last resort: the client must still get a terminal event
        log.error("query_producer_failed", error=repr(exc), exc_info=exc)
        run.error = AppError("Internal server error")
        await queue.put(("error", ""))


async def _write_log(
    deps: GraphDeps,
    run: RunState,
    query_id: uuid.UUID,
    user_id: uuid.UUID,
    question: str,
    started: float,
    error_code: str | None,
) -> None:
    if run.logged:
        return
    run.logged = True
    acc = run.acc
    chart = acc.get("chart_spec")
    entry = QueryLogEntry(
        id=query_id, user_id=user_id, question=question,
        latency_ms=int((time.monotonic() - started) * 1000), route=acc.get("route"),
        generated_sql=acc.get("generated_sql"), sql_blocked=acc.get("blocked", False),
        sql_retry_count=acc.get("retries", 0), refused=acc.get("refused", False),
        error_code=error_code or acc.get("error_code"), cited_chunk_ids=acc.get("citations", []),
        chart_spec=chart.model_dump() if chart else None, answer_text=acc.get("answer", ""),
        tokens_used=acc.get("tokens", 0),
    )  # fmt: skip
    try:
        await usage_service.write_query_log(deps.db.session_factory, entry)
    except SQLAlchemyError as exc:
        log.error("query_log_write_failed", error=repr(exc))


async def stream_query(
    request: DisconnectProbe,
    deps: GraphDeps,
    graph: CompiledGraph,
    user_id: uuid.UUID,
    question: str,
    request_id: str,
) -> AsyncIterator[str]:
    settings = deps.settings
    query_id, started = uuid.uuid4(), time.monotonic()
    run, queue = RunState(), asyncio.Queue[tuple[str, str]]()
    producer = asyncio.create_task(
        _produce(graph, run, queue, {"question": question, "user_id": user_id, "retries": 0})
    )
    try:
        try:
            async with asyncio.timeout(settings.request_deadline_s):
                while True:
                    try:
                        event, data = await asyncio.wait_for(
                            queue.get(), settings.sse_ping_interval_s
                        )
                    except TimeoutError:
                        if await request.is_disconnected():
                            return  # finally: cancels the producer and logs CLIENT_DISCONNECTED
                        yield ": ping\n\n"
                        continue
                    if await request.is_disconnected():
                        return
                    if event == "stage":
                        yield sse("stage", data)
                        continue
                    if event == "final":
                        response = build_response(query_id, run.acc, started)
                        await _write_log(deps, run, query_id, user_id, question, started, None)
                        yield sse("final_answer", response.model_dump_json())
                    else:
                        exc = run.error or AppError()
                        await _write_log(deps, run, query_id, user_id, question, started, exc.code)
                        yield sse(
                            "error", json.dumps(error_body(exc.code, exc.message, request_id))
                        )
                    return
        except TimeoutError:  # asyncio.timeout expired: REQUEST_DEADLINE_S
            exc = RequestTimeoutError()
            await _write_log(deps, run, query_id, user_id, question, started, exc.code)
            yield sse("error", json.dumps(error_body(exc.code, exc.message, request_id)))
    finally:
        producer.cancel()
        with anyio.CancelScope(shield=True):
            await asyncio.gather(producer, return_exceptions=True)
            if not run.logged:
                log.info("client_disconnected", query_id=str(query_id))
                await _write_log(
                    deps, run, query_id, user_id, question, started, "CLIENT_DISCONNECTED"
                )
