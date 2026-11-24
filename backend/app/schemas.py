"""Pydantic v2 contracts: API payloads, LLM structured outputs, and internal result types."""

import re
import uuid
from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

Route = Literal["DOC", "SQL", "HYBRID", "OUT_OF_SCOPE"]
_EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")


def _clean_email(value: str) -> str:
    value = value.strip().lower()
    if len(value) > 255 or not _EMAIL_RE.match(value):
        raise ValueError("invalid email address")
    return value


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


# ---------- auth ----------
class RegisterRequest(StrictModel):
    email: str
    password: str = Field(min_length=8, max_length=128)
    full_name: str = Field(min_length=1, max_length=120)

    _email = field_validator("email")(_clean_email)


class LoginRequest(StrictModel):
    email: str
    password: str = Field(min_length=1, max_length=128)

    _email = field_validator("email")(_clean_email)


class DemoLoginRequest(StrictModel):
    role: Literal["ANALYST", "ADMIN"]


class UserOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    email: str
    full_name: str
    role: Literal["ANALYST", "ADMIN"]


class MeOut(BaseModel):
    user: UserOut
    quota_limit: int | None
    quota_remaining: int | None
    demo_tools_enabled: bool


# ---------- documents ----------
class DocumentOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    title: str
    filename: str
    page_count: int
    chunk_count: int
    embedding_model: str
    is_system_seed: bool
    created_at: datetime


class ChunkOut(BaseModel):
    id: int
    document_id: uuid.UUID
    title: str
    page_number: int
    content: str


# ---------- LLM structured outputs ----------
class RouteDecision(BaseModel):
    route: Route
    reason: str = ""


class SqlGeneration(BaseModel):
    sql: str


class ChartSpec(BaseModel):
    type: Literal["bar", "line"]
    x: str
    y: list[str] = Field(min_length=1, max_length=4)
    title: str = ""


class SynthesisOutput(BaseModel):
    answer_text: str
    cited_chunk_ids: list[int] = Field(default_factory=list)
    chart_spec: ChartSpec | None = None


# ---------- query ----------
class QueryRequest(StrictModel):
    question: str = Field(min_length=3, max_length=1000)

    @field_validator("question")
    @classmethod
    def _strip(cls, value: str) -> str:
        return value.replace("\x00", "").strip()


class Citation(BaseModel):
    chunk_id: int
    document_id: uuid.UUID
    title: str
    page: int


class QueryResponse(BaseModel):
    query_id: uuid.UUID
    route: Route | None
    answer_text: str
    citations: list[Citation]
    executed_sql: str | None
    sql_columns: list[str]
    sql_rows: list[list[object]]
    sql_blocked: bool
    refused: bool
    chart_spec: ChartSpec | None
    latency_ms: int
    tokens_used: int


# ---------- schema / history / metrics ----------
class ColumnInfo(BaseModel):
    name: str
    data_type: str
    nullable: bool


class TableInfo(BaseModel):
    name: str
    row_count: int
    columns: list[ColumnInfo]


class SchemaOut(BaseModel):
    schema_name: Literal["analytics"] = "analytics"
    tables: list[TableInfo]


class HistoryItem(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    question: str
    route: Route | None
    generated_sql: str | None
    sql_blocked: bool
    sql_retry_count: int
    refused: bool
    error_code: str | None
    answer_text: str
    latency_ms: int
    tokens_used: int
    created_at: datetime


class HistoryPage(BaseModel):
    items: list[HistoryItem]
    page: int
    page_size: int
    total: int


class MetricsOut(BaseModel):
    total_queries: int
    route_mix: dict[str, int]
    sql_blocked_count: int
    refusal_rate: float
    error_rate: float
    latency_p50_ms: float
    latency_p95_ms: float
    total_tokens: int
    quota_mode: Literal["enforced", "disabled_mock"]
    global_used_today: int
    global_limit: int
