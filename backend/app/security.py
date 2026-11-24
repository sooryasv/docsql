"""Password hashing, JWT cookie helpers and the CSRF Origin/Referer guard."""

import asyncio
from datetime import UTC, datetime, timedelta
from urllib.parse import urlsplit

import jwt
from fastapi import Response
from pwdlib import PasswordHash
from pwdlib.exceptions import UnknownHashError
from starlette.responses import JSONResponse
from starlette.types import ASGIApp, Receive, Scope, Send

from app.config import Settings
from app.errors import CsrfOriginRejectedError, UnauthenticatedError, error_body

COOKIE_NAME = "docsql_session"
UNSAFE_METHODS = {"POST", "PUT", "PATCH", "DELETE"}
UNUSABLE_HASH = "!unusable"  # demo users: not a valid hash, so password login can never succeed

_hasher = PasswordHash.recommended()
# Fixed dummy hash: login on an unknown email verifies against this for timing parity.
DUMMY_HASH = _hasher.hash("docsql-dummy-password-for-timing-parity")


async def hash_password(password: str) -> str:
    return await asyncio.to_thread(_hasher.hash, password)


async def verify_password(password: str, password_hash: str) -> bool:
    def _verify() -> bool:
        try:
            return _hasher.verify(password, password_hash)
        except UnknownHashError:
            return False

    return await asyncio.to_thread(_verify)


def create_token(user_id: str, role: str, settings: Settings) -> str:
    expires = datetime.now(UTC) + timedelta(minutes=settings.jwt_expire_minutes)
    payload = {"sub": user_id, "role": role, "exp": expires}
    return jwt.encode(payload, settings.jwt_secret, algorithm="HS256")


def decode_token(token: str, settings: Settings) -> dict[str, str]:
    try:
        claims = jwt.decode(token, settings.jwt_secret, algorithms=["HS256"])
    except jwt.PyJWTError as exc:
        raise UnauthenticatedError("Invalid or expired session") from exc
    sub, role = claims.get("sub"), claims.get("role")
    if not isinstance(sub, str) or not isinstance(role, str):
        raise UnauthenticatedError("Invalid session")
    return {"sub": sub, "role": role}


def set_auth_cookie(response: Response, token: str, settings: Settings) -> None:
    response.set_cookie(
        COOKIE_NAME,
        token,
        max_age=settings.jwt_expire_minutes * 60,
        httponly=True,
        samesite="lax",
        secure=settings.cookie_secure,
        path="/",
    )


def clear_auth_cookie(response: Response, settings: Settings) -> None:
    response.delete_cookie(
        COOKIE_NAME, path="/", httponly=True, samesite="lax", secure=settings.cookie_secure
    )


def _origin_of(value: str) -> str | None:
    parts = urlsplit(value)
    if not parts.scheme or not parts.netloc:
        return None
    return f"{parts.scheme}://{parts.netloc}".lower()


class CSRFMiddleware:
    """Pure-ASGI so it never interferes with streaming responses or disconnect detection."""

    def __init__(self, app: ASGIApp, allowed_origins: list[str]) -> None:
        self.app = app
        self.allowed = {o.lower() for o in allowed_origins}

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http" or scope["method"] not in UNSAFE_METHODS:
            await self.app(scope, receive, send)
            return
        headers = {k.decode("latin-1").lower(): v.decode("latin-1") for k, v in scope["headers"]}
        source = headers.get("origin") or headers.get("referer")
        origin = _origin_of(source) if source else None
        if origin is not None and origin in self.allowed:
            await self.app(scope, receive, send)
            return
        exc = CsrfOriginRejectedError()
        request_id = str(scope.get("state", {}).get("request_id", "req_unknown"))
        body = error_body(exc.code, exc.message, request_id)
        await JSONResponse(body, status_code=exc.status_code)(scope, receive, send)
