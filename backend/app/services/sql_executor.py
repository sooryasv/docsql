"""Executes ALREADY-VALIDATED SQL on the read-only engine, and describes the analytics schema."""

import asyncio
from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal

import structlog
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import AsyncEngine

from app.errors import SecurityViolationError, SQLSyntaxOrSchemaError, SQLTimeoutError
from app.schemas import ColumnInfo, SchemaOut, TableInfo
from app.services.sql_guard import ALLOWED_TABLES, ValidatedSQL

log = structlog.get_logger()
SECURITY_STATES = {"42501", "25006"}  # insufficient_privilege, read_only_sql_transaction
TIMEOUT_STATE = "57014"  # query_canceled (statement_timeout)
VALUE_HINTS = """Allowed values: regions.zone IN (SOUTH, WEST, NORTH, EAST);
products.category IN (CLOUD_OPS, DATA_PLATFORM, SECURITY, AI_SUITE);
orders.quarter IN ('2026-Q1','2026-Q2','2026-Q3','2026-Q4');
orders.customer_tier IN (STARTUP, MID_MARKET, ENTERPRISE).
orders.revenue_inr = quantity * unit_price_inr * (100 - discount_pct) / 100.
Q3 revenue target per region is regions.q3_target_inr."""


@dataclass(frozen=True)
class ExecutionResult:
    columns: list[str]
    rows: list[list[object]]


def _json_safe(value: object) -> object:
    if isinstance(value, Decimal):
        return float(value)
    if isinstance(value, date | datetime):
        return value.isoformat()
    if value is None or isinstance(value, bool | int | float | str):
        return value
    return str(value)


def _sqlstate(exc: DBAPIError) -> str:
    return str(getattr(exc.orig, "sqlstate", None) or getattr(exc.orig, "pgcode", "") or "")


def _map_error(exc: DBAPIError) -> Exception:
    state = _sqlstate(exc)
    message = str(getattr(exc.orig, "message", None) or exc.orig)[:300]
    if state in SECURITY_STATES:
        return SecurityViolationError(f"database refused the statement ({state})")
    if state == TIMEOUT_STATE:
        return SQLTimeoutError()
    if state[:2] in {"42", "22"}:  # syntax/schema and data-exception classes: model can fix these
        return SQLSyntaxOrSchemaError(message)
    return exc


async def _run(engine: AsyncEngine, sql: str, timeout_ms: int) -> ExecutionResult:
    async with engine.connect() as conn, conn.begin():
        # First statement of the transaction. (`SET LOCAL TRANSACTION ...` is NOT used.)
        await conn.exec_driver_sql("SET TRANSACTION READ ONLY")
        await conn.execute(
            text(
                "SELECT set_config('statement_timeout', :ms, true), "
                "set_config('search_path', 'analytics', true), "
                "set_config('lock_timeout', '1000ms', true)"
            ),
            {"ms": f"{timeout_ms}ms"},
        )
        # exec_driver_sql: no bind-parameter parsing, so a literal containing ' :x' is safe.
        result = await conn.exec_driver_sql(sql)
        columns = list(result.keys())
        rows = [[_json_safe(v) for v in row] for row in result.fetchall()]
    return ExecutionResult(columns=columns, rows=rows)


async def execute_validated(
    engine: AsyncEngine, validated: ValidatedSQL, timeout_ms: int
) -> ExecutionResult:
    """Runs only the guard's re-emitted SQL string (never the model's original text)."""
    try:
        return await asyncio.wait_for(
            _run(engine, validated.sql, timeout_ms), timeout_ms / 1000 + 2
        )
    except TimeoutError as exc:
        raise SQLTimeoutError() from exc
    except DBAPIError as exc:
        mapped = _map_error(exc)
        if mapped is exc:
            raise
        log.info("sql_execution_failed", error_class=type(mapped).__name__)
        raise mapped from exc


async def describe_schema(engine: AsyncEngine) -> SchemaOut:
    """Column metadata + row counts for GET /schema and the SQL prompt (read-only role)."""
    tables: list[TableInfo] = []
    async with engine.connect() as conn, conn.begin():
        await conn.exec_driver_sql("SET TRANSACTION READ ONLY")
        for name in sorted(ALLOWED_TABLES):
            cols = await conn.execute(
                text(
                    "SELECT column_name, data_type, is_nullable FROM information_schema.columns "
                    "WHERE table_schema = 'analytics' AND table_name = :t ORDER BY ordinal_position"
                ),
                {"t": name},
            )
            columns = [ColumnInfo(name=c, data_type=d, nullable=n == "YES") for c, d, n in cols]
            count = (
                await conn.exec_driver_sql(f"SELECT COUNT(*) FROM analytics.{name}")
            ).scalar_one()
            tables.append(TableInfo(name=name, row_count=int(count), columns=columns))
    return SchemaOut(tables=tables)


def schema_prompt_text(schema: SchemaOut) -> str:
    lines = []
    for table in schema.tables:
        cols = ", ".join(f"{c.name} {c.data_type}" for c in table.columns)
        lines.append(f"analytics.{table.name} ({table.row_count} rows): {cols}")
    return "\n".join(lines) + "\n" + VALUE_HINTS
