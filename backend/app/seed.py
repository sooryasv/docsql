"""Idempotent seed: demo users, the 500-row analytics dataset (Random(42)) and the two seed PDFs."""

import hashlib
import random
from datetime import date, timedelta
from pathlib import Path

import structlog
from sqlalchemy import select, text

from app.config import Settings
from app.db import Database
from app.errors import DuplicateDocumentError
from app.models import Document
from app.services import auth_service, document_service
from app.services.llm_provider import LLMProvider

log = structlog.get_logger()
ASSETS = Path(__file__).parent / "assets"
SEED_LOCK = 727274
SEED_DOCS = (
    ("q3_commercial_discount_policy.pdf", "Q3 Commercial & Discount Policy"),
    ("regional_sla_revenue_handbook.pdf", "Regional SLA & Revenue Targets Handbook"),
)
REGIONS = [
    ("BLR-S", "Bengaluru", "Karnataka", "SOUTH"), ("CHN-S", "Chennai", "Tamil Nadu", "SOUTH"),
    ("MUM-W", "Mumbai", "Maharashtra", "WEST"), ("PUN-W", "Pune", "Maharashtra", "WEST"),
    ("DEL-N", "Delhi", "Delhi", "NORTH"), ("KOL-E", "Kolkata", "West Bengal", "EAST"),
]  # fmt: skip
# Q3 target = Q3 revenue * factor: >1 misses target, <1 exceeds it; attainment = 1/factor.
TARGET_FACTORS = [1.25, 0.80, 1.05, 0.95, 1.12, 0.88]
PRODUCTS = [
    ("CLD-100", "Cloud Monitor Basic", "CLOUD_OPS", 120_000, 20),
    ("CLD-200", "Cloud Orchestrator", "CLOUD_OPS", 340_000, 25),
    ("CLD-300", "Cloud Cost Optimizer", "CLOUD_OPS", 210_000, 20),
    ("DAT-100", "Data Lake Starter", "DATA_PLATFORM", 180_000, 20),
    ("DAT-200", "Stream Processor", "DATA_PLATFORM", 420_000, 30),
    ("DAT-300", "BI Workbench", "DATA_PLATFORM", 250_000, 25),
    ("SEC-100", "Endpoint Shield", "SECURITY", 95_000, 15),
    ("SEC-200", "Identity Gateway", "SECURITY", 310_000, 25),
    ("SEC-300", "Threat Radar", "SECURITY", 460_000, 30),
    ("AIS-100", "Doc Intelligence", "AI_SUITE", 380_000, 25),
    ("AIS-200", "Forecast Studio", "AI_SUITE", 520_000, 30),
    ("AIS-300", "Assistant Builder", "AI_SUITE", 600_000, 30),
]  # fmt: skip
TIER_CAPS = {"STARTUP": 10, "MID_MARKET": 15, "ENTERPRISE": 25}
QUARTERS = [("2026-Q1", date(2026, 1, 1), 150), ("2026-Q2", date(2026, 4, 1), 160),
            ("2026-Q3", date(2026, 7, 1), 184), ("2026-Q4", date(2026, 10, 1), 6)]  # fmt: skip
OVER_CAP_EVERY = 20  # deterministic: order index % 20 == 7 exceeds the tier cap (25 rows)


def build_orders(rng: random.Random) -> list[dict[str, object]]:
    orders: list[dict[str, object]] = []
    for quarter, start, count in QUARTERS:
        for _ in range(count):
            index = len(orders)
            tier = rng.choices(list(TIER_CAPS), weights=[40, 35, 25])[0]
            cap = TIER_CAPS[tier]
            discount = rng.randint(0, cap)
            if index % OVER_CAP_EVERY == 7:
                discount = min(50, cap + rng.randint(3, 10))
            product = rng.randrange(len(PRODUCTS))
            quantity = rng.randint(1, 12)
            revenue = quantity * PRODUCTS[product][3] * (100 - discount) // 100
            orders.append({
                "code": f"ORD-2026-{index + 1:04d}", "region": rng.randrange(len(REGIONS)),
                "product": product, "date": start + timedelta(days=rng.randint(0, 80)),
                "quarter": quarter, "qty": quantity, "disc": discount, "rev": revenue, "tier": tier,
            })  # fmt: skip
    return orders


async def seed_analytics(db: Database) -> bool:
    """Returns True if data was inserted. Advisory lock makes concurrent seeding safe."""
    async with db.primary.begin() as conn:
        await conn.execute(text("SELECT pg_advisory_xact_lock(:k)"), {"k": SEED_LOCK})
        if (await conn.execute(text("SELECT COUNT(*) FROM analytics.orders"))).scalar_one() > 0:
            return False
        orders = build_orders(random.Random(42))
        q3 = [0] * len(REGIONS)
        for o in orders:
            if o["quarter"] == "2026-Q3":
                q3[int(str(o["region"]))] += int(str(o["rev"]))
        region_params = [
            {"c": c, "city": city, "st": st, "z": z, "t": max(1, round(q3[i] * TARGET_FACTORS[i]))}
            for i, (c, city, st, z) in enumerate(REGIONS)
        ]
        await conn.execute(text(
            "INSERT INTO analytics.regions (region_code, city, state, zone, q3_target_inr) "
            "VALUES (:c, :city, :st, :z, :t)"), region_params)  # fmt: skip
        await conn.execute(text(
            "INSERT INTO analytics.products (sku, name, category, unit_price_inr, max_discount_pct) "
            "VALUES (:s, :n, :c, :p, :d)"),
            [{"s": s, "n": n, "c": c, "p": p, "d": d} for s, n, c, p, d in PRODUCTS])  # fmt: skip
        region_ids = [
            r[0] for r in await conn.execute(text("SELECT id FROM analytics.regions ORDER BY id"))
        ]
        product_ids = [
            r[0] for r in await conn.execute(text("SELECT id FROM analytics.products ORDER BY id"))
        ]
        await conn.execute(text(
            "INSERT INTO analytics.orders (order_code, region_id, product_id, order_date, quarter, "
            "quantity, discount_pct, revenue_inr, customer_tier) "
            "VALUES (:code, :rid, :pid, :date, :quarter, :qty, :disc, :rev, :tier)"),
            [{**o, "rid": region_ids[int(str(o["region"]))], "pid": product_ids[int(str(o["product"]))]}
             for o in orders])  # fmt: skip
    log.info("analytics_seeded", orders=len(orders))
    return True


async def seed_documents(db: Database, provider: LLMProvider, settings: Settings) -> None:
    for filename, title in SEED_DOCS:
        data = (ASSETS / filename).read_bytes()
        digest = hashlib.sha256(data).hexdigest()
        async with db.session_factory() as session:
            existing = await session.scalar(
                select(Document).where(Document.is_system_seed, Document.filename == filename)
            )
            if existing is not None:
                if (
                    existing.embedding_model == provider.embedding_model
                    and existing.file_sha256 == digest
                ):
                    continue
                log.info("seed_reindex", filename=filename, old_model=existing.embedding_model)
                await session.delete(existing)
                await session.commit()
        pages = await document_service.extract_pages(data, settings.pdf_parse_timeout_s)
        try:
            await document_service.index_pages(
                db.session_factory, provider, settings, pages=pages, title=title,
                filename=filename, file_sha256=digest, uploaded_by=None, is_system_seed=True,
            )  # fmt: skip
        except DuplicateDocumentError:
            log.info("seed_document_already_present", filename=filename)  # another worker won


async def run_seed(db: Database, provider: LLMProvider, settings: Settings) -> None:
    async with db.session_factory() as session:
        await auth_service.ensure_demo_users(session)
    await seed_analytics(db)
    await seed_documents(db, provider, settings)
