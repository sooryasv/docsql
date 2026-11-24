"""FastAPI dependencies: settings, DB handles, provider, session, current user."""

from collections.abc import AsyncIterator
from typing import Annotated

import structlog
from fastapi import Cookie, Depends, Request
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import Settings
from app.db import Database
from app.errors import ForbiddenError, UnauthenticatedError
from app.models import User
from app.security import COOKIE_NAME, decode_token
from app.services import auth_service
from app.services.llm_provider import LLMProvider


def get_settings_dep(request: Request) -> Settings:
    settings: Settings = request.app.state.settings
    return settings


def get_db(request: Request) -> Database:
    db: Database = request.app.state.db
    return db


def get_provider(request: Request) -> LLMProvider:
    provider: LLMProvider = request.app.state.provider
    return provider


async def get_session(db: Annotated[Database, Depends(get_db)]) -> AsyncIterator[AsyncSession]:
    async with db.session_factory() as session:
        yield session


async def get_current_user(
    session: Annotated[AsyncSession, Depends(get_session)],
    settings: Annotated[Settings, Depends(get_settings_dep)],
    token: Annotated[str | None, Cookie(alias=COOKIE_NAME)] = None,
) -> User:
    if not token:
        raise UnauthenticatedError()
    claims = decode_token(token, settings)
    user = await auth_service.get_user(session, claims["sub"])
    structlog.contextvars.bind_contextvars(user_id=str(user.id))
    return user


async def require_admin(user: Annotated[User, Depends(get_current_user)]) -> User:
    if user.role != "ADMIN":
        raise ForbiddenError("Admin role required")
    return user
