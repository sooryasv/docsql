"""Shared fixtures. Tests run against REAL Postgres + pgvector; only the LLM boundary is mocked."""

import os
import uuid
from collections.abc import AsyncIterator, Callable, Iterator
from io import BytesIO
from pathlib import Path

BACKEND = Path(__file__).resolve().parent.parent
TEST_DB = os.environ.get(
    "TEST_DATABASE_URL", "postgresql://postgres:postgres@localhost:5432/docsql_test"
)
TEST_RO = os.environ.get(
    "TEST_ANALYTICS_READONLY_DATABASE_URL",
    "postgresql://docsql_readonly:docsql_readonly@localhost:5432/docsql_test",
)
os.environ.update(
    DATABASE_URL=TEST_DB,
    ANALYTICS_READONLY_DATABASE_URL=TEST_RO,
    MOCK_LLM="true",
    RUN_SEED_ON_STARTUP="false",
    JWT_SECRET="test-secret-test-secret-test-secret-123",
    ALLOWED_ORIGINS="http://localhost:5173,http://localhost:8000",
    ENABLE_DEMO_TOOLS="true",
    USE_NULL_POOL="false",
)

import httpx  # noqa: E402
import pytest  # noqa: E402
import pytest_asyncio  # noqa: E402
from alembic.config import Config  # noqa: E402
from fastapi import FastAPI  # noqa: E402
from fpdf import FPDF  # noqa: E402
from sqlalchemy import text  # noqa: E402

from alembic import command  # noqa: E402
from app.config import Settings, get_settings  # noqa: E402
from app.db import Database  # noqa: E402
from app.main import create_app  # noqa: E402
from app.models import User  # noqa: E402
from app.seed import seed_analytics  # noqa: E402
from app.services import auth_service  # noqa: E402
from app.services.agent_graph import GraphDeps, build_graph  # noqa: E402
from app.services.mock_llm import MockLLM  # noqa: E402

ORIGIN = {"Origin": "http://localhost:8000"}


@pytest.fixture(scope="session", autouse=True)
def migrated() -> None:
    cfg = Config(str(BACKEND / "alembic.ini"))
    cfg.set_main_option("script_location", str(BACKEND / "alembic"))
    command.upgrade(cfg, "head")


@pytest.fixture(scope="session")
def settings() -> Settings:
    get_settings.cache_clear()
    return get_settings()


@pytest_asyncio.fixture(scope="session")
async def db(settings: Settings, migrated: None) -> AsyncIterator[Database]:
    database = Database.create(settings)
    async with database.primary.connect() as conn:  # startup assertion: pgvector >= 0.8.0
        version = (
            await conn.execute(text("SELECT extversion FROM pg_extension WHERE extname='vector'"))
        ).scalar_one()
    assert tuple(int(p) for p in version.split(".")[:2]) >= (0, 8), f"pgvector {version} < 0.8.0"
    await seed_analytics(database)
    yield database
    await database.dispose()


@pytest_asyncio.fixture(autouse=True)
async def clean_db(db: Database) -> None:
    async with db.primary.begin() as conn:
        await conn.execute(
            text(
                "TRUNCATE public.users, public.documents, public.document_chunks, "
                "public.query_logs, public.usage_counters RESTART IDENTITY CASCADE"
            )
        )


@pytest.fixture
def mock() -> MockLLM:
    return MockLLM()


@pytest.fixture
def deps(db: Database, settings: Settings, mock: MockLLM) -> GraphDeps:
    return GraphDeps(provider=mock, db=db, settings=settings)


@pytest.fixture
def graph(deps: GraphDeps):  # type: ignore[no-untyped-def]
    return build_graph(deps)


def make_app(db: Database, settings: Settings, mock: MockLLM) -> FastAPI:
    app = create_app(settings, mock)
    app.state.settings, app.state.db, app.state.provider = settings, db, mock
    app.state.graph = build_graph(GraphDeps(provider=mock, db=db, settings=settings))
    return app


@pytest.fixture
def app(db: Database, settings: Settings, mock: MockLLM) -> FastAPI:
    return make_app(db, settings, mock)


@pytest_asyncio.fixture
async def client(app: FastAPI) -> AsyncIterator[httpx.AsyncClient]:
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(
        transport=transport, base_url="http://localhost:8000", headers=ORIGIN
    ) as c:
        yield c


@pytest_asyncio.fixture
async def demo_users(db: Database) -> None:
    async with db.session_factory() as session:
        await auth_service.ensure_demo_users(session)


async def new_client(app: FastAPI) -> httpx.AsyncClient:
    transport = httpx.ASGITransport(app=app)
    return httpx.AsyncClient(transport=transport, base_url="http://localhost:8000", headers=ORIGIN)


async def register(
    client: httpx.AsyncClient, email: str, password: str = "password123"
) -> dict[str, str]:
    resp = await client.post(
        "/api/v1/auth/register",
        json={"email": email, "password": password, "full_name": "Test User"},
    )
    assert resp.status_code == 201, resp.text
    return dict(resp.json())


def make_pdf(pages: list[str], title: str = "Test") -> bytes:
    pdf = FPDF()
    pdf.set_title(title)
    for body in pages:
        pdf.add_page()
        pdf.set_font("Helvetica", "", 12)
        if body:
            pdf.multi_cell(0, 7, body)
    return bytes(pdf.output())


def encrypted_pdf() -> bytes:
    from pypdf import PdfReader, PdfWriter

    writer = PdfWriter()
    for page in PdfReader(BytesIO(make_pdf(["secret text here"]))).pages:
        writer.add_page(page)
    writer.encrypt("pw")
    buffer = BytesIO()
    writer.write(buffer)
    return buffer.getvalue()


async def upload(client: httpx.AsyncClient, data: bytes, name: str = "doc.pdf") -> httpx.Response:
    return await client.post("/api/v1/documents", files={"file": (name, data, "application/pdf")})


@pytest.fixture
def pdf_factory() -> Callable[[list[str]], bytes]:
    return make_pdf


@pytest.fixture
def unique_email() -> Iterator[Callable[[], str]]:
    yield lambda: f"user-{uuid.uuid4().hex[:8]}@example.com"


async def create_user(db: Database, email: str, role: str = "ANALYST") -> User:
    async with db.session_factory() as session:
        user = User(email=email, full_name="Direct User", password_hash="!unusable", role=role)
        session.add(user)
        await session.commit()
        return user
