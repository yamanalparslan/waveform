"""FastAPI dependency'leri: DB oturumu, auth, Influx erişimi, tz çevrimi."""

from collections.abc import AsyncIterator
from datetime import datetime
from typing import Annotated, Protocol
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from fastapi import Depends, HTTPException, Query, Request, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from luminmind.config import Settings
from luminmind.core.aggregate import RawSample
from luminmind.core.models import User
from luminmind.core.security import TokenError, decode_jwt

_bearer = HTTPBearer(auto_error=False)


class TimeseriesSource(Protocol):
    """API'nin zaman serisi kaynağı (InfluxStore veya test fake'i)."""

    async def query_plant_series(
        self, vendor_plant_id: str, metric: str, start: datetime, stop: datetime,
        resolution: str = "15m",
    ) -> list[tuple[datetime, float]]: ...

    async def query_device_series(
        self, vendor_plant_id: str, vendor_device_id: str,
        metric: str, start: datetime, stop: datetime,
    ) -> list[tuple[datetime, float]]: ...

    async def query_raw_window(self, start: datetime, stop: datetime) -> list[RawSample]: ...

    async def query_twin_window(
        self, start: datetime, stop: datetime
    ) -> dict[str, dict[datetime, float]]: ...


def get_settings_dep(request: Request) -> Settings:
    settings: Settings = request.app.state.settings
    return settings


async def get_session(request: Request) -> AsyncIterator[AsyncSession]:
    factory = request.app.state.session_factory
    async with factory() as session:
        try:
            yield session
            await session.commit()
        except BaseException:
            await session.rollback()
            raise


def get_influx(request: Request) -> TimeseriesSource:
    source: TimeseriesSource | None = request.app.state.influx
    if source is None:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="time-series store is not configured (INFLUX_URL empty)",
        )
    return source


async def get_current_user(
    request: Request,
    credentials: Annotated[HTTPAuthorizationCredentials | None, Depends(_bearer)],
    session: Annotated[AsyncSession, Depends(get_session)],
) -> User:
    if credentials is None:
        raise HTTPException(status_code=401, detail="missing bearer token")
    settings: Settings = request.app.state.settings
    try:
        claims = decode_jwt(credentials.credentials, settings.jwt_secret, expected_type="access")
    except TokenError as exc:
        raise HTTPException(status_code=401, detail=str(exc)) from exc
    user = (
        await session.scalars(select(User).where(User.email == claims.get("sub")))
    ).one_or_none()
    if user is None:
        raise HTTPException(status_code=401, detail="unknown user")
    return user


async def require_admin(user: Annotated[User, Depends(get_current_user)]) -> User:
    if user.role != "admin":
        raise HTTPException(status_code=403, detail="admin role required")
    return user


def get_tz(
    tz: Annotated[str | None, Query(description="Yanıt zaman dilimi, ör. Europe/Istanbul")] = None,
) -> ZoneInfo | None:
    """Zaman serileri UTC saklanır; istenirse yanıt bu dilime çevrilir."""
    if tz is None:
        return None
    try:
        return ZoneInfo(tz)
    except ZoneInfoNotFoundError as exc:
        raise HTTPException(status_code=422, detail=f"unknown timezone: {tz}") from exc


def in_tz(ts: datetime, zone: ZoneInfo | None) -> datetime:
    return ts if zone is None else ts.astimezone(zone)
