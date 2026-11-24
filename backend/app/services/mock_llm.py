"""Deterministic mock provider for CI/tests/plumbing. Its outputs are never quality metrics."""

import asyncio
import hashlib
import math
import re

from app.config import EMBEDDING_DIM
from app.errors import LLMUnavailableError
from app.schemas import ChartSpec, RouteDecision, SynthesisOutput
from app.services.llm_provider import SynthesisRequest, l2_normalize

STOPWORDS = {
    "the", "a", "an", "of", "to", "is", "are", "in", "on", "for", "and", "or", "what", "how",
    "do", "does", "can", "by", "with", "at", "be", "it", "this", "that", "from", "as", "our",
}  # fmt: skip
CONCEPTS = {  # tiny synonym table so the vector branch can match paraphrases with zero keyword overlap
    "reimbursement": "refund", "reimburse": "refund", "repayment": "refund", "refunds": "refund",
    "rebate": "discount", "markdown": "discount", "discounts": "discount",
}  # fmt: skip
OUT_OF_SCOPE_WORDS = ("weather", "poem", "joke", "recipe", "capital of", "movie", "who won")
SQL_WORDS = ("revenue", "orders", "how many", "total", "average", "count", "top ", "by region",
             "by quarter", "by product", "delete", "drop ", "truncate", "update ")  # fmt: skip
DOC_WORDS = ("policy", "sla", "refund", "approval", "payment terms", "discount cap", "response time",
             "escalation", "at risk", "bonus", "zone", "handbook", "effective")  # fmt: skip
SQL_REVENUE_BY_REGION = (
    "SELECT r.region_code, SUM(o.revenue_inr) AS revenue_inr FROM analytics.orders o "
    "JOIN analytics.regions r ON r.id = o.region_id GROUP BY r.region_code ORDER BY revenue_inr DESC"
)
SQL_BY_QUARTER = (
    "SELECT quarter, COUNT(*) AS orders, SUM(revenue_inr) AS revenue_inr FROM analytics.orders "
    "GROUP BY quarter ORDER BY quarter"
)
SQL_OVER_CAP = (
    "SELECT customer_tier, COUNT(*) AS over_cap_orders FROM analytics.orders "
    "WHERE quarter = '2026-Q3' AND ((customer_tier = 'STARTUP' AND discount_pct > 10) "
    "OR (customer_tier = 'MID_MARKET' AND discount_pct > 15) "
    "OR (customer_tier = 'ENTERPRISE' AND discount_pct > 25)) "
    "GROUP BY customer_tier ORDER BY over_cap_orders DESC"
)
SQL_TARGETS = (
    "SELECT r.region_code, r.q3_target_inr, SUM(o.revenue_inr) AS q3_revenue_inr "
    "FROM analytics.regions r JOIN analytics.orders o ON o.region_id = r.id "
    "WHERE o.quarter = '2026-Q3' GROUP BY r.region_code, r.q3_target_inr ORDER BY q3_revenue_inr DESC"
)


def _has_word(text: str, words: tuple[str, ...]) -> bool:
    return any(re.search(rf"\b{re.escape(w.strip())}\b", text) for w in words)


def hash_embedding(text: str) -> list[float]:
    counts: dict[int, float] = {}
    for token in re.findall(r"[a-z0-9]+", text.lower()):
        token = CONCEPTS.get(token, token)
        if token in STOPWORDS:
            continue
        digest = hashlib.blake2b(token.encode(), digest_size=8).digest()
        value = int.from_bytes(digest, "big")
        sign = 1.0 if (value >> 40) & 1 else -1.0
        counts[value % EMBEDDING_DIM] = counts.get(value % EMBEDDING_DIM, 0.0) + sign
    vector = [0.0] * EMBEDDING_DIM
    for index, weight in counts.items():
        vector[index] = math.copysign(1 + math.log(abs(weight)), weight) if weight else 0.0
    return l2_normalize(vector)


class MockLLM:
    embedding_model = "mock-hash-768"

    def __init__(self) -> None:
        self.calls: list[tuple[str, dict[str, object]]] = []
        # Test hooks
        self.sql_queue: list[str] = []
        self.route_override: str | None = None
        self.fabricate_citations = False
        self.chart_override: ChartSpec | None = None
        self.delay_s = 0.0
        self.fail_with: Exception | None = None
        self.cancelled = False

    async def _enter(self, name: str, **details: object) -> None:
        self.calls.append((name, details))
        if self.fail_with is not None:
            raise self.fail_with
        try:
            await asyncio.sleep(self.delay_s)
        except asyncio.CancelledError:
            self.cancelled = True
            raise

    async def embed_documents(self, texts: list[str]) -> list[list[float]]:
        if self.fail_with is not None and isinstance(self.fail_with, LLMUnavailableError):
            raise self.fail_with
        return [hash_embedding(t) for t in texts]

    async def embed_query(self, text: str) -> list[float]:
        return hash_embedding(text)

    async def route(
        self, question: str, doc_titles: tuple[str, ...] = ()
    ) -> tuple[RouteDecision, int]:
        await self._enter("route", question=question)
        low = question.lower()
        if self.route_override:
            route = self.route_override
        elif any(w in low for w in OUT_OF_SCOPE_WORDS):
            route = "OUT_OF_SCOPE"
        else:
            has_sql, has_doc = _has_word(low, SQL_WORDS), _has_word(low, DOC_WORDS)
            route = (
                "HYBRID"
                if has_sql and has_doc
                else "SQL"
                if has_sql
                else "DOC"
                if has_doc
                else "OUT_OF_SCOPE"
            )
        return RouteDecision.model_validate({"route": route, "reason": "mock router"}), 10

    async def generate_sql(
        self, question: str, schema_text: str, error: str | None
    ) -> tuple[str, int]:
        await self._enter("generate_sql", question=question, schema_text=schema_text, error=error)
        if self.sql_queue:
            return self.sql_queue.pop(0), 10
        low = question.lower()
        if any(w in low for w in ("delete", "drop ", "truncate")):
            return "DROP TABLE analytics.orders", 10
        if "discount" in low and any(w in low for w in ("exceed", "above", "over", "cap")):
            return SQL_OVER_CAP, 10
        if "target" in low:
            return SQL_TARGETS, 10
        if "region" in low:
            return SQL_REVENUE_BY_REGION, 10
        return SQL_BY_QUARTER, 10

    async def synthesize(self, request: SynthesisRequest) -> tuple[SynthesisOutput, int]:
        await self._enter("synthesize", question=request.question, prompt=request.prompt)
        parts: list[str] = []
        cited: list[int] = []
        if request.chunk_ids:
            parts.append("From the retrieved passage: " + request.chunk_texts[0][:240].strip())
            cited.append(request.chunk_ids[0])
        if self.fabricate_citations:
            cited.append(999_999_999)
        chart = self.chart_override
        if request.rows:
            parts.append(f"The query returned {len(request.rows)} row(s); first row: " + ", ".join(
                f"{c}={v}" for c, v in zip(request.columns, request.rows[0], strict=False)))  # fmt: skip
            chart = chart or _auto_chart(request.columns, request.rows)
        answer = " ".join(parts) or "No supporting data was available."
        output = SynthesisOutput(answer_text=answer, cited_chunk_ids=cited, chart_spec=chart)
        return output, max(1, len(request.prompt) // 4)


def _auto_chart(columns: tuple[str, ...], rows: tuple[tuple[object, ...], ...]) -> ChartSpec | None:
    numeric = [
        c for i, c in enumerate(columns)
        if isinstance(rows[0][i], int | float) and not isinstance(rows[0][i], bool)
    ]  # fmt: skip
    labels = [c for c in columns if c not in numeric]
    if not numeric or not labels:
        return None
    kind = "line" if any(k in labels[0] for k in ("quarter", "month", "date")) else "bar"
    return ChartSpec.model_validate({"type": kind, "x": labels[0], "y": numeric[:2], "title": ""})
