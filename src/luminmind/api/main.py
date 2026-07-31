"""FastAPI uygulaması (PLAN.md Faz 6).

Tüm endpoint'ler `/api/v1` altındadır; OpenAPI şeması `/docs` ve
`/openapi.json`'dan dashboard ekibiyle paylaşılır. Zaman serileri UTC döner,
`?tz=Europe/Istanbul` ile çevrilebilir (onaylanan karar).
"""

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Annotated

from fastapi import Depends, FastAPI, Request
from fastapi.responses import RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine
from starlette.middleware.trustedhost import TrustedHostMiddleware

from luminmind.api.deps import TimeseriesSource, get_session
from luminmind.api.routers import anomalies, auth, market, plants, timeseries
from luminmind.api.schemas import HealthOut
from luminmind.config import Settings, get_settings
from luminmind.core.db import create_session_factory
from luminmind.core.hardening import (
    LoginRateLimiter,
    SecurityHeadersMiddleware,
    enforce_production_settings,
)
from luminmind.core.influx import InfluxStore
from luminmind.web.prospect_routes import router as prospect_web_router
from luminmind.web.routes import RequiresLogin
from luminmind.web.routes import router as web_router
from luminmind.web.theme import STATIC_DIR


def create_app(
    settings: Settings | None = None,
    engine: AsyncEngine | None = None,
    influx: TimeseriesSource | None = None,
) -> FastAPI:
    """App factory; test için engine/influx enjekte edilebilir."""
    settings = settings or get_settings()
    provided_engine = engine
    provided_influx = influx

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        own_engine = provided_engine is None
        own_influx = provided_influx is None and bool(settings.influx_url)
        if provided_engine is not None:
            app.state.engine = provided_engine
        else:
            from luminmind.core.db import create_engine

            app.state.engine = create_engine(settings.postgres_dsn)
        app.state.session_factory = create_session_factory(app.state.engine)
        if provided_influx is not None:
            app.state.influx = provided_influx
        elif own_influx:
            app.state.influx = InfluxStore(
                url=settings.influx_url, org=settings.influx_org, token=settings.influx_token
            )
        else:
            app.state.influx = None
        try:
            yield
        finally:
            if own_influx and app.state.influx is not None:
                await app.state.influx.__aexit__(None, None, None)
            if own_engine:
                await app.state.engine.dispose()

    # Güvensiz sırlarla üretimde açılmayı engelle (dev/test modunda uyarı yok)
    enforce_production_settings(settings)

    app = FastAPI(
        title="LuminMind API",
        version="0.1.0",
        description="GES izleme, dijital ikiz ve BESS/arbitraj platformu REST API'si",
        lifespan=lifespan,
        root_path="",
    )
    app.state.settings = settings
    app.state.login_limiter = LoginRateLimiter(
        max_attempts=settings.lm_login_max_attempts,
        window_s=settings.lm_login_window_min * 60.0,
    )

    # HSTS yalnızca genel adres tanımlıyken anlamlı; yerel HTTP'de tarayıcıyı
    # kalıcı olarak https'e kilitleyip geliştirmeyi bozar.
    app.add_middleware(SecurityHeadersMiddleware, hsts=settings.is_public)
    if settings.allowed_hosts:
        app.add_middleware(TrustedHostMiddleware, allowed_hosts=settings.allowed_hosts)

    prefix = "/api/v1"
    app.include_router(auth.router, prefix=prefix)
    app.include_router(plants.router, prefix=prefix)
    app.include_router(timeseries.router, prefix=prefix)
    app.include_router(anomalies.router, prefix=prefix)
    app.include_router(market.router, prefix=prefix)
    app.include_router(web_router)
    # Fizibilite akışı ayrı router'da (routes.py 2500 satırı aştı); oturum ve
    # şablon altyapısını web_router'dan devralıyor.
    app.include_router(prospect_web_router)

    # Arayüz stilleri. Yol paketin içinden türetilir; `pyproject.toml`
    # içindeki `package-data` girdisi olmadan tekerlek/imaj bu dizini taşımaz
    # ve sayfa stilsiz açılır.
    app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")

    @app.exception_handler(RequiresLogin)
    async def _redirect_to_login(request: Request, exc: RequiresLogin) -> Response:
        return RedirectResponse("/ui/login", status_code=303)

    @app.get("/", include_in_schema=False)
    async def root() -> Response:
        return RedirectResponse("/ui")

    @app.get(f"{prefix}/health", response_model=HealthOut, tags=["health"])
    async def health(session: Annotated[object, Depends(get_session)]) -> HealthOut:
        postgres_ok = True
        try:
            await session.execute(text("SELECT 1"))  # type: ignore[attr-defined]
        except Exception:
            postgres_ok = False
        return HealthOut(
            status="ok" if postgres_ok else "degraded",
            postgres=postgres_ok,
            influx_configured=app.state.influx is not None,
        )

    return app


app = create_app()
