"""Alembic environment. Migrations are a release step; they never run inside the app lifespan."""

import asyncio

from sqlalchemy.engine import Connection

from alembic import context
from app.config import get_settings
from app.db import make_engine, normalize_url


def run_migrations_offline() -> None:
    url, _ = normalize_url(get_settings().database_url)
    context.configure(url=url, literal_binds=True, dialect_opts={"paramstyle": "named"})
    with context.begin_transaction():
        context.run_migrations()


def _do_run(connection: Connection) -> None:
    context.configure(connection=connection)
    with context.begin_transaction():
        context.run_migrations()


async def run_migrations_online() -> None:
    engine = make_engine(get_settings().database_url, pool_size=1, null_pool=True)
    try:
        async with engine.connect() as connection:
            await connection.run_sync(_do_run)
    finally:
        await engine.dispose()


if context.is_offline_mode():
    run_migrations_offline()
else:
    asyncio.run(run_migrations_online())
