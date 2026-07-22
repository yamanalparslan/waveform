"""Sunucu taraflı Türkçe arayüz (Jinja2, /ui altında).

Oturum: login formu JWT üretir ve HttpOnly çerezde taşır; sayfa bağımlılığı
çerezi doğrular, geçersizse /ui/login'e yönlendirir. Grafikler sunucuda SVG
olarak üretilir (charts.py). Saatler TRT gösterilir.
"""

import csv
import io
import json
import uuid
from collections.abc import Sequence
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Annotated, Any
from urllib.parse import quote
from zoneinfo import ZoneInfo

from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response, StreamingResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from luminmind.analytics.comparison import plant_actual_from_samples
from luminmind.api.deps import get_session
from luminmind.config import Settings
from luminmind.core.models import (
    AnomalyEvent,
    ArbitragePlan,
    BatterySystem,
    Inverter,
    Plant,
    PvArray,
    User,
)
from luminmind.core.security import (
    TokenError,
    create_jwt,
    decode_jwt,
    hash_password,
    verify_password,
)
from luminmind.web.charts import Series, line_chart, price_plan_chart, sparkline

router = APIRouter(prefix="/ui", tags=["web"])
templates = Jinja2Templates(directory=Path(__file__).parent / "templates")

TRT = ZoneInfo("Europe/Istanbul")
_COOKIE = "lm_session"

KIND_LABELS = {"microcrack": "Mikro çatlak", "shading": "Gölgelenme", "soiling": "Kirlilik"}
SEVERITY_LABELS = {"warning": "Uyarı", "critical": "Kritik"}
STATUS_LABELS = {"open": "Açık", "acked": "Onaylandı", "resolved": "Çözüldü"}
STATUS_CHIP = {"open": "info", "acked": "muted", "resolved": "ok"}
KIND_CHIP = {"microcrack": "crit", "shading": "warn", "soiling": "warn"}
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


async def require_admin(user: Annotated[User, Depends(get_web_user)]) -> User:
    if user.role != "admin":
        raise HTTPException(status_code=403, detail="admin only")
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


def _fmt_int(x: float | int) -> str:
    return f"{int(round(x)):,}".replace(",", ".")


def _fmt_1(x: float) -> str:
    return f"{x:,.1f}".replace(",", ".")


async def sidebar_plants_context(
    session: AsyncSession,
) -> tuple[list[dict[str, Any]], list[Plant]]:
    """Sol menüde listelenecek tesisleri ve açık anomali sayaçlarını döndürür.

    Aynı üst başlığı paylaşan tesisleri (ör. "Tescom İzmir GES · Mekanik" ve
    "Tescom İzmir GES · Uretim") tek bir grup altında toplar. Üst başlık ile
    birebir eşleşen bir tesis varsa (eski tekil kayıt) grup başlığı ona
    tıklanabilir olur; yoksa yalnızca ayraç metni olarak görünür.

    Her giriş `{id, name, open_anomalies, children, is_group}` biçimindedir.
    Alt tesisler `children` içinde aynı şemayla listelenir.
    """
    plants = (await session.scalars(select(Plant).order_by(Plant.name))).all()
    if not plants:
        return [], []
    counts_rows = (
        await session.execute(
            select(AnomalyEvent.plant_id, func.count())
            .where(
                AnomalyEvent.status == "open",
                AnomalyEvent.plant_id.in_([p.id for p in plants]),
            )
            .group_by(AnomalyEvent.plant_id)
        )
    ).all()
    counts = {row[0]: row[1] for row in counts_rows}
    plants_by_name = {p.name: p for p in plants}
    plant_items = {}
    
    for p in plants:
        plant_items[p.id] = {
            "id": p.id, 
            "name": p.name, 
            "short_name": p.name.split(" · ")[-1] if " · " in p.name else p.name,
            "open_anomalies": counts.get(p.id, 0),
            "children": []
        }
        
    top_level = []
    for p in plants:
        if " · " in p.name:
            parent_name = p.name.split(" · ")[0]
            if parent_name in plants_by_name:
                parent_id = plants_by_name[parent_name].id
                plant_items[parent_id]["children"].append(plant_items[p.id])
                continue
        top_level.append(plant_items[p.id])

    return top_level, list(plants)


def _as_utc(ts: datetime | None) -> datetime | None:
    """SQLite gibi tz-naive tutan sürücülerden dönen damgayı UTC olarak yorumlar."""
    if ts is None:
        return None
    return ts.replace(tzinfo=UTC) if ts.tzinfo is None else ts


def _time_ago_tr(ts: datetime) -> str:
    delta = datetime.now(tz=UTC) - ts
    seconds = int(delta.total_seconds())
    if seconds < 60:
        return f"{seconds} sn önce"
    if seconds < 3600:
        return f"{seconds // 60} dk önce"
    if seconds < 86400:
        return f"{seconds // 3600} saat önce"
    return f"{seconds // 86400} gün önce"


# ------------------------------ Auth ------------------------------


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


# ------------------------------ Overview ------------------------------


@router.get("", response_class=HTMLResponse)
async def overview(
    request: Request,
    session: Annotated[AsyncSession, Depends(get_session)],
    user: Annotated[User, Depends(get_web_user)],
) -> HTMLResponse:
    influx = request.app.state.influx
    settings: Settings = request.app.state.settings
    now = datetime.now(tz=UTC)
    start, stop = _trt_day_window(now.astimezone(TRT).date())

    plants = (await session.scalars(select(Plant).order_by(Plant.name))).all()
    open_counts_rows = (
        await session.execute(
            select(AnomalyEvent.plant_id, func.count())
            .where(AnomalyEvent.status == "open")
            .group_by(AnomalyEvent.plant_id)
        )
    ).all()
    open_counts: dict[uuid.UUID, int] = {row[0]: row[1] for row in open_counts_rows}

    children_by_parent_name = {}
    top_level_plants = []
    
    for p in plants:
        if " · " in p.name:
            parent_name = p.name.split(" · ")[0]
            children_by_parent_name.setdefault(parent_name, []).append(p)
        else:
            top_level_plants.append(p)

    total_power = 0.0
    total_energy = 0.0
    plants_producing = 0
    cards: list[dict[str, Any]] = []
    for plant in top_level_plants:
        last_power_val = 0.0
        today_energy_val = 0.0
        series: list[tuple[datetime, float]] = []
        children = children_by_parent_name.get(plant.name, [])
        is_parent = len(children) > 0

        if influx is not None:
            if is_parent:
                combined_series = {}
                for child in children:
                    child_series = await influx.query_plant_series(
                        child.vendor_plant_id, "ac_power_kw", start, min(stop, now), "5m"
                    )
                    for t, v in child_series:
                        combined_series[t] = combined_series.get(t, 0.0) + v
                series = sorted(combined_series.items())
            else:
                series = await influx.query_plant_series(
                    plant.vendor_plant_id, "ac_power_kw", start, min(stop, now), "5m"
                )
            if series:
                last_power_val = float(series[-1][1])
                today_energy_val = sum(v for _, v in series) * 0.25

        inverters = []
        if is_parent:
            for c in children:
                inverters.extend(await c.awaitable_attrs.inverters)
        else:
            inverters = list(await plant.awaitable_attrs.inverters)
            
        inv_power_sum = sum((getattr(inv, "last_power_kw", 0.0) or 0.0) for inv in inverters)
        inv_daily_sum = sum((getattr(inv, "last_energy_daily_kwh", 0.0) or 0.0) for inv in inverters)
        
        if inv_power_sum > 0:
            last_power_val = inv_power_sum
        if inv_daily_sum > 0:
            today_energy_val = inv_daily_sum
        total_power += last_power_val
        total_energy += today_energy_val
        if last_power_val > 0.1:
            plants_producing += 1
            
        plant_open_anomalies = open_counts.get(plant.id, 0)
        capacity = plant.dc_capacity_kwp or 0.0
        if is_parent:
            plant_open_anomalies += sum(open_counts.get(c.id, 0) for c in children)
            capacity += sum((c.dc_capacity_kwp or 0.0) for c in children)

        status_class = "ok" if last_power_val > 0.1 else "muted"
        status_label = "Üretiyor" if last_power_val > 0.1 else "Bekliyor"
        cards.append(
            {
                "plant": plant,
                "last_power": (
                    f"{_fmt_int(last_power_val)} kW" if influx is not None else "—"
                ),
                "today_energy": (
                    f"{_fmt_int(today_energy_val)} kWh" if influx is not None else "—"
                ),
                "open_anomalies": plant_open_anomalies,
                "capacity_label": (
                    f"{_fmt_1(capacity)} kWp"
                    if capacity
                    else "kapasite tanımsız"
                ),
                "status_class": status_class,
                "status_label": status_label,
                "sparkline_svg": sparkline(series, color="#f2b544"),
            }
        )

    total_capacity_mwp = sum((p.dc_capacity_kwp or 0.0) for p in plants) / 1000.0
    totals = {
        "power_kw": _fmt_int(total_power) if influx is not None else "—",
        "energy_kwh": _fmt_int(total_energy) if influx is not None else "—",
        "plants_producing": plants_producing,
        "capacity_mwp": _fmt_1(total_capacity_mwp),
        "open_anomalies": sum(open_counts.values()),
    }

    # Son 5 olay
    recent_events_rows = (
        await session.scalars(
            select(AnomalyEvent).order_by(AnomalyEvent.started_at.desc()).limit(5)
        )
    ).all()
    plant_by_id = {p.id: p for p in plants}
    recent_events = [
        {
            "kind_label": KIND_LABELS.get(e.kind, e.kind),
            "chip_class": KIND_CHIP.get(e.kind, "muted"),
            "plant_name": plant_by_id[e.plant_id].name if e.plant_id in plant_by_id else "—",
            "time_ago": _time_ago_tr(e.started_at.astimezone(UTC)),
            "summary": f"{SEVERITY_LABELS.get(e.severity, e.severity)} · "
            f"{e.deviation_pct:.1f}% sapma · {STATUS_LABELS.get(e.status, e.status)}",
        }
        for e in recent_events_rows
    ]

    sidebar_plants, _ = await sidebar_plants_context(session)
    return templates.TemplateResponse(
        request,
        "overview.html",
        {
            "user": user,
            "section": "overview",
            "plant": None,  # overview'da alt-nav açık olmasın
            "sidebar_plants": sidebar_plants,
            "plants": cards,
            "totals": totals,
            "recent_events": recent_events,
            "influx_ok": influx is not None,
            "mock_mode": settings.lm_use_mock_vendors,
            "today_label": now.astimezone(TRT).strftime("%d.%m.%Y"),
            "page_title": "Genel Bakış",
            "auto_refresh_s": 60,
        },
    )


# ------------------------------ Map ------------------------------


@router.get("/harita", response_class=HTMLResponse)
async def map_page(
    request: Request,
    session: Annotated[AsyncSession, Depends(get_session)],
    user: Annotated[User, Depends(get_web_user)],
) -> HTMLResponse:
    sidebar_plants, plants = await sidebar_plants_context(session)
    sites = [
        {"name": p.name, "lat": p.latitude, "lon": p.longitude, "capacity": p.dc_capacity_kwp}
        for p in plants
        if " · " not in p.name and p.latitude is not None and p.longitude is not None
    ]
    return templates.TemplateResponse(
        request,
        "map.html",
        {
            "user": user,
            "section": "map",
            "plant": None,
            "sidebar_plants": sidebar_plants,
            "sites": sites,
            "sites_json": json.dumps(sites),
            "page_title": "Saha Haritası",
        },
    )


# ------------------------------ Plant detail ------------------------------


async def _load_plant(session: AsyncSession, plant_id: uuid.UUID) -> Plant:
    plant = await session.get(Plant, plant_id)
    if plant is None:
        raise HTTPException(status_code=404, detail="plant not found")
    return plant


async def _open_anomaly_count(session: AsyncSession, plant_id: uuid.UUID) -> int:
    result = await session.execute(
        select(func.count()).where(
            AnomalyEvent.plant_id == plant_id, AnomalyEvent.status == "open"
        )
    )
    return int(result.scalar() or 0)


@router.get("/plants/{plant_id}", response_class=HTMLResponse)
async def plant_detail(
    request: Request,
    plant_id: uuid.UUID,
    session: Annotated[AsyncSession, Depends(get_session)],
    user: Annotated[User, Depends(get_web_user)],
) -> HTMLResponse:
    plant = await _load_plant(session, plant_id)
    children = (await session.scalars(select(Plant).where(Plant.name.startswith(f"{plant.name} · ")))).all()
    is_parent = len(children) > 0
    
    day = _parse_day(request.query_params.get("date"), datetime.now(tz=TRT).date())
    start, stop = _trt_day_window(day)

    series: list[Series] = []
    peak_kw = 0.0
    energy_kwh = 0.0
    expected_kwh = 0.0
    influx = request.app.state.influx
    if influx is not None:
        actual_dict = plant_actual_from_samples(await influx.query_raw_window(start, stop))
        expected_dict = await influx.query_twin_window(start, stop)
        actual = {}
        expected = {}
        
        if is_parent:
            child_ids = [c.vendor_plant_id for c in children]
            for cid in child_ids:
                for t, v in actual_dict.get(cid, {}).items():
                    actual[t] = actual.get(t, 0.0) + v
                for t, v in expected_dict.get(cid, {}).items():
                    expected[t] = expected.get(t, 0.0) + v
        else:
            actual = actual_dict.get(plant.vendor_plant_id, {})
            expected = expected_dict.get(plant.vendor_plant_id, {})

        if actual:
            series.append(Series("Gerçek", "#f2b544", sorted(actual.items())))
            peak_kw = max(actual.values())
            energy_kwh = sum(actual.values()) * 0.25
        if expected:
            series.append(Series("Beklenen", "#5aa1e3", sorted(expected.items())))
            expected_kwh = sum(expected.values()) * 0.25

    pr = 0.0
    ui_energy_kwh = energy_kwh

    inverters = []
    batteries = []
    if is_parent:
        for c in children:
            inverters.extend(await c.awaitable_attrs.inverters)
            batteries.extend(await c.awaitable_attrs.batteries)
    else:
        inverters = list(await plant.awaitable_attrs.inverters)
        batteries = list(await plant.awaitable_attrs.batteries)

    inverter_rows = _build_inverter_rows(inverters)
    daily_sum = sum((getattr(inv, "last_energy_daily_kwh", 0.0) or 0.0) for inv in inverters)
    
    if daily_sum > 0:
        ui_energy_kwh = daily_sum

    if expected_kwh > 1.0:
        pr = min(200.0, ui_energy_kwh / expected_kwh * 100.0)
    kpis = {
        "peak_kw": _fmt_int(peak_kw) if peak_kw else "—",
        "energy_kwh": _fmt_int(ui_energy_kwh) if ui_energy_kwh else "—",
        "energy_trend": "cihaz verisi" if daily_sum > 0 else "15 dk toplamı",
        "expected_kwh": _fmt_int(expected_kwh) if expected_kwh else "—",
        "pr": _fmt_1(pr) if pr else "—",
        "pr_ok": pr >= 85.0,
    }

    sidebar_plants, _ = await sidebar_plants_context(session)

    child_sites = []
    if is_parent:
        for c in children:
            child_sites.append({
                "id": str(c.id),
                "name": c.name.split(" · ")[-1],
                "lat": c.latitude,
                "lon": c.longitude,
                "capacity": c.dc_capacity_kwp
            })

    return templates.TemplateResponse(
        request,
        "plant_detail.html",
        {
            "user": user,
            "section": "plant",
            "plant": plant,
            "sidebar_plants": sidebar_plants,
            "plant_open_anomalies": await _open_anomaly_count(session, plant.id),
            "day_str": day.isoformat(),
            "kpis": kpis,
            "production_chart": line_chart(series, TRT, unit="kW"),
            "is_parent": is_parent,
            "child_sites_json": json.dumps(child_sites) if is_parent else "[]",
            "inverter_rows": inverter_rows,
            "batteries": batteries,
            "page_title": plant.name,
            "auto_refresh_s": 60,
        },
    )


def _build_inverter_rows(inverters: "Sequence[Any]") -> list[dict[str, Any]]:
    """İnvertör tablosu için görsel satırları hazırlar (durum/rozet/sıcaklık rengi)."""
    from luminmind.analytics.inverter_health import (
        CRITICAL_OVERHEAT_C,
        OVERHEAT_C,
        STALE_AFTER,
    )

    now = datetime.now(tz=UTC)
    rows: list[dict[str, Any]] = []
    for inv in inverters:
        last_seen_local = "—"
        health_chip = "muted"
        health_label = "Beklemede"
        temp_color = "var(--text)"

        if inv.last_seen_at is not None:
            last_seen = (
                inv.last_seen_at.replace(tzinfo=UTC)
                if inv.last_seen_at.tzinfo is None else inv.last_seen_at
            )
            age = now - last_seen
            last_seen_local = last_seen.astimezone(TRT).strftime("%d.%m %H:%M")
            if age > STALE_AFTER:
                health_chip, health_label = "crit", "Çevrimdışı"
            else:
                healthy_status = (inv.last_status or "").upper() in {
                    "AKTIF", "AKTİF", "ACTIVE", "OK", "NORMAL", "RUN", "",
                }
                bad_error = (inv.last_error_code or "0") not in {"0", "0.0", ""}
                if bad_error or not healthy_status:
                    health_chip, health_label = "crit", "Arıza"
                elif inv.last_temp_c is not None and inv.last_temp_c > OVERHEAT_C:
                    health_chip, health_label = "warn", "Aşırı sıcak"
                elif inv.last_power_kw is not None and inv.last_power_kw > 0.1:
                    health_chip, health_label = "ok", "Üretiyor"
                else:
                    health_chip, health_label = "info", "Bekliyor"

        if inv.last_temp_c is not None:
            if inv.last_temp_c > CRITICAL_OVERHEAT_C:
                temp_color = "var(--coral)"
            elif inv.last_temp_c > OVERHEAT_C:
                temp_color = "var(--amber)"

        err = inv.last_error_code
        error_or_status = "—"
        if err and err not in {"0", "0.0", ""}:
            error_or_status = f"kod {err}"
        elif inv.last_status:
            error_or_status = inv.last_status

        rows.append(
            {
                "vendor_device_id": inv.vendor_device_id,
                "plant_id": inv.plant_id,
                "health_chip": health_chip,
                "health_label": health_label,
                "power": (
                    f"{inv.last_power_kw:.1f} kW" if inv.last_power_kw is not None else "—"
                ),
                "temp": f"{inv.last_temp_c:.1f} °C" if inv.last_temp_c is not None else "—",
                "temp_color": temp_color,
                "energy_daily": f"{inv.last_energy_daily_kwh:.1f} kWh" if getattr(inv, "last_energy_daily_kwh", None) is not None else "—",
                "error_or_status": error_or_status,
                "last_seen": last_seen_local,
            }
        )
    return rows


# ------------------------------ Inverter detail ------------------------------


@router.get("/plants/{plant_id}/inverters/{device_id}", response_class=HTMLResponse)
async def inverter_detail(
    request: Request,
    plant_id: uuid.UUID,
    device_id: str,
    session: Annotated[AsyncSession, Depends(get_session)],
    user: Annotated[User, Depends(get_web_user)],
) -> HTMLResponse:
    from luminmind.analytics.inverter_health import (
        CRITICAL_OVERHEAT_C,
        KIND_INV_ERROR,
        KIND_INV_OFFLINE,
        KIND_INV_OVERHEAT,
        OVERHEAT_C,
    )

    plant = await _load_plant(session, plant_id)
    
    # Check if the plant has children (by string matching name, e.g. 'Parent · Child')
    all_plants = (await session.scalars(select(Plant))).all()
    valid_plant_ids = [plant.id]
    for p in all_plants:
        if " · " in p.name and p.name.split(" · ")[0] == plant.name:
            valid_plant_ids.append(p.id)

    inverter = (
        await session.scalars(
            select(Inverter).where(
                Inverter.plant_id.in_(valid_plant_ids), Inverter.vendor_device_id == device_id
            )
        )
    ).first()
    if inverter is None:
        raise HTTPException(status_code=404, detail="inverter not found")

    day = _parse_day(request.query_params.get("date"), datetime.now(tz=TRT).date())
    start, stop = _trt_day_window(day)

    influx = request.app.state.influx
    power_points: list[tuple[datetime, float]] = []
    temp_points: list[tuple[datetime, float]] = []
    if influx is not None:
        power_points = await influx.query_device_series(
            plant.vendor_plant_id, device_id, "ac_power_kw", start, stop
        )
        temp_points = await influx.query_device_series(
            plant.vendor_plant_id, device_id, "temp_c", start, stop
        )

    peak_power = max((v for _, v in power_points), default=0.0)
    energy_kwh = sum(v for _, v in power_points) * 0.25 if power_points else 0.0

    # Sağlık rozeti — plant_detail'daki mantıkla aynı, tek satır için
    rows = _build_inverter_rows([inverter])
    health = {
        "chip": rows[0]["health_chip"] if rows else "muted",
        "label": rows[0]["health_label"] if rows else "Beklemede",
    }

    temp_color = "var(--text)"
    if inverter.last_temp_c is not None:
        if inverter.last_temp_c > CRITICAL_OVERHEAT_C:
            temp_color = "var(--coral)"
        elif inverter.last_temp_c > OVERHEAT_C:
            temp_color = "var(--amber)"

    last_seen_local = "veri yok"
    if inverter.last_seen_at is not None:
        _ls = (
            inverter.last_seen_at.replace(tzinfo=UTC)
            if inverter.last_seen_at.tzinfo is None else inverter.last_seen_at
        )
        last_seen_local = _ls.astimezone(TRT).strftime("%d.%m %H:%M")
    kpis = {
        "last_power": (
            f"{inverter.last_power_kw:.1f}" if inverter.last_power_kw is not None else "—"
        ),
        "last_seen": last_seen_local,
        "peak_power": _fmt_int(peak_power) if peak_power else "—",
        "energy_kwh": _fmt_int(energy_kwh) if energy_kwh else "—",
        "last_temp": f"{inverter.last_temp_c:.1f}" if inverter.last_temp_c is not None else "—",
        "temp_color": temp_color,
        "temp_hot": inverter.last_temp_c is not None and inverter.last_temp_c > OVERHEAT_C,
    }

    # Cihaza dair son 10 olay (evidence.device_id eşleşen)
    all_events = (
        await session.scalars(
            select(AnomalyEvent)
            .where(
                AnomalyEvent.plant_id == plant.id,
                AnomalyEvent.kind.in_({KIND_INV_OFFLINE, KIND_INV_OVERHEAT, KIND_INV_ERROR}),
            )
            .order_by(AnomalyEvent.started_at.desc())
            .limit(50)
        )
    ).all()
    device_events = []
    for e in all_events:
        if str(e.evidence.get("device_id", "")) != device_id:
            continue
        from luminmind.analytics.inverter_health import DEVICE_KIND_LABELS
        device_events.append({
            "kind_label": DEVICE_KIND_LABELS.get(e.kind, e.kind),
            "severity_label": SEVERITY_LABELS.get(e.severity, e.severity),
            "severity_chip": "crit" if e.severity == "critical" else "warn",
            "status_label": STATUS_LABELS.get(e.status, e.status),
            "status_chip": STATUS_CHIP.get(e.status, "muted"),
            "started_local": (
                # started_at NOT NULL, _as_utc None dönmez
                _as_utc(e.started_at).astimezone(TRT).strftime("%d.%m.%Y %H:%M")  # type: ignore[union-attr]
            ),
        })
        if len(device_events) >= 10:
            break

    sidebar_plants, _ = await sidebar_plants_context(session)
    return templates.TemplateResponse(
        request,
        "inverter_detail.html",
        {
            "user": user,
            "section": "plant",
            "plant": plant,
            "sidebar_plants": sidebar_plants,
            "plant_open_anomalies": await _open_anomaly_count(session, plant.id),
            "device_id": device_id,
            "inverter": inverter,
            "day_str": day.isoformat(),
            "kpis": kpis,
            "health": health,
            "power_chart": line_chart(
                [Series("Güç", "#f2b544", power_points)], TRT, unit="kW"
            ),
            "temp_chart": line_chart(
                [Series("Sıcaklık", "#5aa1e3", temp_points)], TRT, unit="°C"
            ),
            "device_events": device_events,
            "page_title": f"{plant.name} · İnvertör {device_id}",
            "auto_refresh_s": 60,
        },
    )


# ------------------------------ Anomalies ------------------------------


@router.get("/anomalies/{anomaly_id}", response_class=HTMLResponse)
async def anomaly_detail(
    request: Request,
    anomaly_id: uuid.UUID,
    session: Annotated[AsyncSession, Depends(get_session)],
    user: Annotated[User, Depends(get_web_user)],
) -> HTMLResponse:
    from luminmind.analytics.inverter_health import DEVICE_KIND_LABELS

    event = await session.get(AnomalyEvent, anomaly_id)
    if event is None:
        raise HTTPException(status_code=404, detail="anomaly not found")
    plant = await session.get(Plant, event.plant_id)
    if plant is None:
        raise HTTPException(status_code=404, detail="plant not found")

    kind_label = KIND_LABELS.get(event.kind) or DEVICE_KIND_LABELS.get(event.kind, event.kind)
    device_id = event.evidence.get("device_id") if isinstance(event.evidence, dict) else None

    # SQLite gibi tz-naive tutan sürücülerde damgayı UTC olarak yorumla
    started_at = (
        event.started_at.replace(tzinfo=UTC) if event.started_at.tzinfo is None
        else event.started_at
    )
    ended_at = event.ended_at
    if ended_at is not None and ended_at.tzinfo is None:
        ended_at = ended_at.replace(tzinfo=UTC)
    started_local = started_at.astimezone(TRT).strftime("%d.%m.%Y %H:%M")
    ended = ended_at or datetime.now(tz=UTC)
    duration_s = int((ended - started_at).total_seconds())
    if duration_s < 60:
        duration_label = f"{duration_s} sn"
    elif duration_s < 3600:
        duration_label = f"{duration_s // 60} dk"
    elif duration_s < 86400:
        duration_label = f"{duration_s // 3600} sa {(duration_s % 3600) // 60} dk"
    else:
        duration_label = f"{duration_s // 86400} gün {(duration_s % 86400) // 3600} sa"

    # Kanıt tablosu — evidence dict'ini okunabilir sıraya çevir
    evidence_rows: list[tuple[str, str]] = []
    if isinstance(event.evidence, dict):
        friendly = {
            "device_id": "Cihaz",
            "temp_c": "Sıcaklık (°C)",
            "threshold_c": "Eşik (°C)",
            "minutes_since_last": "Son veriden bu yana (dk)",
            "error_code": "Hata kodu",
            "status": "Durum",
            "step_delta_pct": "Basamak farkı (%)",
            "before_median_pct": "Öncesi medyan (%)",
            "after_median_pct": "Sonrası medyan (%)",
            "band_hours_utc": "Bant saatleri (UTC)",
            "band_median_pct": "Bant medyan (%)",
            "recurring_days": "Tekrarlayan günler",
            "median_pct": "Medyan sapma (%)",
            "mad_pct": "MAD (%)",
        }
        for k, v in event.evidence.items():
            evidence_rows.append((friendly.get(k, k), str(v)))

    # Olay penceresi grafiği — cihaz varsa cihaz serisi, yoksa tesis toplamı
    chart_html = ""
    window_label = ""
    influx = request.app.state.influx
    if influx is not None:
        w_start = started_at - timedelta(hours=3)
        w_stop = started_at + timedelta(hours=3)
        window_label = (
            w_start.astimezone(TRT).strftime("%d.%m %H:%M") + " – "
            + w_stop.astimezone(TRT).strftime("%H:%M")
        )
        if device_id:
            points = await influx.query_device_series(
                plant.vendor_plant_id, str(device_id), "ac_power_kw", w_start, w_stop
            )
        else:
            points = await influx.query_plant_series(
                plant.vendor_plant_id, "ac_power_kw", w_start, w_stop, "5m"
            )
        chart_html = line_chart([Series("Güç", "#f2b544", points)], TRT, unit="kW")

    deviation_color = "var(--text)"
    if event.deviation_pct <= -15:
        deviation_color = "var(--coral)"
    elif event.deviation_pct <= -5:
        deviation_color = "var(--amber)"

    sidebar_plants, _ = await sidebar_plants_context(session)
    return templates.TemplateResponse(
        request,
        "anomaly_detail.html",
        {
            "user": user,
            "section": "anomalies",
            "plant": plant,
            "sidebar_plants": sidebar_plants,
            "plant_open_anomalies": await _open_anomaly_count(session, plant.id),
            "event": event,
            "kind_label": kind_label,
            "severity_label": SEVERITY_LABELS.get(event.severity, event.severity),
            "severity_chip": "crit" if event.severity == "critical" else "warn",
            "status_label": STATUS_LABELS.get(event.status, event.status),
            "status_chip": STATUS_CHIP.get(event.status, "muted"),
            "device_id": device_id,
            "started_local": started_local,
            "time_ago": _time_ago_tr(started_at),
            "duration_label": duration_label,
            "evidence_rows": evidence_rows,
            "chart": chart_html,
            "window_label": window_label,
            "deviation_color": deviation_color,
            "page_title": f"{plant.name} · {kind_label}",
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
            "started_local": (
                # started_at NOT NULL, _as_utc None dönmez
                _as_utc(e.started_at).astimezone(TRT).strftime("%d.%m.%Y %H:%M")  # type: ignore[union-attr]
            ),
        }
        for e in events
    ]
    stats = {
        "open": sum(1 for e in events if e.status == "open"),
        "acked": sum(1 for e in events if e.status == "acked"),
        "resolved": sum(1 for e in events if e.status == "resolved"),
    }
    sidebar_plants, _ = await sidebar_plants_context(session)
    return templates.TemplateResponse(
        request,
        "anomalies.html",
        {
            "user": user,
            "section": "anomalies",
            "plant": plant,
            "sidebar_plants": sidebar_plants,
            "plant_open_anomalies": stats["open"],
            "events": rows,
            "stats": stats,
            "kind_labels": KIND_LABELS,
            "severity_labels": SEVERITY_LABELS,
            "status_labels": STATUS_LABELS,
            "status_chip": STATUS_CHIP,
            "back_url": f"/ui/plants/{plant.id}/anomalies",
            "page_title": f"{plant.name} · Anomaliler",
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
    if not back.startswith("/ui"):
        back = "/ui"
    return RedirectResponse(back, status_code=303)


# ------------------------------ Arbitrage ------------------------------


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
            ts = (
                slot.slot_start if slot.slot_start.tzinfo else slot.slot_start.replace(tzinfo=UTC)
            )
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
    sidebar_plants, _ = await sidebar_plants_context(session)
    return templates.TemplateResponse(
        request,
        "arbitrage.html",
        {
            "user": user,
            "section": "arbitrage",
            "plant": plant,
            "sidebar_plants": sidebar_plants,
            "plant_open_anomalies": await _open_anomaly_count(session, plant.id),
            "day_str": day.isoformat(),
            "plan": plan,
            "slots": slot_rows,
            "chart": price_plan_chart(prices, actions, TRT),
            "action_labels": ACTION_LABELS,
            "page_title": f"{plant.name} · Arbitraj",
        },
    )


# ------------------------------ Reports ------------------------------


async def _load_daily_report(
    influx: Any,
    plants: "Sequence[Plant]",
    start: datetime,
    stop: datetime,
) -> list[dict[str, Any]]:
    """`lm_daily` bucket'ından günlük agregatları okur; boşsa lm_raw'dan türetir."""
    rows: list[dict[str, Any]] = []
    if influx is None:
        return rows
    current = start
    while current < stop:
        day_end = current + timedelta(days=1)
        raw = plant_actual_from_samples(
            await influx.query_raw_window(current, day_end)
        )
        expected = await influx.query_twin_window(current, day_end)
        for p in plants:
            actual = raw.get(p.vendor_plant_id, {})
            exp = expected.get(p.vendor_plant_id, {})
            if not actual and not exp:
                continue
            energy = sum(actual.values()) * 0.25
            expected_kwh = sum(exp.values()) * 0.25
            peak = max(actual.values()) if actual else 0.0
            pr = (
                min(200.0, energy / expected_kwh * 100.0)
                if expected_kwh > 1.0
                else 0.0
            )
            rows.append(
                {
                    "date_obj": current.date(),
                    "plant_id": p.id,
                    "plant_name": p.name,
                    "energy_kwh": energy,
                    "expected_kwh": expected_kwh,
                    "peak_kw": peak,
                    "pr": pr,
                }
            )
        current = day_end
    return rows


@router.get("/raporlar", response_class=HTMLResponse)
async def reports_page(
    request: Request,
    session: Annotated[AsyncSession, Depends(get_session)],
    user: Annotated[User, Depends(get_web_user)],
) -> HTMLResponse:
    influx = request.app.state.influx
    days_param = request.query_params.get("days", "7")
    days = int(days_param) if days_param.isdigit() and int(days_param) in {7, 14, 30, 60} else 7

    now = datetime.now(tz=UTC)
    end_day = now.astimezone(TRT).date()
    start_day = end_day - timedelta(days=days - 1)
    start_utc = datetime(start_day.year, start_day.month, start_day.day, tzinfo=TRT).astimezone(UTC)
    stop_utc = (
        datetime(end_day.year, end_day.month, end_day.day, tzinfo=TRT) + timedelta(days=1)
    ).astimezone(UTC)

    plants = (await session.scalars(select(Plant).order_by(Plant.name))).all()
    rows = await _load_daily_report(influx, plants, start_utc, stop_utc)

    # KPI özet
    total_energy = sum(r["energy_kwh"] for r in rows)
    total_peak = max((r["peak_kw"] for r in rows), default=0.0)
    prs = [r["pr"] for r in rows if r["pr"] > 0]
    avg_pr = sum(prs) / len(prs) if prs else 0.0

    anomalies_new = int(
        (
            await session.execute(
                select(func.count()).where(AnomalyEvent.started_at >= start_utc)
            )
        ).scalar()
        or 0
    )

    # Enerji eğrisi (portföy toplamı, günlük)
    daily_totals: dict[date, float] = {}
    for r in rows:
        daily_totals[r["date_obj"]] = daily_totals.get(r["date_obj"], 0.0) + r["energy_kwh"]
    energy_points = [
        (datetime(d.year, d.month, d.day, tzinfo=TRT), v)
        for d, v in sorted(daily_totals.items())
    ]
    energy_chart = line_chart(
        [Series("Portföy enerjisi", "#f2b544", energy_points)],
        TRT,
        unit="kWh",
    )

    def _pr_color(pr: float) -> str:
        if pr <= 0:
            return "var(--text-faint)"
        if pr >= 90:
            return "var(--teal)"
        if pr >= 75:
            return "var(--amber)"
        return "var(--coral)"

    daily_rows_view = [
        {
            "date": r["date_obj"].strftime("%d.%m.%Y"),
            "plant": r["plant_name"],
            "energy": _fmt_int(r["energy_kwh"]),
            "expected": _fmt_int(r["expected_kwh"]) if r["expected_kwh"] else "—",
            "pr": f"{r['pr']:.1f}%" if r["pr"] else "—",
            "pr_color": _pr_color(r["pr"]),
            "peak": _fmt_int(r["peak_kw"]),
        }
        for r in sorted(rows, key=lambda x: (x["date_obj"], x["plant_name"]), reverse=True)
    ]

    return templates.TemplateResponse(
        request,
        "reports.html",
        {
            "user": user,
            "section": "reports",
            "plant": None,
            "sidebar_plants": (await sidebar_plants_context(session))[0],
            "days": days,
            "range_start": start_day.strftime("%d.%m.%Y"),
            "range_end": end_day.strftime("%d.%m.%Y"),
            "summary": {
                "energy_kwh": _fmt_int(total_energy),
                "avg_pr": _fmt_1(avg_pr) if avg_pr else "—",
                "peak_kw": _fmt_int(total_peak) if total_peak else "—",
                "anomalies_new": anomalies_new,
            },
            "energy_chart": energy_chart,
            "daily_rows": daily_rows_view,
            "page_title": "Raporlar",
        },
    )


@router.get("/raporlar/indir")
async def report_download(
    request: Request,
    session: Annotated[AsyncSession, Depends(get_session)],
    user: Annotated[User, Depends(get_web_user)],
) -> StreamingResponse:
    influx = request.app.state.influx
    days_param = request.query_params.get("days", "7")
    days = int(days_param) if days_param.isdigit() and int(days_param) in {7, 14, 30, 60} else 7
    now = datetime.now(tz=UTC)
    end_day = now.astimezone(TRT).date()
    start_day = end_day - timedelta(days=days - 1)
    start_utc = datetime(start_day.year, start_day.month, start_day.day, tzinfo=TRT).astimezone(UTC)
    stop_utc = (
        datetime(end_day.year, end_day.month, end_day.day, tzinfo=TRT) + timedelta(days=1)
    ).astimezone(UTC)

    plants = (await session.scalars(select(Plant).order_by(Plant.name))).all()
    rows = await _load_daily_report(influx, plants, start_utc, stop_utc)

    buffer = io.StringIO()
    writer = csv.writer(buffer)
    writer.writerow(["tarih", "tesis", "enerji_kwh", "beklenen_kwh", "performans_orani",
                     "tepe_guc_kw"])
    for r in sorted(rows, key=lambda x: (x["date_obj"], x["plant_name"])):
        writer.writerow([
            r["date_obj"].isoformat(),
            r["plant_name"],
            f"{r['energy_kwh']:.2f}",
            f"{r['expected_kwh']:.2f}",
            f"{r['pr']:.2f}",
            f"{r['peak_kw']:.2f}",
        ])
    buffer.seek(0)
    filename = f"luminmind-rapor-{start_day.isoformat()}-{end_day.isoformat()}.csv"
    return StreamingResponse(
        iter([buffer.getvalue()]),
        media_type="text/csv",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


# ------------------------------ Plant management ------------------------------


def _plant_form_defaults(plant: Plant | None = None) -> dict[str, Any]:
    if plant is None:
        return {
            "name": "", "vendor": "huawei", "vendor_plant_id": "",
            "latitude": "", "longitude": "",
            "dc_capacity_kwp": "", "ac_capacity_kw": "", "timezone": "Europe/Istanbul",
        }
    return {
        "name": plant.name,
        "vendor": plant.vendor,
        "vendor_plant_id": plant.vendor_plant_id,
        "latitude": plant.latitude or "",
        "longitude": plant.longitude or "",
        "dc_capacity_kwp": plant.dc_capacity_kwp or "",
        "ac_capacity_kw": plant.ac_capacity_kw or "",
        "timezone": plant.timezone,
    }


def _opt_float(value: str | None) -> float | None:
    if value is None or value == "":
        return None
    try:
        return float(value)
    except ValueError:
        return None


@router.get("/tesisler/yeni", response_class=HTMLResponse)
async def plant_new_page(
    request: Request,
    session: Annotated[AsyncSession, Depends(get_session)],
    user: Annotated[User, Depends(require_admin)],
) -> HTMLResponse:
    sidebar_plants, _ = await sidebar_plants_context(session)
    return templates.TemplateResponse(
        request,
        "plant_form.html",
        {
            "user": user,
            "section": "plant-new",
            "plant": None,
            "sidebar_plants": sidebar_plants,
            "editing": False,
            "form": _plant_form_defaults(),
            "post_url": "/ui/tesisler/yeni",
            "error": None, "success": None,
            "page_title": "Yeni tesis",
        },
    )


@router.post("/tesisler/yeni")
async def plant_new_submit(
    request: Request,
    session: Annotated[AsyncSession, Depends(get_session)],
    user: Annotated[User, Depends(require_admin)],
    name: Annotated[str, Form()],
    vendor: Annotated[str, Form()],
    vendor_plant_id: Annotated[str, Form()],
    latitude: Annotated[str, Form()] = "",
    longitude: Annotated[str, Form()] = "",
    dc_capacity_kwp: Annotated[str, Form()] = "",
    ac_capacity_kw: Annotated[str, Form()] = "",
    timezone: Annotated[str, Form()] = "Europe/Istanbul",
    tilt_deg: Annotated[str, Form()] = "",
    azimuth_deg: Annotated[str, Form()] = "",
    modules_per_string: Annotated[str, Form()] = "",
    strings: Annotated[str, Form()] = "",
) -> Response:
    existing = (
        await session.scalars(
            select(Plant).where(
                Plant.vendor == vendor, Plant.vendor_plant_id == vendor_plant_id
            )
        )
    ).one_or_none()
    if existing is not None:
        sidebar_plants, _ = await sidebar_plants_context(session)
        return templates.TemplateResponse(
            request,
            "plant_form.html",
            {
                "user": user,
                "section": "plant-new",
                "plant": None,
                "sidebar_plants": sidebar_plants,
                "editing": False,
                "form": _plant_form_defaults(),
                "post_url": "/ui/tesisler/yeni",
                "error": "Bu üretici + üretici kimliği zaten kayıtlı.",
                "success": None,
                "page_title": "Yeni tesis",
            },
            status_code=409,
        )
    plant = Plant(
        owner_id=user.id,
        name=name,
        vendor=vendor,
        vendor_plant_id=vendor_plant_id,
        latitude=_opt_float(latitude),
        longitude=_opt_float(longitude),
        dc_capacity_kwp=_opt_float(dc_capacity_kwp),
        ac_capacity_kw=_opt_float(ac_capacity_kw),
        timezone=timezone or "Europe/Istanbul",
    )
    session.add(plant)
    await session.flush()
    # opsiyonel PV dizisi
    tilt = _opt_float(tilt_deg)
    az = _opt_float(azimuth_deg)
    mps = _opt_float(modules_per_string)
    strs = _opt_float(strings)
    if all(x is not None for x in (tilt, az, mps, strs)):
        session.add(
            PvArray(
                plant_id=plant.id,
                tilt_deg=tilt or 0.0,
                azimuth_deg=az or 0.0,
                modules_per_string=int(mps or 0),
                strings=int(strs or 0),
                module_params={"pdc0": 550.0, "gamma_pdc": -0.0035},
            )
        )
    return RedirectResponse(f"/ui/plants/{plant.id}", status_code=303)


@router.get("/tesisler/{plant_id}/duzenle", response_class=HTMLResponse)
async def plant_edit_page(
    request: Request,
    plant_id: uuid.UUID,
    session: Annotated[AsyncSession, Depends(get_session)],
    user: Annotated[User, Depends(require_admin)],
) -> HTMLResponse:
    plant = await _load_plant(session, plant_id)
    sidebar_plants, _ = await sidebar_plants_context(session)
    return templates.TemplateResponse(
        request,
        "plant_form.html",
        {
            "user": user,
            "section": "plant",
            "plant": plant,
            "sidebar_plants": sidebar_plants,
            "editing": True,
            "form": _plant_form_defaults(plant),
            "post_url": f"/ui/tesisler/{plant.id}/duzenle",
            "error": None, "success": None,
            "page_title": f"{plant.name} · düzenle",
        },
    )


@router.post("/tesisler/{plant_id}/duzenle")
async def plant_edit_submit(
    request: Request,
    plant_id: uuid.UUID,
    session: Annotated[AsyncSession, Depends(get_session)],
    user: Annotated[User, Depends(require_admin)],
    name: Annotated[str, Form()],
    vendor: Annotated[str, Form()],
    vendor_plant_id: Annotated[str, Form()],
    latitude: Annotated[str, Form()] = "",
    longitude: Annotated[str, Form()] = "",
    dc_capacity_kwp: Annotated[str, Form()] = "",
    ac_capacity_kw: Annotated[str, Form()] = "",
    timezone: Annotated[str, Form()] = "Europe/Istanbul",
) -> Response:
    plant = await _load_plant(session, plant_id)
    plant.name = name
    plant.vendor = vendor
    plant.vendor_plant_id = vendor_plant_id
    plant.latitude = _opt_float(latitude)
    plant.longitude = _opt_float(longitude)
    plant.dc_capacity_kwp = _opt_float(dc_capacity_kwp)
    plant.ac_capacity_kw = _opt_float(ac_capacity_kw)
    plant.timezone = timezone or "Europe/Istanbul"
    return RedirectResponse(f"/ui/plants/{plant.id}", status_code=303)


# ------------------------------ User management ------------------------------


@router.get("/kullanicilar", response_class=HTMLResponse)
async def users_page(
    request: Request,
    session: Annotated[AsyncSession, Depends(get_session)],
    user: Annotated[User, Depends(require_admin)],
    error: str | None = None,
    success: str | None = None,
) -> HTMLResponse:
    sidebar_plants, _ = await sidebar_plants_context(session)
    users = (await session.scalars(select(User).order_by(User.email))).all()
    users_view = [
        {
            "id": u.id,
            "email": u.email,
            "role": u.role,
            "created_at": u.created_at.strftime("%d.%m.%Y") if u.created_at else "—",
        }
        for u in users
    ]
    return templates.TemplateResponse(
        request,
        "users.html",
        {
            "user": user,
            "section": "users",
            "plant": None,
            "sidebar_plants": sidebar_plants,
            "users": users_view,
            "error": error, "success": success,
            "page_title": "Kullanıcılar",
        },
    )


@router.post("/kullanicilar/yeni")
async def user_new_submit(
    session: Annotated[AsyncSession, Depends(get_session)],
    user: Annotated[User, Depends(require_admin)],
    email: Annotated[str, Form()],
    password: Annotated[str, Form()],
    role: Annotated[str, Form()] = "viewer",
) -> Response:
    if role not in {"admin", "viewer"}:
        role = "viewer"
    exists = (await session.scalars(select(User).where(User.email == email))).one_or_none()
    if exists is not None:
        return RedirectResponse(
            "/ui/kullanicilar?error=" + quote("Bu e-posta zaten kayıtlı"), status_code=303
        )
    session.add(User(email=email, hashed_password=hash_password(password), role=role))
    return RedirectResponse(
        "/ui/kullanicilar?success=" + quote("Kullanıcı oluşturuldu"), status_code=303
    )


@router.post("/kullanicilar/{user_id}/rol")
async def user_role_submit(
    user_id: uuid.UUID,
    session: Annotated[AsyncSession, Depends(get_session)],
    user: Annotated[User, Depends(require_admin)],
    role: Annotated[str, Form()],
) -> Response:
    if role not in {"admin", "viewer"} or user_id == user.id:
        return RedirectResponse("/ui/kullanicilar", status_code=303)
    target = await session.get(User, user_id)
    if target is not None:
        target.role = role
    return RedirectResponse(
        "/ui/kullanicilar?success=" + quote("Rol güncellendi"), status_code=303
    )
