"""AST guard for LLM-generated SQL. Pure function; the executor runs ONLY the re-emitted SQL.

Rejections that signal an attack raise SecurityViolationError (terminal, never retried).
Plain syntax problems and unknown table names raise SQLSyntaxOrSchemaError (one bounded retry).
"""

from dataclasses import dataclass
from typing import NoReturn

import sqlglot
from sqlglot import exp
from sqlglot.errors import SqlglotError

from app.errors import SecurityViolationError, SQLSyntaxOrSchemaError

MAX_SQL_CHARS = 2000
MAX_NODES = 400
MAX_TABLE_REFS = 4
ALLOWED_TABLES = frozenset({"regions", "products", "orders"})
ALLOWED_SCHEMAS = frozenset({"", "analytics"})

BLOCKED_NODES: tuple[type[exp.Expression], ...] = (
    exp.Insert, exp.Update, exp.Delete, exp.Drop, exp.Alter, exp.Create, exp.Command, exp.Grant,
    exp.Copy, exp.TruncateTable, exp.Into, exp.Merge, exp.Lock, exp.Fetch, exp.Set, exp.Use,
    exp.Transaction, exp.Commit, exp.Rollback, exp.Kill, exp.Analyze, exp.Describe, exp.Show,
    exp.Lateral, exp.Unnest, exp.Values, exp.TableSample, exp.Pivot,
)  # fmt: skip
SET_OPERATIONS = (exp.Union, exp.Intersect, exp.Except)
PARAMETER_NODES = (exp.Placeholder, exp.Parameter, exp.SessionParameter)
# Allowlist by sqlglot CLASS (not by name string). Operators/columns/literals are not Func nodes.
ALLOWED_FUNCTIONS: tuple[type[exp.Expression], ...] = (
    exp.Count, exp.Sum, exp.Avg, exp.Min, exp.Max, exp.Coalesce, exp.Round, exp.DateTrunc,
    exp.TimestampTrunc, exp.Extract, exp.Lower, exp.Upper, exp.Abs, exp.Nullif, exp.Cast, exp.Case,
    exp.And, exp.Or,  # logical operators are Func subclasses in sqlglot 30
)  # fmt: skip
T = exp.DataType.Type
ALLOWED_CAST_TYPES = frozenset({
    T.INT, T.BIGINT, T.SMALLINT, T.DECIMAL, T.DOUBLE, T.FLOAT, T.TEXT, T.VARCHAR, T.CHAR,
    T.DATE, T.TIMESTAMP, T.TIMESTAMPTZ, T.BOOLEAN,
})  # fmt: skip


@dataclass(frozen=True)
class ValidatedSQL:
    sql: str
    tables: tuple[str, ...]
    limit: int


def _block(reason: str) -> NoReturn:
    raise SecurityViolationError(reason)


def _identifier_name(identifier: exp.Expression | None) -> str:
    """Unquoted identifiers fold to lower case in Postgres; quoted ones are case-sensitive."""
    if not isinstance(identifier, exp.Identifier):
        return ""
    return identifier.name if identifier.args.get("quoted") else identifier.name.lower()


def _parse(sql: str) -> exp.Select:
    if len(sql) > MAX_SQL_CHARS:
        _block(f"statement longer than {MAX_SQL_CHARS} characters")
    cleaned = sql.strip().rstrip(";").strip()
    if not cleaned:
        raise SQLSyntaxOrSchemaError("empty statement")
    try:
        statements = sqlglot.parse(cleaned, read="postgres")
    except RecursionError:
        _block("statement nesting depth exceeded")
    except SqlglotError as exc:
        raise SQLSyntaxOrSchemaError(f"SQL could not be parsed: {str(exc)[:200]}") from exc
    if len(statements) != 1 or statements[0] is None:
        _block("exactly one statement is allowed")
    root = statements[0]
    if isinstance(root, SET_OPERATIONS):
        _block("set operations (UNION/INTERSECT/EXCEPT) are not allowed")
    if not isinstance(root, exp.Select):
        _block(f"only SELECT statements are allowed (got {type(root).__name__})")
    if sum(1 for _ in root.walk()) > MAX_NODES:
        _block(f"statement exceeds {MAX_NODES} AST nodes")
    return root


def _check_nodes(root: exp.Select) -> None:
    for node in root.walk():
        node.comments = None  # re-emitted SQL carries no comments
        if isinstance(node, BLOCKED_NODES):
            _block(f"{type(node).__name__} is not allowed")
        if isinstance(node, SET_OPERATIONS):
            _block("set operations are not allowed")
        if isinstance(node, exp.Window):
            _block("window functions are not allowed")
        if isinstance(node, PARAMETER_NODES):
            _block("bind parameters and session references are not allowed")
        if isinstance(node, exp.With) and node.args.get("recursive"):
            _block("WITH RECURSIVE is not allowed")
        if isinstance(node, exp.DataType) and node.this not in ALLOWED_CAST_TYPES:
            _block(f"cast to type {node.sql(dialect='postgres')} is not allowed")


def _check_functions(root: exp.Select) -> None:
    for node in root.walk():
        if not isinstance(node, exp.Func):
            continue
        if isinstance(node, exp.If) and isinstance(node.parent, exp.Case):
            continue  # CASE WHEN is represented with If children
        if not isinstance(node, ALLOWED_FUNCTIONS):
            _block(f"function {node.sql_name()} is not allowed")


def _check_joins(root: exp.Select) -> None:
    for select in root.find_all(exp.Select):
        source = select.args.get("from_")
        if source is not None and len(source.expressions) > 1:
            _block("comma joins are not allowed")
    for join in root.find_all(exp.Join):
        if str(join.args.get("kind") or "").upper() == "CROSS":
            _block("CROSS JOIN is not allowed")
        condition = join.args.get("on")
        if condition is None:
            _block("every JOIN needs an ON condition")
        if not any(True for _ in condition.find_all(exp.Column)):
            _block("JOIN condition must reference at least one column")


def _check_tables(root: exp.Select) -> tuple[str, ...]:
    cte_names = {c.alias.lower() for c in root.find_all(exp.CTE)}
    for name in cte_names:
        if name.startswith("pg_") or name in ALLOWED_TABLES or name == "information_schema":
            _block(f"CTE name {name} shadows a catalog or analytics table")
    physical: list[str] = []
    for table in root.find_all(exp.Table):
        name = _identifier_name(table.this)
        schema = _identifier_name(table.args.get("db"))
        if table.args.get("catalog") is not None or not isinstance(table.this, exp.Identifier):
            _block("table functions and catalog-qualified tables are not allowed")
        if not schema and name in cte_names:
            continue
        if schema not in ALLOWED_SCHEMAS:
            _block(f"schema {schema} is not allowed")
        if name.startswith("pg_") or name in {"information_schema", "users"}:
            _block(f"table {name} is not allowed")
        if name not in ALLOWED_TABLES:
            raise SQLSyntaxOrSchemaError(
                f"unknown table {name!r}; use only regions, products, orders"
            )
        physical.append(name)
    if len(physical) > MAX_TABLE_REFS:
        _block(f"more than {MAX_TABLE_REFS} table references")
    return tuple(sorted(set(physical)))


def _apply_limit(root: exp.Select, max_rows: int) -> int:
    """Only the ROOT select's LIMIT bounds the result; subquery limits are irrelevant to the cap."""
    offset = root.args.get("offset")
    if offset is not None and not (
        isinstance(offset.expression, exp.Literal) and not offset.expression.is_string
    ):
        _block("OFFSET must be a literal number")
    limit = root.args.get("limit")
    if limit is None:
        root.set("limit", exp.Limit(expression=exp.Literal.number(max_rows)))
        return max_rows
    value = limit.expression
    if not isinstance(value, exp.Literal) or value.is_string or not value.name.isdigit():
        _block("LIMIT must be a literal integer")
    effective = min(int(value.name), max_rows)
    limit.set("expression", exp.Literal.number(effective))
    return effective


def validate_sql(sql: str, max_rows: int = 100) -> ValidatedSQL:
    root = _parse(sql)
    _check_nodes(root)
    _check_functions(root)
    _check_joins(root)
    tables = _check_tables(root)
    limit = _apply_limit(root, max_rows)
    return ValidatedSQL(sql=root.sql(dialect="postgres"), tables=tables, limit=limit)
