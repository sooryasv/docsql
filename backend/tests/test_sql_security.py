"""C1: LLM-generated SQL cannot write, escape `analytics`, call non-allowlisted functions, or run unbounded."""

import json
import time
import uuid
from pathlib import Path

import pytest
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError

from app.config import Settings
from app.db import Database
from app.errors import SecurityViolationError, SQLSyntaxOrSchemaError, SQLTimeoutError
from app.services.agent_graph import BLOCKED_TEXT, GraphDeps, build_graph
from app.services.mock_llm import MockLLM
from app.services.sql_executor import execute_validated
from app.services.sql_guard import validate_sql

DATASET = json.loads((Path(__file__).parent.parent / "evals" / "dataset.json").read_text())
ADVERSARIAL = [(p["name"], p["sql"]) for p in DATASET["adversarial_sql"]]
EXTRA_ADVERSARIAL = [
    ("FOR UPDATE", "SELECT * FROM analytics.orders FOR UPDATE"),
    ("$1 parameter", "SELECT * FROM analytics.orders WHERE id = $1"),
    ("named parameter", "SELECT * FROM analytics.orders WHERE id = :x"),
    ("window function", "SELECT RANK() OVER (ORDER BY id) FROM analytics.orders"),
    ("oversized input", "SELECT " + "1, " * 1500 + "1 FROM analytics.orders"),
    (
        "CTE shadowing pg_tables",
        "WITH pg_tables AS (SELECT * FROM pg_tables) SELECT * FROM pg_tables",
    ),
    ("regclass cast", "SELECT 'pg_user'::regclass FROM analytics.orders"),
    ("LIMIT ALL", "SELECT * FROM analytics.orders LIMIT ALL"),
    ("SELECT INTO", "SELECT * INTO analytics.copy FROM analytics.orders"),
]
POSITIVE = [
    "SELECT COUNT(*) FROM orders WHERE quarter = '2026-Q3' AND customer_tier = 'STARTUP' AND discount_pct > 10",
    "SELECT COUNT(*) FROM orders WHERE (customer_tier = 'STARTUP' OR customer_tier = 'ENTERPRISE') AND NOT quantity < 2",
    "SELECT region_id FROM orders GROUP BY region_id HAVING SUM(revenue_inr) < 0.9 * 1000000 AND COUNT(*) BETWEEN 1 AND 99",
    "SELECT COUNT(*), COUNT(DISTINCT region_id) FROM analytics.orders",
    "SELECT SUM(revenue_inr)::numeric AS s FROM analytics.orders",
    "SELECT CASE WHEN quantity > 5 THEN 'big' ELSE 'small' END AS size, COUNT(*) FROM orders GROUP BY 1",
    "SELECT EXTRACT(month FROM order_date) AS m, SUM(revenue_inr) FROM orders GROUP BY 1 ORDER BY 1",
    "WITH q3 AS (SELECT region_id, SUM(revenue_inr) AS rev FROM orders WHERE quarter = '2026-Q3' "
    "GROUP BY region_id) SELECT r.region_code, q3.rev FROM q3 JOIN regions r ON r.id = q3.region_id",
    "SELECT r.zone, p.category, SUM(o.revenue_inr) FROM orders o JOIN regions r ON r.id = o.region_id "
    "JOIN products p ON p.id = o.product_id GROUP BY r.zone, p.category",
    "SELECT ROUND(AVG(discount_pct), 2), DATE_TRUNC('month', order_date) FROM orders GROUP BY 2",
    "SELECT COALESCE(MAX(quantity), 0), NULLIF(MIN(quantity), 0), ABS(-1), LOWER(quarter) FROM orders GROUP BY quarter",
]


def test_dataset_has_25_adversarial_payloads() -> None:
    assert len(ADVERSARIAL) == 25


@pytest.mark.parametrize(
    ("name", "sql"), ADVERSARIAL + EXTRA_ADVERSARIAL, ids=lambda v: str(v)[:30]
)
def test_adversarial_payload_is_blocked(name: str, sql: str) -> None:
    with pytest.raises(SecurityViolationError):
        validate_sql(sql)


@pytest.mark.parametrize("sql", POSITIVE)
def test_realistic_queries_pass(sql: str) -> None:
    validated = validate_sql(sql)
    assert validated.sql.rstrip().upper().endswith("LIMIT 100") or "LIMIT" in validated.sql.upper()
    assert set(validated.tables) <= {"orders", "regions", "products"}


def test_trailing_semicolon_is_tolerated_but_embedded_one_is_not() -> None:
    assert validate_sql("SELECT 1 FROM analytics.orders;").limit == 100
    with pytest.raises(SecurityViolationError):
        validate_sql("SELECT 1 FROM analytics.orders; SELECT 2")


def test_benign_errors_are_retryable_not_security() -> None:
    with pytest.raises(SQLSyntaxOrSchemaError):
        validate_sql("SELEC FROM")
    with pytest.raises(SQLSyntaxOrSchemaError):
        validate_sql("SELECT * FROM analytics.customers")


def test_root_limit_injected_and_clamped_and_subquery_bypass_capped() -> None:
    assert validate_sql("SELECT * FROM orders").limit == 100
    assert validate_sql("SELECT * FROM orders LIMIT 5").limit == 5
    clamped = validate_sql("SELECT * FROM orders LIMIT 100000")
    assert clamped.limit == 100 and "LIMIT 100000" not in clamped.sql
    sub = validate_sql("SELECT * FROM (SELECT * FROM orders LIMIT 3) AS s")
    assert sub.limit == 100 and sub.sql.rstrip().endswith("LIMIT 100")
    assert validate_sql("SELECT * FROM orders", max_rows=10).limit == 10


def test_comments_are_stripped_from_reemitted_sql() -> None:
    validated = validate_sql("SELECT * FROM orders -- evil */ ; DROP TABLE x")
    assert "DROP" not in validated.sql and "--" not in validated.sql


# ----- DB-level defense: bypass proof (skips sqlglot entirely) with negative controls -----
async def test_readonly_role_blocks_reads_outside_analytics_and_writes(db: Database) -> None:
    async with db.readonly.connect() as conn:
        with pytest.raises(DBAPIError) as exc:
            await conn.exec_driver_sql("SELECT * FROM public.users")
        assert getattr(exc.value.orig, "sqlstate", "") == "42501"  # insufficient_privilege
    async with db.readonly.connect() as conn:
        with pytest.raises(DBAPIError) as exc2:
            await conn.exec_driver_sql("DELETE FROM analytics.orders")
        assert getattr(exc2.value.orig, "sqlstate", "") in {"25006", "42501"}


async def test_negative_control_primary_role_can_do_what_readonly_cannot(db: Database) -> None:
    """Shows the DB role is what blocks the above: the app role CAN read users and DELETE."""
    async with db.primary.connect() as conn:
        assert (await conn.execute(text("SELECT COUNT(*) FROM public.users"))).scalar_one() == 0
        await conn.execute(text("DELETE FROM analytics.orders WHERE id = -1"))
        await conn.rollback()


async def test_set_transaction_read_only_is_valid_first_statement(db: Database) -> None:
    async with db.readonly.connect() as conn, conn.begin():
        await conn.exec_driver_sql("SET TRANSACTION READ ONLY")
        assert (await conn.exec_driver_sql("SELECT 1")).scalar_one() == 1


async def test_statement_timeout_kills_pg_sleep_on_raw_engine(db: Database) -> None:
    started = time.monotonic()
    async with db.readonly.connect() as conn:
        with pytest.raises(DBAPIError) as exc:
            await conn.exec_driver_sql("SELECT pg_sleep(5)")
    assert getattr(exc.value.orig, "sqlstate", "") == "57014"
    assert time.monotonic() - started < 4.5


async def test_executor_maps_timeout_to_sql_timeout_error(db: Database) -> None:
    heavy = validate_sql(
        "SELECT COUNT(*) FROM orders a JOIN orders b ON a.quarter = b.quarter "
        "JOIN orders c ON a.quarter = c.quarter JOIN orders d ON a.quarter = d.quarter"
    )
    with pytest.raises(SQLTimeoutError):
        await execute_validated(db.readonly, heavy, timeout_ms=300)


async def test_executor_maps_schema_errors_and_runs_literal_with_colon(db: Database) -> None:
    with pytest.raises(SQLSyntaxOrSchemaError):
        await execute_validated(db.readonly, validate_sql("SELECT nope FROM orders"), 3000)
    literal = validate_sql("SELECT COUNT(*) AS n FROM orders WHERE order_code = 'a :x b'")
    result = await execute_validated(db.readonly, literal, 3000)
    assert result.rows == [[0]]  # ':x' inside a literal is not parsed as a bind parameter


async def test_executor_returns_json_safe_rows(db: Database) -> None:
    result = await execute_validated(
        db.readonly,
        validate_sql("SELECT order_date, revenue_inr FROM orders ORDER BY id LIMIT 2"),
        3000,
    )
    assert result.columns == ["order_date", "revenue_inr"]
    assert isinstance(result.rows[0][0], str) and len(result.rows) == 2
    json.dumps(result.rows)


# ----- retry taxonomy through the real graph -----
async def _run(graph, question: str) -> dict:  # type: ignore[no-untyped-def,type-arg]
    return await graph.ainvoke({"question": question, "user_id": uuid.uuid4(), "retries": 0})


async def test_syntax_error_retries_exactly_once_then_succeeds(graph, mock: MockLLM) -> None:  # type: ignore[no-untyped-def]
    mock.route_override = "SQL"
    mock.sql_queue = [
        "SELECT regionn FROM analytics.regions",
        "SELECT region_code FROM analytics.regions ORDER BY region_code",
    ]
    state = await _run(graph, "list the region codes")
    assert state["retries"] == 1 and state.get("rows") and not state.get("blocked")
    sql_calls = [d for n, d in mock.calls if n == "generate_sql"]
    assert len(sql_calls) == 2 and sql_calls[0]["error"] is None and sql_calls[1]["error"]


async def test_second_syntax_failure_is_terminal_without_third_attempt(
    graph, mock: MockLLM
) -> None:  # type: ignore[no-untyped-def]
    mock.route_override = "SQL"
    mock.sql_queue = [
        "SELECT a FROM analytics.regions",
        "SELECT b FROM analytics.regions",
        "SELECT 1",
    ]
    state = await _run(graph, "anything")
    assert state["retries"] == 1 and state["error_code"] == "SQL_FAILED"
    assert len([1 for n, _ in mock.calls if n == "generate_sql"]) == 2
    assert all(n != "synthesize" for n, _ in mock.calls)


async def test_security_violation_has_zero_retries_and_is_never_shown_to_llm(
    graph, mock: MockLLM
) -> None:  # type: ignore[no-untyped-def]
    mock.route_override = "SQL"
    mock.sql_queue = ["DROP TABLE analytics.orders", "SELECT 1 FROM orders"]
    state = await _run(graph, "please clean up the orders table")
    assert state["blocked"] is True and state.get("retries", 0) == 0
    assert state["answer"] == BLOCKED_TEXT and state.get("executed_sql") is None
    calls = [(n, d) for n, d in mock.calls]
    assert [n for n, _ in calls].count("generate_sql") == 1  # no retry
    assert all(n != "synthesize" for n, _ in calls)  # no LLM synthesis
    assert all(d.get("error") is None for n, d in calls if n == "generate_sql")
    assert "DROP" not in json.dumps([d for n, d in calls if n != "generate_sql"], default=str)


async def test_timeout_is_terminal_but_not_blocked(
    db: Database, settings: Settings, mock: MockLLM
) -> None:
    fast = settings.model_copy(update={"sql_timeout_ms": 300})
    graph = build_graph(GraphDeps(provider=mock, db=db, settings=fast))
    mock.route_override = "SQL"
    mock.sql_queue = [
        "SELECT COUNT(*) FROM orders a JOIN orders b ON a.quarter = b.quarter "
        "JOIN orders c ON a.quarter = c.quarter JOIN orders d ON a.quarter = d.quarter"
    ]
    state = await _run(graph, "heavy query")
    assert state["error_code"] == "QUERY_TIMEOUT" and not state.get("blocked")
    assert all(n != "synthesize" for n, _ in mock.calls)
