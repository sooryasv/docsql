"""Auth endpoints. Thin handlers: validate, call a service, set/clear the cookie."""

from typing import Annotated

from fastapi import APIRouter, Depends, Response
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import get_current_user, get_db, get_session, get_settings_dep
from app.config import Settings
from app.db import Database
from app.models import User
from app.schemas import DemoLoginRequest, LoginRequest, MeOut, RegisterRequest, UserOut
from app.security import clear_auth_cookie, create_token, set_auth_cookie
from app.services import auth_service, usage_service

router = APIRouter(prefix="/auth", tags=["auth"])
Session = Annotated[AsyncSession, Depends(get_session)]
Cfg = Annotated[Settings, Depends(get_settings_dep)]


def _issue(response: Response, user: User, settings: Settings) -> UserOut:
    set_auth_cookie(response, create_token(str(user.id), user.role, settings), settings)
    return UserOut.model_validate(user)


@router.post("/register", status_code=201)
async def register(
    payload: RegisterRequest, response: Response, session: Session, cfg: Cfg
) -> UserOut:
    return _issue(response, await auth_service.register(session, payload), cfg)


@router.post("/login")
async def login(payload: LoginRequest, response: Response, session: Session, cfg: Cfg) -> UserOut:
    return _issue(response, await auth_service.login(session, payload), cfg)


@router.post("/demo-login")
async def demo_login(
    payload: DemoLoginRequest, response: Response, session: Session, cfg: Cfg
) -> UserOut:
    return _issue(response, await auth_service.demo_login(session, payload, cfg), cfg)


@router.post("/logout", status_code=204)
async def logout(
    response: Response, cfg: Cfg, _user: Annotated[User, Depends(get_current_user)]
) -> None:
    clear_auth_cookie(response, cfg)


@router.get("/me")
async def me(
    user: Annotated[User, Depends(get_current_user)],
    db: Annotated[Database, Depends(get_db)],
    cfg: Cfg,
) -> MeOut:
    remaining = await usage_service.remaining_quota(db.session_factory, user.id, cfg)
    limit = None if cfg.mock_llm else cfg.daily_query_limit
    return MeOut(
        user=UserOut.model_validate(user),
        quota_limit=limit,
        quota_remaining=remaining,
        demo_tools_enabled=cfg.enable_demo_tools,
    )
