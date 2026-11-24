"""Auth, CSRF, RBAC/IDOR, upload validation matrix, quotas, atomicity and seeding."""

import asyncio
import uuid

import httpx
import pytest
from sqlalchemy import func, select, text

from app.config import Settings
from app.db import Database
from app.errors import DailyQuotaExceededError, GlobalQuotaExceededError, LLMUnavailableError
from app.models import Document, DocumentChunk, User
from app.security import DUMMY_HASH
from app.seed import seed_analytics, seed_documents
from app.services import auth_service, document_service, usage_service
from app.services.mock_llm import MockLLM
from tests.conftest import (
    create_user,
    encrypted_pdf,
    make_app,
    make_pdf,
    new_client,
    register,
    upload,
)

TEXT_PAGE = "Refund requests are accepted within thirty days of purchase. " * 6


# ---------- auth ----------
async def test_register_login_me_logout_and_cookie_flags(client: httpx.AsyncClient) -> None:
    resp = await client.post(
        "/api/v1/auth/register",
        json={"email": "  Foo@Example.COM ", "password": "password123", "full_name": "Foo"},
    )
    assert resp.status_code == 201 and resp.json()["email"] == "foo@example.com"
    cookie = resp.headers["set-cookie"].lower()
    assert (
        "httponly" in cookie
        and "samesite=lax" in cookie
        and "secure" not in cookie.replace("samesite", "")
    )
    assert (await client.get("/api/v1/auth/me")).json()["user"]["role"] == "ANALYST"
    assert (await client.post("/api/v1/auth/logout")).status_code == 204
    client.cookies.clear()
    assert (await client.get("/api/v1/auth/me")).status_code == 401
    login = await client.post(
        "/api/v1/auth/login", json={"email": "FOO@example.com", "password": "password123"}
    )
    assert login.status_code == 200
    assert (
        await client.post(
            "/api/v1/auth/login", json={"email": "foo@example.com", "password": "wrong-pass"}
        )
    ).status_code == 401


async def test_register_validation_and_duplicates(client: httpx.AsyncClient) -> None:
    base = {"email": "dup@example.com", "password": "password123", "full_name": "D"}
    assert (
        await client.post("/api/v1/auth/register", json={**base, "role": "ADMIN"})
    ).status_code == 422
    assert (
        await client.post("/api/v1/auth/register", json={**base, "password": "short"})
    ).status_code == 422
    assert (
        await client.post("/api/v1/auth/register", json={**base, "email": "not-an-email"})
    ).status_code == 422
    assert (await client.post("/api/v1/auth/register", json=base)).status_code == 201
    dup = await client.post("/api/v1/auth/register", json={**base, "email": "DUP@example.com"})
    assert dup.status_code == 409 and dup.json()["error"]["code"] == "EMAIL_TAKEN"


async def test_unknown_email_login_still_verifies_a_dummy_hash(
    client: httpx.AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    seen: list[str] = []
    real = auth_service.verify_password

    async def spy(password: str, hashed: str) -> bool:
        seen.append(hashed)
        return await real(password, hashed)

    monkeypatch.setattr(auth_service, "verify_password", spy)
    resp = await client.post(
        "/api/v1/auth/login", json={"email": "ghost@example.com", "password": "password123"}
    )
    assert resp.status_code == 401 and resp.json()["error"]["code"] == "INVALID_CREDENTIALS"
    assert seen == [DUMMY_HASH]  # timing parity: hashing work happens even for unknown emails


async def test_demo_login_and_unusable_password(
    client: httpx.AsyncClient, demo_users: None, app
) -> None:  # type: ignore[no-untyped-def]
    resp = await client.post("/api/v1/auth/demo-login", json={"role": "ADMIN"})
    assert resp.status_code == 200 and resp.json()["role"] == "ADMIN"
    pw = await client.post(
        "/api/v1/auth/login", json={"email": "demo.admin@docsql.dev", "password": "anything12"}
    )
    assert pw.status_code == 401  # demo accounts have unusable password hashes
    app.state.settings = app.state.settings.model_copy(update={"enable_demo_tools": False})
    assert (await client.post("/api/v1/auth/demo-login", json={"role": "ADMIN"})).status_code == 404


async def test_unauthenticated_requests_get_envelope_with_request_id(
    client: httpx.AsyncClient,
) -> None:
    resp = await client.get("/api/v1/documents")
    body = resp.json()["error"]
    assert resp.status_code == 401 and body["code"] == "UNAUTHENTICATED"
    assert body["request_id"] == resp.headers["x-request-id"]
    assert (await client.get("/api/v1/nope")).json()["error"]["code"] == "NOT_FOUND"


async def test_csrf_origin_guard(app) -> None:  # type: ignore[no-untyped-def]
    payload = {"email": "csrf@example.com", "password": "password123", "full_name": "C"}
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://localhost:8000"
    ) as raw:
        missing = await raw.post("/api/v1/auth/register", json=payload)
        foreign = await raw.post(
            "/api/v1/auth/register", json=payload, headers={"Origin": "https://evil.example"}
        )
        referer = await raw.post(
            "/api/v1/auth/register",
            json=payload,
            headers={"Referer": "http://localhost:5173/login"},
        )
        safe_get = await raw.get("/healthz")
    assert missing.status_code == 403 and missing.json()["error"]["code"] == "CSRF_ORIGIN_REJECTED"
    assert foreign.status_code == 403
    assert referer.status_code == 201  # Referer fallback from an allowed origin is accepted
    assert safe_get.status_code == 200


async def test_healthz_checks_both_pools_and_pgvector(client: httpx.AsyncClient) -> None:
    body = (await client.get("/healthz")).json()
    assert body["status"] == "ok"
    assert body["checks"]["primary"] == "ok" and body["checks"]["readonly"] == "ok"
    assert not body["checks"]["pgvector"].startswith("bad")


# ---------- upload validation matrix ----------
async def test_upload_success_and_listing(client: httpx.AsyncClient) -> None:
    await register(client, "up@example.com")
    resp = await upload(client, make_pdf([TEXT_PAGE, TEXT_PAGE]), "My_Policy.pdf")
    assert resp.status_code == 201
    doc = resp.json()
    assert doc["title"] == "My Policy" and doc["page_count"] == 2 and doc["chunk_count"] >= 2
    assert doc["embedding_model"] == "mock-hash-768" and doc["is_system_seed"] is False
    assert [d["id"] for d in (await client.get("/api/v1/documents")).json()] == [doc["id"]]


async def test_upload_rejections(client: httpx.AsyncClient) -> None:
    await register(client, "rej@example.com")
    big = await upload(client, b"%PDF-" + b"0" * (5 * 1024 * 1024))
    assert big.status_code == 413 and big.json()["error"]["code"] == "FILE_TOO_LARGE"
    cases = {
        "INVALID_PDF": [b"not a pdf at all", b"%PDF-1.4 truncated garbage", encrypted_pdf()],
        "TOO_MANY_PAGES": [make_pdf([TEXT_PAGE] * 16)],
        "SCANNED_PDF_UNSUPPORTED": [make_pdf(["", ""])],
    }
    for code, payloads in cases.items():
        for data in payloads:
            resp = await upload(client, data)
            assert resp.status_code == 422 and resp.json()["error"]["code"] == code, (
                code,
                resp.text,
            )
    assert (await client.get("/api/v1/documents")).json() == []


def test_nul_bytes_are_stripped_from_extracted_text(monkeypatch: pytest.MonkeyPatch) -> None:
    class Page:
        def extract_text(self) -> str:
            return "re\x00fund\x00 window"

    class Reader:
        is_encrypted = False
        pages = [Page()]

        def __init__(self, _stream: object) -> None: ...

    monkeypatch.setattr(document_service, "PdfReader", Reader)
    assert document_service._extract_sync(b"%PDF-1.4 fake") == ["refund window"]


async def test_duplicate_upload_and_document_cap(
    client: httpx.AsyncClient,
    app,
    db: Database,
    settings: Settings,
    mock: MockLLM,  # type: ignore[no-untyped-def]
) -> None:
    app.state.settings = settings.model_copy(update={"max_docs_per_user": 2})
    await register(client, "cap@example.com")
    first = make_pdf([TEXT_PAGE])
    assert (await upload(client, first, "a.pdf")).status_code == 201
    dup = await upload(client, first, "a-again.pdf")
    assert dup.status_code == 409 and dup.json()["error"]["code"] == "DUPLICATE_DOCUMENT"
    assert (await upload(client, make_pdf([TEXT_PAGE + "b"]), "b.pdf")).status_code == 201
    over = await upload(client, make_pdf([TEXT_PAGE + "c"]), "c.pdf")
    assert over.status_code == 409 and over.json()["error"]["code"] == "DOCUMENT_LIMIT"


async def test_same_file_by_two_users_is_allowed(app) -> None:  # type: ignore[no-untyped-def]
    data = make_pdf([TEXT_PAGE])
    for email in ("s1@example.com", "s2@example.com"):
        async with await new_client(app) as c:
            await register(c, email)
            assert (await upload(c, data)).status_code == 201


async def test_upload_is_atomic_when_embedding_fails(
    client: httpx.AsyncClient, mock: MockLLM, db: Database
) -> None:
    await register(client, "atomic@example.com")
    mock.fail_with = LLMUnavailableError()
    resp = await upload(client, make_pdf([TEXT_PAGE]))
    assert resp.status_code == 503 and resp.json()["error"]["code"] == "LLM_UNAVAILABLE"
    async with db.session_factory() as session:
        assert await session.scalar(select(func.count()).select_from(Document)) == 0
        assert await session.scalar(select(func.count()).select_from(DocumentChunk)) == 0


async def test_concurrent_uploads_cannot_exceed_document_cap(
    app,
    db: Database,
    settings: Settings,
    mock: MockLLM,  # type: ignore[no-untyped-def]
) -> None:
    app.state.settings = settings.model_copy(update={"max_docs_per_user": 2})
    async with await new_client(app) as c:
        await register(c, "race@example.com")
        results = await asyncio.gather(
            *[upload(c, make_pdf([TEXT_PAGE + str(i)])) for i in range(6)]
        )
    assert sorted(r.status_code for r in results).count(201) == 2


# ---------- RBAC / IDOR ----------
async def _two_users_with_a_doc(app):  # type: ignore[no-untyped-def]
    a, b = await new_client(app), await new_client(app)
    await register(a, "owner@example.com")
    await register(b, "intruder@example.com")
    doc = (await upload(a, make_pdf([TEXT_PAGE]))).json()
    return a, b, doc


async def test_idor_other_users_document_chunk_and_delete_are_404(app, db: Database) -> None:  # type: ignore[no-untyped-def]
    a, b, doc = await _two_users_with_a_doc(app)
    async with db.session_factory() as session:
        chunk_id = await session.scalar(
            select(DocumentChunk.id).where(DocumentChunk.document_id == uuid.UUID(doc["id"]))
        )
    try:
        assert (await a.get(f"/api/v1/documents/{doc['id']}/chunks/{chunk_id}")).status_code == 200
        assert (await b.get(f"/api/v1/documents/{doc['id']}/chunks/{chunk_id}")).status_code == 404
        assert (await b.delete(f"/api/v1/documents/{doc['id']}")).status_code == 404
        assert (await b.get("/api/v1/documents")).json() == []
        assert len((await a.get("/api/v1/documents")).json()) == 1  # still there
        # chunk id that belongs to a DIFFERENT (visible) document => 404, not a leak
        own = (await upload(b, make_pdf(["Other doc text. " * 40]))).json()
        assert (await b.get(f"/api/v1/documents/{own['id']}/chunks/{chunk_id}")).status_code == 404
    finally:
        await a.aclose()
        await b.aclose()


async def test_seed_documents_are_visible_but_cannot_be_deleted(
    client: httpx.AsyncClient, db: Database, settings: Settings, mock: MockLLM
) -> None:
    await seed_documents(db, mock, settings)
    await register(client, "seedview@example.com")
    docs = (await client.get("/api/v1/documents")).json()
    assert len(docs) == 2 and all(d["is_system_seed"] for d in docs)
    resp = await client.delete(f"/api/v1/documents/{docs[0]['id']}")
    assert resp.status_code == 403 and resp.json()["error"]["code"] == "SEED_DOCUMENT_PROTECTED"
    chunk_id = None
    async with db.session_factory() as session:
        chunk_id = await session.scalar(
            select(DocumentChunk.id)
            .where(DocumentChunk.document_id == uuid.UUID(docs[0]["id"]))
            .limit(1)
        )
    assert (
        await client.get(f"/api/v1/documents/{docs[0]['id']}/chunks/{chunk_id}")
    ).status_code == 200


async def test_owner_can_delete_own_document_and_chunks_cascade(
    client: httpx.AsyncClient, db: Database
) -> None:
    await register(client, "del@example.com")
    doc = (await upload(client, make_pdf([TEXT_PAGE]))).json()
    assert (await client.delete(f"/api/v1/documents/{doc['id']}")).status_code == 204
    async with db.session_factory() as session:
        assert await session.scalar(select(func.count()).select_from(DocumentChunk)) == 0


async def test_admin_has_no_cross_tenant_delete(app, db: Database, demo_users: None) -> None:  # type: ignore[no-untyped-def]
    a, _b, doc = await _two_users_with_a_doc(app)
    admin = await new_client(app)
    async with admin:
        await admin.post("/api/v1/auth/demo-login", json={"role": "ADMIN"})
        assert (await admin.delete(f"/api/v1/documents/{doc['id']}")).status_code == 404
        assert (await admin.get("/api/v1/metrics")).status_code == 200
    await a.aclose()
    await _b.aclose()


# ---------- atomic quotas ----------
async def test_quota_is_atomic_under_20_parallel_requests(db: Database, settings: Settings) -> None:
    live = settings.model_copy(
        update={"mock_llm": False, "daily_query_limit": 5, "global_daily_query_limit": 100}
    )
    user = await create_user(db, "race-quota@example.com")
    results = await asyncio.gather(
        *[usage_service.consume_quota(db.session_factory, user.id, live) for _ in range(20)],
        return_exceptions=True,
    )
    assert sum(r is None for r in results) == 5
    assert all(isinstance(r, DailyQuotaExceededError) for r in results if r is not None)
    assert await usage_service.remaining_quota(db.session_factory, user.id, live) == 0


async def test_global_quota_failure_rolls_back_user_increment(
    db: Database, settings: Settings
) -> None:
    live = settings.model_copy(
        update={"mock_llm": False, "daily_query_limit": 10, "global_daily_query_limit": 3}
    )
    user = await create_user(db, "global-quota@example.com")
    for _ in range(3):
        await usage_service.consume_quota(db.session_factory, user.id, live)
    with pytest.raises(GlobalQuotaExceededError):
        await usage_service.consume_quota(db.session_factory, user.id, live)
    assert await usage_service.remaining_quota(db.session_factory, user.id, live) == 7  # not 6


async def test_mock_mode_disables_quota(db: Database, settings: Settings) -> None:
    user = await create_user(db, "mockq@example.com")
    tight = settings.model_copy(update={"mock_llm": True, "daily_query_limit": 1})
    for _ in range(3):
        await usage_service.consume_quota(db.session_factory, user.id, tight)
    assert await usage_service.remaining_quota(db.session_factory, user.id, tight) is None


# ---------- seed ----------
async def test_seed_is_idempotent_and_reindexes_on_model_change(
    db: Database, settings: Settings, mock: MockLLM
) -> None:
    assert await seed_analytics(db) is False  # already seeded by the session fixture
    async with db.primary.connect() as conn:
        counts = [
            (await conn.execute(text(f"SELECT COUNT(*) FROM analytics.{t}"))).scalar_one()
            for t in ("orders", "regions", "products")
        ]
        over_cap = (
            await conn.execute(
                text(
                    "SELECT COUNT(*) FROM analytics.orders WHERE (customer_tier='STARTUP' AND discount_pct>10) "
                    "OR (customer_tier='MID_MARKET' AND discount_pct>15) OR (customer_tier='ENTERPRISE' AND discount_pct>25)"
                )
            )
        ).scalar_one()
        met = (
            await conn.execute(
                text(
                    "SELECT COUNT(*) FILTER (WHERE s.rev >= r.q3_target_inr), COUNT(*) FILTER (WHERE s.rev < r.q3_target_inr) "
                    "FROM analytics.regions r JOIN (SELECT region_id, SUM(revenue_inr) rev FROM analytics.orders "
                    "WHERE quarter='2026-Q3' GROUP BY region_id) s ON s.region_id = r.id"
                )
            )
        ).one()
    assert counts == [500, 6, 12] and over_cap >= 20 and met[0] >= 1 and met[1] >= 1

    await seed_documents(db, mock, settings)
    await seed_documents(db, mock, settings)
    async with db.session_factory() as session:
        docs = (await session.scalars(select(Document))).all()
        assert len(docs) == 2 and {d.embedding_model for d in docs} == {"mock-hash-768"}

    class OtherModel(MockLLM):
        embedding_model = "mock-other-768"

    await seed_documents(db, OtherModel(), settings)
    async with db.session_factory() as session:
        docs = (await session.scalars(select(Document))).all()
        assert len(docs) == 2 and {d.embedding_model for d in docs} == {"mock-other-768"}
        chunk_total = await session.scalar(select(func.count()).select_from(DocumentChunk))
        assert chunk_total == sum(d.chunk_count for d in docs)  # old chunks were cascaded away


async def test_demo_users_seeded_idempotently(db: Database) -> None:
    async with db.session_factory() as session:
        await auth_service.ensure_demo_users(session)
        await auth_service.ensure_demo_users(session)
        emails = sorted((await session.scalars(select(User.email))).all())
    assert emails == ["demo.admin@docsql.dev", "demo.analyst@docsql.dev"]


async def test_schema_endpoint_lists_analytics_tables(client: httpx.AsyncClient) -> None:
    await register(client, "schema@example.com")
    body = (await client.get("/api/v1/schema")).json()
    assert [t["name"] for t in body["tables"]] == ["orders", "products", "regions"]
    assert body["tables"][0]["row_count"] == 500 and body["tables"][0]["columns"]


async def test_make_app_isolated_state(db: Database, settings: Settings, mock: MockLLM) -> None:
    app_a, app_b = make_app(db, settings, mock), make_app(db, settings, MockLLM())
    assert app_a.state.provider is not app_b.state.provider
