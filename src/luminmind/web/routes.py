"""Sunucu taraflı Türkçe arayüz (Jinja2, /ui altında).

Oturum: login formu JWT üretir ve HttpOnly çerezde taşır; sayfa bağımlılığı
çerezi doğrular, geçersizse /ui/login'e yönlendirir. Grafikler sunucuda SVG
olarak üretilir (charts.py) — tarayıcıda JS gerekmez. Saatler TRT gösterilir.
"""

import json
import uuid
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Annotated
from zoneinfo import ZoneInfo

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response
from fastapi.templating import Jinja2Templates
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from luminmind.analytics.comparison import plant_actual_from_samples
from luminmind.api.deps import get_session
from luminmind.config import Settings
from luminmind.core.models import AnomalyEvent, ArbitragePlan, BatterySystem, Plant, User
from luminmind.core.security import TokenError, create_jwt, decode_jwt, verify_password
from luminmind.web.charts import Series, line_chart, price_plan_chart

router = APIRouter(prefix="/ui", tags=["web"])
templates = Jinja2Templates(directory=Path(__file__).parent / "templates")

TRT = ZoneInfo("Europe/Istanbul")
_COOKIE = "lm_session"

KIND_LABELS = {"microcrack": "Mikro çatlak", "shading": "Gölgelenme", "soiling": "Kirlilik"}
SEVERITY_LABELS = {"warning": "Uyarı", "critical": "Kritik"}
STATUS_LABELS = {"open": "Açık", "acked": "Onaylandı", "resolved": "Çözüldü"}
ACTION_LABELS = {"charge": "Şarj", "discharge": "Deşarj", "idle": "Beklemede"}


class RequiresLogin(Exception):
    """Sayfa oturum ister; handler /ui/login'e yönlendirir (main.py'de kayıtlı)."""


async def get_web_user(
    request: Request, session: Annotated[AsyncSession, Depends(get_session)]
) -> User:
    token = request.cookies.get(_COOKIE)
    if not token:
        raise RequiresLogin
    settings: Settings = request.app.state.settings
    try:
        claims = decode_jwt(token, settings.jwt_secret, expected_type="access")
    except TokenError as exc:
        raise RequiresLogin from exc
    user = (
        await session.scalars(select(User).where(User.email == claims.get("sub")))
    ).one_or_none()
    if user is None:
        raise RequiresLogin
    return user


def _trt_day_window(day: date) -> tuple[datetime, datetime]:
    start = datetime(day.year, day.month, day.day, tzinfo=TRT).astimezone(UTC)
    return start, start + timedelta(days=1)


def _parse_day(value: str | None, default: date) -> date:
    if not value:
        return default
    try:
        return date.fromisoformat(value)
    except ValueError:
        return default


@router.get("/login", response_class=HTMLResponse)
async def login_page(request: Request) -> HTMLResponse:
    return templates.TemplateResponse(request, "login.html", {"user": None, "error": None})


@router.post("/login")
async def login_submit(
    request: Request,
    session: Annotated[AsyncSession, Depends(get_session)],
    email: Annotated[str, Form()],
    password: Annotated[str, Form()],
) -> Response:
    settings: Settings = request.app.state.settings
    user = (await session.scalars(select(User).where(User.email == email))).one_or_none()
    if user is None or not verify_password(password, user.hashed_password):
        return templates.TemplateResponse(
            request,
            "login.html",
            {"user": None, "error": "E-posta veya şifre hatalı."},
            status_code=401,
        )
    token = create_jwt(
        {"sub": user.email, "role": user.role},
        settings.jwt_secret,
        ttl_s=settings.jwt_access_ttl_min * 60,
        token_type="access",
    )
    response = RedirectResponse("/ui", status_code=303)
    response.set_cookie(
        _COOKIE,
        token,
        httponly=True,
        samesite="lax",
        max_age=settings.jwt_access_ttl_min * 60,
    )
    return response


@router.post("/logout")
async def logout() -> Response:
    response = RedirectResponse("/ui/login", status_code=303)
    response.delete_cookie(_COOKIE)
    return response


@router.get("", response_class=HTMLResponse)
async def overview(
    request: Request,
    session: Annotated[AsyncSession, Depends(get_session)],
    user: Annotated[User, Depends(get_web_user)],
) -> HTMLResponse:
    influx = request.app.state.influx
    now = datetime.now(tz=UTC)
    start, stop = _trt_day_window(now.astimezone(TRT).date())

    plants = (await session.scalars(select(Plant).order_by(Plant.name))).all()
    count_rows = (
        await session.execute(
            select(AnomalyEvent.plant_id, func.count())
            .where(AnomalyEvent.status == "open")
            .group_by(AnomalyEvent.plant_id)
        )
    ).all()
    open_counts: dict[uuid.UUID, int] = {row[0]: row[1] for row in count_rows}

    cards = []
    for plant in plants:
        last_power, today_energy = "—", "—"
        if influx is not None:
            series = await influx.query_plant_series(
                plant.vendor_plant_id, "ac_power_kw", start, min(stop, now), "15m"
            )
            if series:
                last_power = f"{series[-1][1]:,.0f} kW"
                today_energy = f"{sum(v for _, v in series) * 0.25:,.0f} kWh"
        cards.append(
            {
                "plant": plant,
                "last_power": last_power,
                "today_energy": today_energy,
                "open_anomalies": open_counts.get(plant.id, 0),
            }
        )
    return templates.TemplateResponse(
        request,
        "overview.html",
        {
            "user": user,
            "section": "overview",
            "plant": plants[0] if plants else None,
            "plants": cards,
            "influx_ok": influx is not None,
            "today_label": now.astimezone(TRT).strftime("%d.%m.%Y"),
        },
    )


@router.get("/harita", response_class=HTMLResponse)
async def map_page(
    request: Request,
    session: Annotated[AsyncSession, Depends(get_session)],
    user: Annotated[User, Depends(get_web_user)],
) -> HTMLResponse:
    """Koordinatı tanımlı tüm sahaları Leaflet + OpenStreetMap üzerinde gösterir."""
    plants = (await session.scalars(select(Plant).order_by(Plant.name))).all()
    sites = [
        {
            "name": p.name,
            "lat": p.latitude,
            "lon": p.longitude,
            "capacity": p.dc_capacity_kwp,
        }
        for p in plants
        if p.latitude is not None and p.longitude is not None
    ]
    return templates.TemplateResponse(
        request,
        "map.html",
        {
            "user": user,
            "section": "map",
            "plant": plants[0] if plants else None,
            "sites": sites,
            "sites_json": json.dumps(sites),
        },
    )


async def _load_plant(session: AsyncSession, plant_id: uuid.UUID) -> Plant:
    plant = await session.get(Plant, plant_id)
    if plant is None:
        raise RequiresLogin  # bilinmeyen tesis → ana sayfaya dönüş yerine login akışı
    return plant


@router.get("/plants/{plant_id}", response_class=HTMLResponse)
async def plant_detail(
    request: Request,
    plant_id: uuid.UUID,
    session: Annotated[AsyncSession, Depends(get_session)],
    user: Annotated[User, Depends(get_web_user)],
    date_param: Annotated[str | None, None] = None,
) -> HTMLResponse:
    plant = await _load_plant(session, plant_id)
    day = _parse_day(request.query_params.get("date"), datetime.now(tz=TRT).date())
    start, stop = _trt_day_window(day)

    series: list[Series] = []
    influx = request.app.state.influx
    if influx is not None:
        actual = plant_actual_from_samples(await influx.query_raw_window(start, stop)).get(
            plant.vendor_plant_id, {}
        )
        expected = (await influx.query_twin_window(start, stop)).get(plant.vendor_plant_id, {})
        if actual:
            series.append(Series("Gerçek", "#3fa9f5", sorted(actual.items())))
        if expected:
            series.append(Series("Beklenen", "#e3b341", sorted(expected.items())))

    return templates.TemplateResponse(
        request,
        "plant_detail.html",
        {
            "user": user,
            "section": "plant",
            "plant": plant,
            "day_str": day.isoformat(),
            "production_chart": line_chart(series, TRT, unit="kW"),
            "inverters": await plant.awaitable_attrs.inverters,
            "batteries": await plant.awaitable_attrs.batteries,
        },
    )


@router.get("/plants/{plant_id}/anomalies", response_class=HTMLResponse)
async def anomalies_page(
    request: Request,
    plant_id: uuid.UUID,
    session: Annotated[AsyncSession, Depends(get_session)],
    user: Annotated[User, Depends(get_web_user)],
) -> HTMLResponse:
    plant = await _load_plant(session, plant_id)
    events = (
        await session.scalars(
            select(AnomalyEvent)
            .where(AnomalyEvent.plant_id == plant.id)
            .order_by(AnomalyEvent.started_at.desc())
        )
    ).all()
    rows = [
        {
            "id": e.id,
            "kind": e.kind,
            "severity": e.severity,
            "deviation_pct": e.deviation_pct,
            "status": e.status,
            "started_local": e.started_at.astimezone(TRT).strftime("%d.%m.%Y %H:%M"),
        }
        for e in events
    ]
    return templates.TemplateResponse(
        request,
        "anomalies.html",
        {
            "user": user,
            "section": "anomalies",
            "plant": plant,
            "events": rows,
            "kind_labels": KIND_LABELS,
            "severity_labels": SEVERITY_LABELS,
            "status_labels": STATUS_LABELS,
            "back_url": f"/ui/plants/{plant.id}/anomalies",
        },
    )


@router.post("/anomalies/{anomaly_id}/status")
async def anomaly_status(
    anomaly_id: uuid.UUID,
    session: Annotated[AsyncSession, Depends(get_session)],
    user: Annotated[User, Depends(get_web_user)],
    status: Annotated[str, Form()],
    back: Annotated[str, Form()] = "/ui",
) -> Response:
    if status in {"open", "acked", "resolved"}:
        event = await session.get(AnomalyEvent, anomaly_id)
        if event is not None:
            event.status = status
    if not back.startswith("/ui"):  # open-redirect koruması
        back = "/ui"
    return RedirectResponse(back, status_code=303)


@router.get("/plants/{plant_id}/arbitrage", response_class=HTMLResponse)
async def arbitrage_page(
    request: Request,
    plant_id: uuid.UUID,
    session: Annotated[AsyncSession, Depends(get_session)],
    user: Annotated[User, Depends(get_web_user)],
) -> HTMLResponse:
    plant = await _load_plant(session, plant_id)
    day = _parse_day(request.query_params.get("date"), datetime.now(tz=TRT).date())

    battery_ids = (
        await session.scalars(select(BatterySystem.id).where(BatterySystem.plant_id == plant.id))
    ).all()
    plan = (
        await session.scalars(
            select(ArbitragePlan).where(
                ArbitragePlan.battery_id.in_(battery_ids), ArbitragePlan.plan_date == day
            )
        )
    ).first()

    prices: list[tuple[datetime, float]] = []
    actions: dict[datetime, tuple[str, float]] = {}
    slot_rows = []
    if plan is not None:
        for slot in await plan.awaitable_attrs.slots:
            ts = slot.slot_start if slot.slot_start.tzinfo else slot.slot_start.replace(tzinfo=UTC)
            prices.append((ts, slot.price_try_mwh))
            actions[ts] = (slot.action, slot.power_kw)
            slot_rows.append(
                {
                    "local_time": ts.astimezone(TRT).strftime("%H:%M"),
                    "action": slot.action,
                    "power_kw": slot.power_kw,
                    "price_try_mwh": slot.price_try_mwh,
                }
            )
    return templates.TemplateResponse(
        request,
        "arbitrage.html",
        {
            "user": user,
            "section": "arbitrage",
            "plant": plant,
            "day_str": day.isoformat(),
            "plan": plan,
            "slots": slot_rows,
            "chart": price_plan_chart(prices, actions, TRT),
            "action_labels": ACTION_LABELS,
        },
    )
