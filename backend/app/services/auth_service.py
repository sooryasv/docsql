"""Registration, login and demo login. Services own commit()."""

import uuid

import structlog
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import Settings
from app.errors import EmailTakenError, InvalidCredentialsError, NotFoundError, UnauthenticatedError
from app.models import User
from app.schemas import DemoLoginRequest, LoginRequest, RegisterRequest
from app.security import DUMMY_HASH, UNUSABLE_HASH, hash_password, verify_password

log = structlog.get_logger()
DEMO_EMAILS = {"ANALYST": "demo.analyst@docsql.dev", "ADMIN": "demo.admin@docsql.dev"}
DEMO_NAMES = {"ANALYST": "Demo Analyst", "ADMIN": "Demo Admin"}


async def register(session: AsyncSession, payload: RegisterRequest) -> User:
    """Creates an ANALYST only; the role is never read from the request."""
    password_hash = await hash_password(payload.password)
    user = User(
        email=payload.email,
        full_name=payload.full_name.strip(),
        password_hash=password_hash,
        role="ANALYST",
    )
    session.add(user)
    try:
        await session.commit()
    except IntegrityError as exc:
        await session.rollback()
        raise EmailTakenError() from exc
    return user


async def login(session: AsyncSession, payload: LoginRequest) -> User:
    user = (
        await session.execute(select(User).where(User.email == payload.email))
    ).scalar_one_or_none()
    # Unknown email: still burn one argon2 verification so timing does not enumerate accounts.
    stored = user.password_hash if user is not None else DUMMY_HASH
    valid = await verify_password(payload.password, stored)
    if user is None or not valid:
        raise InvalidCredentialsError()
    return user


async def demo_login(session: AsyncSession, payload: DemoLoginRequest, settings: Settings) -> User:
    if not settings.enable_demo_tools:
        raise NotFoundError()
    email = DEMO_EMAILS[payload.role]
    user = (await session.execute(select(User).where(User.email == email))).scalar_one_or_none()
    if user is None:
        raise NotFoundError("Demo users are not seeded")
    return user


async def ensure_demo_users(session: AsyncSession) -> None:
    """Idempotent: demo accounts have unusable password hashes (cookie-by-email only)."""
    for role, email in DEMO_EMAILS.items():
        exists = (await session.execute(select(User.id).where(User.email == email))).first()
        if exists is None:
            session.add(
                User(
                    email=email, full_name=DEMO_NAMES[role], password_hash=UNUSABLE_HASH, role=role
                )
            )
    await session.commit()


async def get_user(session: AsyncSession, user_id: str) -> User:
    try:
        parsed = uuid.UUID(user_id)
    except ValueError as exc:
        raise UnauthenticatedError("Invalid session") from exc
    user = await session.get(User, parsed)
    if user is None:
        raise UnauthenticatedError("Account no longer exists")
    return user
