"""Two async engines: the app role (public.*) and the read-only role (analytics.* only)."""

from dataclasses import dataclass

from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.pool import NullPool

from app.config import Settings

_SSL_MODES = {"require", "verify-ca", "verify-full"}


def normalize_url(raw: str) -> tuple[str, bool]:
    """Return (postgresql+asyncpg URL without sslmode, ssl_required)."""
    url = make_url(raw).set(drivername="postgresql+asyncpg")
    query = dict(url.query)
    sslmode = query.pop("sslmode", None)
    ssl_param = query.pop("ssl", None)
    query.pop("channel_binding", None)
    ssl_on = (isinstance(sslmode, str) and sslmode in _SSL_MODES) or ssl_param in (
        "true",
        "require",
    )
    return url.set(query=query).render_as_string(hide_password=False), ssl_on


def make_engine(raw_url: str, *, pool_size: int, null_pool: bool) -> AsyncEngine:
    url, ssl_on = normalize_url(raw_url)
    connect_args: dict[str, object] = {"statement_cache_size": 0}
    if ssl_on:
        connect_args["ssl"] = "require"
    if null_pool:
        engine = create_async_engine(
            url, poolclass=NullPool, pool_pre_ping=True, connect_args=connect_args
        )
    else:
        engine = create_async_engine(
            url, pool_size=pool_size, max_overflow=2, pool_pre_ping=True, connect_args=connect_args
        )
    return engine


@dataclass
class Database:
    primary: AsyncEngine
    readonly: AsyncEngine
    session_factory: async_sessionmaker[AsyncSession]

    @classmethod
    def create(cls, settings: Settings) -> "Database":
        primary = make_engine(settings.database_url, pool_size=5, null_pool=settings.use_null_pool)
        readonly = make_engine(
            settings.analytics_readonly_database_url,
            pool_size=3,
            null_pool=settings.use_null_pool,
        )
        factory = async_sessionmaker(primary, expire_on_commit=False)
        return cls(primary=primary, readonly=readonly, session_factory=factory)

    async def dispose(self) -> None:
        await self.primary.dispose()
        await self.readonly.dispose()
