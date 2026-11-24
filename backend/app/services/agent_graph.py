"""Bounded LangGraph pipeline: router -> (refuse | hybrid_rag | generate_sql) -> ... -> verify.

Isolation: generate_sql receives the question and the analytics schema ONLY; retrieved PDF text
never reaches it. Blocked SQL ends in a deterministic answer with no further LLM call.
"""

import json
import secrets
import uuid
from dataclasses import dataclass
from typing import Literal, TypedDict

import structlog
from langgraph.graph import END, START, StateGraph
from langgraph.graph.state import CompiledStateGraph
from sqlalchemy import select

from app.config import Settings
from app.db import Database
from app.errors import SecurityViolationError, SQLSyntaxOrSchemaError, SQLTimeoutError
from app.models import Document
from app.schemas import ChartSpec, Route, SynthesisOutput
from app.services import rag_service
from app.services.llm_provider import LLMProvider, SynthesisRequest
from app.services.rag_service import RetrievedChunk
from app.services.sql_executor import describe_schema, execute_validated, schema_prompt_text
from app.services.sql_guard import validate_sql

log = structlog.get_logger()

MAX_PROMPT_ROWS = 50
MAX_CELL_CHARS = 200
REFUSAL_TEXT = (
    "I can only answer from the loaded policy documents and the connected sales database, "
    "and I could not find a supporting source for that question."
)
BLOCKED_TEXT = "The generated SQL was blocked by the safety policy and was not executed."
TIMEOUT_TEXT = "The database query exceeded its time limit and was cancelled."
SQL_FAILED_TEXT = (
    "I could not produce a valid SQL query for that question after one correction attempt."
)


class GraphState(TypedDict, total=False):
    question: str
    user_id: uuid.UUID
    route: Route
    chunks: list[RetrievedChunk]
    generated_sql: str
    executed_sql: str | None
    sql_error: str | None
    needs_retry: bool
    retries: int
    columns: list[str]
    rows: list[list[object]]
    blocked: bool
    sql_timeout: bool
    sql_failed: bool
    refused: bool
    answer: str
    citations: list[int]
    chart_spec: ChartSpec | None
    tokens: int
    error_code: str | None


CompiledGraph = CompiledStateGraph[GraphState, None, GraphState, GraphState]


@dataclass(frozen=True)
class GraphDeps:
    provider: LLMProvider
    db: Database
    settings: Settings


# ---------- pure, testable helpers ----------
def new_nonce() -> str:
    return secrets.token_hex(8)


def strip_nonce(text: str, nonce: str) -> str:
    """Remove any attempt by untrusted text to forge this request's delimiter tags."""
    return text.replace(f"chunk_{nonce}", "").replace(f"rows_{nonce}", "")


def _cell(value: object) -> object:
    return value[:MAX_CELL_CHARS] if isinstance(value, str) else value


def build_synthesis_prompt(
    question: str,
    route: str,
    nonce: str,
    chunks: list[RetrievedChunk],
    columns: list[str],
    rows: list[list[object]],
) -> str:
    parts = [f"Question: {question}", f"Route: {route}"]
    for chunk in chunks:
        body = strip_nonce(chunk.content, nonce)
        parts.append(f'<chunk_{nonce} id="{chunk.id}" page="{chunk.page}">{body}</chunk_{nonce}>')
    if route == "HYBRID" and not chunks:
        parts.append("No supporting document passage was retrieved for the document part.")
    if columns:
        table = {
            "columns": columns,
            "rows": [[_cell(v) for v in r] for r in rows[:MAX_PROMPT_ROWS]],
        }
        payload = strip_nonce(json.dumps(table, default=str), nonce)
        parts.append(f"<rows_{nonce}>{payload}</rows_{nonce}>")
    return "\n".join(parts)


def _is_number(value: object) -> bool:
    return isinstance(value, int | float) and not isinstance(value, bool)


def _chart_is_valid(spec: ChartSpec, columns: list[str], rows: list[list[object]]) -> bool:
    if spec.x not in columns or any(y not in columns for y in spec.y) or not rows:
        return False
    for y in spec.y:
        values = [row[columns.index(y)] for row in rows if row[columns.index(y)] is not None]
        if not values or not all(_is_number(v) for v in values):
            return False
    return True


def verify_synthesis(
    output: SynthesisOutput, retrieved_ids: set[int], columns: list[str], rows: list[list[object]]
) -> tuple[list[int], ChartSpec | None]:
    """Referential integrity only: IDs must have been retrieved; chart columns must exist."""
    citations = [i for i in dict.fromkeys(output.cited_chunk_ids) if i in retrieved_ids]
    chart = output.chart_spec
    return citations, chart if chart and _chart_is_valid(chart, columns, rows) else None


# ---------- graph ----------
def build_graph(deps: GraphDeps) -> CompiledGraph:
    provider, settings = deps.provider, deps.settings

    async def router(state: GraphState) -> GraphState:
        async with deps.db.session_factory() as session:
            visible = Document.is_system_seed.is_(True) | (Document.uploaded_by == state["user_id"])
            stmt = (
                select(Document.title)
                .where(visible, Document.embedding_model == provider.embedding_model)
                .order_by(Document.is_system_seed.desc(), Document.title.asc())
                .limit(20)
            )
            doc_titles = tuple((await session.execute(stmt)).scalars().all())
        decision, tokens = await provider.route(state["question"], doc_titles)
        route = decision.route if decision.route in ("DOC", "SQL", "HYBRID") else "OUT_OF_SCOPE"
        return {"route": route, "tokens": state.get("tokens", 0) + tokens}

    async def refuse(_state: GraphState) -> GraphState:
        return {"refused": True, "answer": REFUSAL_TEXT, "citations": [], "chart_spec": None}

    async def hybrid_rag(state: GraphState) -> GraphState:
        chunks = await rag_service.retrieve(
            deps.db.session_factory, provider, settings, state["question"], state["user_id"]
        )
        if state["route"] == "DOC" and not chunks:
            return {"chunks": [], "refused": True, "answer": REFUSAL_TEXT, "citations": []}
        return {"chunks": chunks}

    async def generate_sql(state: GraphState) -> GraphState:
        schema_text = schema_prompt_text(await describe_schema(deps.db.readonly))
        # Only the question, the schema and (benign) error text: NEVER retrieved chunks.
        sql, tokens = await provider.generate_sql(
            state["question"], schema_text, state.get("sql_error")
        )
        return {"generated_sql": sql, "tokens": state.get("tokens", 0) + tokens}

    async def validate_and_execute_sql(state: GraphState) -> GraphState:
        retries = state.get("retries", 0)
        try:
            validated = validate_sql(state["generated_sql"], settings.sql_max_rows)
            result = await execute_validated(deps.db.readonly, validated, settings.sql_timeout_ms)
        except SecurityViolationError as exc:  # terminal: 0 retries, text never reaches the LLM
            log.warning("sql_blocked", reason=str(exc))
            return {"blocked": True, "needs_retry": False, "sql_error": None}
        except SQLSyntaxOrSchemaError as exc:
            if retries < 1:
                return {"sql_error": str(exc), "retries": retries + 1, "needs_retry": True}
            return {"sql_failed": True, "needs_retry": False, "sql_error": str(exc)}
        except SQLTimeoutError:
            return {"sql_timeout": True, "needs_retry": False}
        return {
            "executed_sql": validated.sql,
            "columns": result.columns,
            "rows": result.rows,
            "needs_retry": False,
        }

    async def synthesize_and_verify(state: GraphState) -> GraphState:
        deterministic = _deterministic_outcome(state)
        if deterministic is not None:
            return deterministic
        chunks, columns, rows = (
            state.get("chunks", []),
            state.get("columns", []),
            state.get("rows", []),
        )
        nonce = new_nonce()
        prompt = build_synthesis_prompt(
            state["question"], state["route"], nonce, chunks, columns, rows
        )
        request = SynthesisRequest(
            question=state["question"],
            route=state["route"],
            prompt=prompt,
            chunk_ids=tuple(c.id for c in chunks),
            chunk_texts=tuple(c.content for c in chunks),
            columns=tuple(columns),
            rows=tuple(tuple(r) for r in rows[:MAX_PROMPT_ROWS]),
        )
        output, tokens = await provider.synthesize(request)
        total = state.get("tokens", 0) + tokens
        citations, chart = verify_synthesis(output, {c.id for c in chunks}, columns, rows)
        if state["route"] == "DOC" and not citations:
            return {"refused": True, "answer": REFUSAL_TEXT, "citations": [], "tokens": total}
        if state["route"] == "SQL":
            citations = []
        return {
            "answer": output.answer_text,
            "citations": citations,
            "chart_spec": chart,
            "tokens": total,
        }

    def route_after_router(state: GraphState) -> Literal["refuse", "hybrid_rag", "generate_sql"]:
        route = state["route"]
        return (
            "refuse"
            if route == "OUT_OF_SCOPE"
            else "generate_sql"
            if route == "SQL"
            else "hybrid_rag"
        )

    def after_rag(state: GraphState) -> Literal["end", "synthesize_and_verify", "generate_sql"]:
        if state.get("refused"):
            return "end"
        return "synthesize_and_verify" if state["route"] == "DOC" else "generate_sql"

    def after_sql(state: GraphState) -> Literal["generate_sql", "synthesize_and_verify"]:
        return "generate_sql" if state.get("needs_retry") else "synthesize_and_verify"

    graph: StateGraph[GraphState, None, GraphState, GraphState] = StateGraph(GraphState)
    nodes = {
        "router": router,
        "refuse": refuse,
        "hybrid_rag": hybrid_rag,
        "generate_sql": generate_sql,
        "validate_and_execute_sql": validate_and_execute_sql,
        "synthesize_and_verify": synthesize_and_verify,
    }
    for name, action in nodes.items():
        # langgraph's generic NodeInputT resolves to Never for plain async functions (verified).
        graph.add_node(name, action, input_schema=GraphState)  # type: ignore[call-overload]
    graph.add_edge(START, "router")
    graph.add_conditional_edges(
        "router",
        route_after_router,
        {"refuse": "refuse", "hybrid_rag": "hybrid_rag", "generate_sql": "generate_sql"},
    )
    graph.add_edge("refuse", END)
    graph.add_conditional_edges(
        "hybrid_rag",
        after_rag,
        {
            "end": END,
            "synthesize_and_verify": "synthesize_and_verify",
            "generate_sql": "generate_sql",
        },
    )
    graph.add_edge("generate_sql", "validate_and_execute_sql")
    graph.add_conditional_edges(
        "validate_and_execute_sql",
        after_sql,
        {"generate_sql": "generate_sql", "synthesize_and_verify": "synthesize_and_verify"},
    )
    graph.add_edge("synthesize_and_verify", END)
    return graph.compile()


def _deterministic_outcome(state: GraphState) -> GraphState | None:
    """Terminal SQL outcomes get a fixed answer and NO further LLM call."""
    if state.get("blocked"):
        return {"answer": BLOCKED_TEXT, "citations": [], "chart_spec": None}
    if state.get("sql_timeout"):
        return {
            "answer": TIMEOUT_TEXT,
            "citations": [],
            "chart_spec": None,
            "error_code": "QUERY_TIMEOUT",
        }
    if state.get("sql_failed"):
        return {
            "answer": SQL_FAILED_TEXT,
            "citations": [],
            "chart_spec": None,
            "error_code": "SQL_FAILED",
        }
    return None
