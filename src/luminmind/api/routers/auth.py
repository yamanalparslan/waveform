from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Request
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from luminmind.api.deps import get_current_user, get_session
from luminmind.api.schemas import (
    AccessToken,
    LoginRequest,
    RefreshRequest,
    TokenPair,
    UserOut,
)
from luminmind.config import Settings
from luminmind.core.models import User
from luminmind.core.security import TokenError, create_jwt, decode_jwt, verify_password

router = APIRouter(prefix="/auth", tags=["auth"])


def _issue_access(settings: Settings, email: str, role: str) -> str:
    return create_jwt(
        {"sub": email, "role": role},
        settings.jwt_secret,
        ttl_s=settings.jwt_access_ttl_min * 60,
        token_type="access",
    )


@router.post("/login", response_model=TokenPair)
async def login(
    payload: LoginRequest,
    request: Request,
    session: Annotated[AsyncSession, Depends(get_session)],
) -> TokenPair:
    settings: Settings = request.app.state.settings
    user = (
        await session.scalars(select(User).where(User.email == payload.email))
    ).one_or_none()
    if user is None or not verify_password(payload.password, user.hashed_password):
        raise HTTPException(status_code=401, detail="invalid credentials")
    return TokenPair(
        access_token=_issue_access(settings, user.email, user.role),
        refresh_token=create_jwt(
            {"sub": user.email},
            settings.jwt_secret,
            ttl_s=settings.jwt_refresh_ttl_min * 60,
            token_type="refresh",
        ),
    )


@router.post("/refresh", response_model=AccessToken)
async def refresh(
    payload: RefreshRequest,
    request: Request,
    session: Annotated[AsyncSession, Depends(get_session)],
) -> AccessToken:
    settings: Settings = request.app.state.settings
    try:
        claims = decode_jwt(payload.refresh_token, settings.jwt_secret, expected_type="refresh")
    except TokenError as exc:
        raise HTTPException(status_code=401, detail=str(exc)) from exc
    user = (
        await session.scalars(select(User).where(User.email == claims.get("sub")))
    ).one_or_none()
    if user is None:
        raise HTTPException(status_code=401, detail="unknown user")
    return AccessToken(access_token=_issue_access(settings, user.email, user.role))


@router.get("/me", response_model=UserOut)
async def me(user: Annotated[User, Depends(get_current_user)]) -> UserOut:
    return UserOut(id=user.id, email=user.email, role=user.role)
