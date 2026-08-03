"""Sunucu taraflı Türkçe arayüz (Jinja2, /ui altında).

Oturum: login formu JWT üretir ve HttpOnly çerezde taşır; sayfa bağımlılığı
çerezi doğrular, geçersizse /ui/login'e yönlendirir. Grafikler sunucuda SVG
olarak üretilir (charts.py). Saatler TRT gösterilir.
"""

import json
import uuid
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Annotated, Any
from urllib.parse import quote
from zoneinfo import ZoneInfo

from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response
from fastapi.templating import Jinja2Templates
from markupsafe import Markup
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from luminmind.analytics.accuracy import AccuracyScore, align_series, score_day
from luminmind.analytics.comparison import floor_to_grid
from luminmind.analytics.insights import (
    PRIORITY_LABELS,
    PRIORITY_ORDER,
    LossFinding,
    PortfolioInsights,
    SiteContext,
    finding_from_event,
    portfolio_insights,
    shortfall_from_score,
)
from luminmind.analytics.rollup import (
    CounterKind,
    PlantRollup,
    SiteRollup,
    counter_energy_kwh,
    counter_kind_for,
    energy_kwh,
    peak_kw,
    performance_ratio,
    roll_up,
    sum_series,
)
from luminmind.api.deps import get_session
from luminmind.config import Settings
from luminmind.core.hardening import LoginRateLimiter, client_ip
from luminmind.core.models import (
    AnomalyEvent,
    ArbitragePlan,
    BatterySystem,
    Inverter,
    Plant,
    PvArray,
    Site,
    User,
)
from luminmind.core.security import (
    TokenError,
    create_jwt,
    decode_jwt,
    hash_password,
    verify_password,
)
from luminmind.web.advice import (
    PR_NORMAL_PCT,
    PR_WEAK_PCT,
    Task,
    build_task,
    fmt_number,
    fmt_try,
    money_of,
    performance_chip,
    portfolio_headline,
    sort_tasks,
    tariff_for,
)
from luminmind.web.charts import (
    Segment,
    Series,
    donut,
    heatmap,
    line_chart,
    performance_color,
    price_plan_chart,
    sparkline,
    stacked_bar,
)
from luminmind.web.theme import (
    CHART_ACTUAL,
    CHART_EXPECTED,
    CHART_TEMPERATURE,
    PALETTE,
    css_root_block,
    priority_color,
    series_color,
)

router = APIRouter(prefix="/ui", tags=["web"])
templates = Jinja2Templates(directory=Path(__file__).parent / "templates")
# Palet her sayfada gerekli; her route'un context'ine elle eklemek yerine global.
# Bir sayfada unutulursa o sayfa renksiz açılırdı ve hata vermezdi.
#
# `Markup` şart: blok `<style>` içine giriyor ve Jinja'nın autoescape'i normalde
# tırnakları `&#34;` yapıyor. `<style>` bir "raw text" öğesi olduğu için tarayıcı
# bu karakter referansını çözmez; CSS ayrıştırıcısı `&#34;` içindeki `;`yi
# bildirimin sonu sanır ve `--sans:"Segoe UI"` gibi tırnaklı her token orada
# kesilir. Sonuç sessiz: sayfa hata vermeden tarayıcının varsayılan yüzüyle
# (Windows'ta Times New Roman) açılır. Aşağıdaki test bunu kilitliyor.
templates.env.globals["theme_css"] = Markup(css_root_block())
# Satır içi stil/JS'in renk uydurmaması için palet şablonlara da açılır
# (Leaflet işaretçisi rengi CSS değişkeniyle verilemiyor).
templates.env.globals["palette"] = PALETTE
# Para ve sayı biçimi tek yerden: şablonda `| try_money` yazmak, her sayfada
# ayrı ayrı `round`/`int` zinciri kurup Türkçe ayıraçları kaybetmekten iyi.
templates.env.filters["try_money"] = fmt_try
templates.env.filters["tr_number"] = fmt_number

TRT = ZoneInfo("Europe/Istanbul")
_COOKIE = "lm_session"
# Panel internete açılabildiği için 4 karakterlik parola kabul edilmez.
# Taban NIST SP 800-63B'nin kullanıcı seçimli parolalar için verdiği asgari
# uzunluk. Asıl koruma uzunluk değil, giriş hız sınırı + scrypt hash'i.
_MIN_PASSWORD_LENGTH = 8

KIND_LABELS = {
    "microcrack": "Panel hasarı",
    "shading": "Gölgelenme",
    "soiling": "Panel kirliliği",
}
# "Bekleyen iş" = henüz düzeltilmemiş olay. `acked` da bekleyendir: biri işi
# üstlendi diye kayıp durmaz. Rozetler, sayaçlar ve aksiyon planı aynı tanımı
# kullanmak zorunda — ayrışırsa rozette 3 yazarken listede 1 kalem görünür.
PENDING_STATUSES = ("open", "acked")

SEVERITY_LABELS = {"warning": "Önemli", "critical": "Acil"}
STATUS_LABELS = {"open": "Yapılacak", "acked": "İlgileniliyor", "resolved": "Tamamlandı"}
STATUS_CHIP = {"open": "crit", "acked": "warn", "resolved": "ok"}
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


def _elapsed_window(day: date, now: datetime | None = None) -> tuple[datetime, datetime]:
    """Günün **tamamlanmış** kısmı: bugün için pencere son ızgara sınırına kırpılır.

    İki ayrı çarpıtmayı birlikte kapatıyor:

    * Kırpmamak — dijital ikiz günün tamamını tahmin eder, gerçek üretim yalnızca
      şu ana kadar vardır. Aynı pencereden okunmazsa öğlen performans oranı
      yarıya düşer, "kaçan gelir" günün kalanını da kayıp sayar ve ana sayfa
      sebepsiz alarm verir.
    * Tam `now`'a kırpmak — devam eden 15 dakikalık pencerede ikizin noktası
      vardır ama ölçüm henüz tamamlanmamıştır; her sayfa yüklemesinde 15 dakikaya
      kadar karşılıksız beklenti görünür. Izgara sınırına inmek bunu siler.

    Geçmiş günlerde `now` pencerenin dışında kaldığı için etkisizdir.
    """
    start, stop = _trt_day_window(day)
    return start, min(stop, floor_to_grid(now or datetime.now(tz=UTC)))


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

    Bir liste (görüntüleme için sözlükler) + ham Plant listesi döner; ikincisi
    çağıran route'ların ilk-tesis vb. seçim yapmasına yarar.
    """
    plants = (
        await session.scalars(
            select(Plant).options(selectinload(Plant.sites)).order_by(Plant.name)
        )
    ).all()
    if not plants:
        return [], []
    counts_rows = (
        await session.execute(
            select(AnomalyEvent.plant_id, func.count())
            .where(
                AnomalyEvent.status.in_(PENDING_STATUSES),
                AnomalyEvent.plant_id.in_([p.id for p in plants]),
            )
            .group_by(AnomalyEvent.plant_id)
        )
    ).all()
    counts = {row[0]: row[1] for row in counts_rows}
    return [
        {
            "id": p.id,
            "name": p.name,
            "open_anomalies": counts.get(p.id, 0),
            # Sahalar menüde tesisin altında listelenir; kullanıcı fabrikaları
            # tek tek de inceleyebilsin diye.
            "sites": [
                {"code": s.code, "name": s.name, "series_key": s.series_key}
                for s in p.sites
            ],
        }
        for p in plants
    ], list(plants)


async def _inverter_counts(
    session: AsyncSession, plant_ids: "Sequence[uuid.UUID]"
) -> dict[uuid.UUID, int]:
    """Tesis başına invertör sayısı — cihaz payı üzerinden kayıp tahmini için."""
    if not plant_ids:
        return {}
    rows = (
        await session.execute(
            select(Inverter.plant_id, func.count())
            .where(Inverter.plant_id.in_(plant_ids))
            .group_by(Inverter.plant_id)
        )
    ).all()
    return {row[0]: int(row[1]) for row in rows}


async def load_tasks(
    session: AsyncSession,
    settings: Settings,
    plants: "Sequence[Plant]",
    statuses: "Sequence[str]" = PENDING_STATUSES,
) -> list[Task]:
    """Anomali olaylarını kullanıcı diliyle yazılmış iş listesine çevirir."""
    if not plants:
        return []
    plant_by_id = {p.id: p for p in plants}
    events = (
        await session.scalars(
            select(AnomalyEvent)
            .where(
                AnomalyEvent.plant_id.in_(list(plant_by_id)),
                AnomalyEvent.status.in_(list(statuses)),
            )
            .order_by(AnomalyEvent.started_at.desc())
        )
    ).all()
    counts = await _inverter_counts(session, list(plant_by_id))
    now = datetime.now(tz=UTC)
    return sort_tasks(
        [
            build_task(
                event,
                plant_by_id[event.plant_id],
                tariff_for(plant_by_id[event.plant_id], settings),
                counts.get(event.plant_id, 0),
                now,
            )
            for event in events
        ]
    )


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
    limiter: LoginRateLimiter = request.app.state.login_limiter
    source = client_ip(request)

    if limiter.is_blocked(source):
        wait_min = max(1, limiter.retry_after_s(source) // 60)
        return templates.TemplateResponse(
            request,
            "login.html",
            {
                "user": None,
                "error": f"Çok fazla hatalı deneme. {wait_min} dakika sonra tekrar deneyin.",
            },
            status_code=429,
        )

    user = (await session.scalars(select(User).where(User.email == email))).one_or_none()
    if user is None or not verify_password(password, user.hashed_password):
        limiter.register_failure(source)
        return templates.TemplateResponse(
            request,
            "login.html",
            {"user": None, "error": "E-posta veya şifre hatalı."},
            status_code=401,
        )

    limiter.reset(source)
    ttl_s = settings.lm_session_ttl_min * 60
    token = create_jwt(
        {"sub": user.email, "role": user.role},
        settings.jwt_secret,
        ttl_s=ttl_s,
        token_type="access",
    )
    response = RedirectResponse("/ui", status_code=303)
    response.set_cookie(
        _COOKIE,
        token,
        httponly=True,
        samesite="lax",
        # HTTPS arkasında zorunlu: bayrak olmadan çerez düz HTTP isteğine de
        # eklenir ve ağı dinleyen biri oturumu doğrudan ele geçirir.
        secure=settings.cookie_secure,
        max_age=ttl_s,
    )
    return response


@router.post("/logout")
async def logout(request: Request) -> Response:
    settings: Settings = request.app.state.settings
    response = RedirectResponse("/ui/login", status_code=303)
    # Silme isteği çerezin kurulduğu bayraklarla eşleşmeli, aksi halde tarayıcı
    # eski çerezi tutar ve "çıkış yaptım" görünürken oturum açık kalır.
    response.delete_cookie(
        _COOKIE, httponly=True, samesite="lax", secure=settings.cookie_secure
    )
    return response


# ------------------------------ Overview ------------------------------


async def _daily_energy_series(
    influx: Any, entries: "Sequence[SiteEntry]", today: date, days: int = 14
) -> list[Series]:
    """Son N günün toplam üretimi (kWh/gün) — çizgi grafik için.

    Tek sorgu penceresiyle tüm aralık çekilir, günlere Python'da bölünür; gün
    başına ayrı sorgu atmak ana sayfayı on dört kat yavaşlatırdı.

    Bugünün yaşanmamış kısmı pencereden çıkarılır, bu yüzden son nokta
    kaçınılmaz olarak yarım gündür — grafikte yükselen bir eğrinin sonunda
    düşüş gibi görünür ama eksik gün değil, henüz tamamlanmamış gündür.
    """
    if influx is None or not entries:
        return []
    start, _ = _trt_day_window(today - timedelta(days=days - 1))
    _, stop = _elapsed_window(today)
    keys = [e.key for e in entries]
    actual, expected = await _day_curves(influx, keys, start, stop)
    # KPI kartıyla aynı kaynaktan okunur; grafiği integralden, kartı sayaçtan
    # beslemek aynı ekranda birbirini tutmayan iki üretim rakamı doğururdu.
    counters = await _counters_by_key(influx, keys, start, stop)
    kinds = {e.key: e.counter_kind for e in entries}

    produced: list[tuple[datetime, float]] = []
    forecast: list[tuple[datetime, float]] = []
    for offset in range(days):
        current = today - timedelta(days=days - 1 - offset)
        lo, hi = _trt_day_window(current)
        if lo >= stop:
            break
        day_actual = _metered_or_integrated(counters, actual, lo, hi, kinds)
        day_expected = sum(
            v for curve in expected.values() for ts, v in curve.items() if lo <= ts < hi
        ) * 0.25
        produced.append((lo, round(day_actual, 1)))
        forecast.append((lo, round(day_expected, 1)))

    series = [Series("Ürettiğiniz", CHART_ACTUAL, produced)] if produced else []
    if any(v > 0 for _, v in forecast):
        series.append(Series("Olması gereken", CHART_EXPECTED, forecast, dashed=True))
    return series


@router.get("", response_class=HTMLResponse)
async def portfolio_page(
    request: Request,
    session: Annotated[AsyncSession, Depends(get_session)],
    user: Annotated[User, Depends(get_web_user)],
) -> HTMLResponse:
    influx = request.app.state.influx
    settings: Settings = request.app.state.settings
    now = datetime.now(tz=UTC)
    today = now.astimezone(TRT).date()

    sidebar_plants, plants = await sidebar_plants_context(session)
    days = [await _load_plant_day(session, influx, plant, today) for plant in plants]

    contexts: list[SiteContext] = []
    for day in days:
        contexts.extend(_site_contexts(day, settings))
    portfolio = portfolio_insights(contexts, await _loss_findings(session, plants))
    breakdown_svg, breakdown_legend = _priority_breakdown(portfolio)

    total_energy = sum(d.rollup.actual_kwh for d in days)
    total_expected = sum(d.rollup.expected_kwh for d in days)
    total_power = sum(d.rollup.last_power_kw for d in days)
    total_money = sum(
        money_of(d.rollup.actual_kwh, tariff_for(d.plant, settings)) for d in days
    )
    site_count = sum(d.rollup.site_count for d in days)
    open_task_count = sum(d.rollup.open_anomalies for d in days)

    cards = [
        {
            "plant": d.plant,
            "last_power": f"{_fmt_int(d.rollup.last_power_kw)} kW" if influx else "—",
            "today_energy": f"{_fmt_int(d.rollup.actual_kwh)} kWh" if influx else "—",
            "today_money": (
                fmt_try(money_of(d.rollup.actual_kwh, tariff_for(d.plant, settings)))
                if influx and d.rollup.actual_kwh >= 1
                else "—"
            ),
            "pr": _fmt_1(d.rollup.pr_pct) if d.rollup.pr_pct else "—",
            "pr_chip": performance_chip(d.rollup.pr_pct) if d.rollup.pr_pct else "muted",
            "open_anomalies": d.rollup.open_anomalies,
            "capacity_label": (
                f"{_fmt_1(d.rollup.capacity_kwp)} kWp"
                if d.rollup.capacity_kwp
                else "kurulu güç girilmemiş"
            ),
            "status_class": "ok" if d.rollup.last_power_kw > 0.1 else "muted",
            "status_label": (
                "Üretiyor" if d.rollup.last_power_kw > 0.1 else "Şu an üretim yok"
            ),
            "sparkline_svg": sparkline(
                sorted(sum_series(list(d.actual.values())).items()), color=CHART_ACTUAL
            ),
            "sites": [
                {
                    "name": s.name,
                    "key": s.series_key,
                    "code": next(
                        (e.code for e in d.entries if e.key == s.series_key), ""
                    ),
                    "capacity": _fmt_1(s.capacity_kwp) if s.capacity_kwp else "—",
                    "energy": _fmt_int(s.actual_kwh),
                    "pr": _fmt_1(s.pr_pct) if s.pr_pct else "—",
                    "pr_chip": performance_chip(s.pr_pct) if s.pr_pct else "muted",
                    "open_anomalies": s.open_anomalies,
                }
                for s in d.rollup.sites
            ],
        }
        for d in days
    ]

    # Günlük grafik tek tesis varsayımı yapmaz: tüm sahalar birleştirilir
    all_entries = [entry for d in days for entry in d.entries]
    daily = await _daily_energy_series(influx, all_entries, today)

    locations = _plant_locations(plants)

    return templates.TemplateResponse(
        request,
        "overview.html",
        {
            "user": user,
            "section": "portfolio",
            "plant": None,  # portföyde alt-nav açık olmasın
            "sidebar_plants": sidebar_plants,
            "plants": cards,
            "totals": {
                "power_kw": _fmt_int(total_power) if influx else "—",
                "energy_kwh": _fmt_int(total_energy) if influx else "—",
                "money": fmt_try(total_money) if influx else "—",
                "capacity_mwp": _fmt_1(
                    sum(d.rollup.capacity_kwp for d in days) / 1000.0
                ),
                "plant_count": len(plants),
                "site_count": site_count,
                "open_anomalies": open_task_count,
            },
            "headline": portfolio_headline(
                total_energy,
                total_expected,
                open_task_count,
                has_data=influx is not None and bool(plants),
            ),
            "recoverable": {
                "money": fmt_try(portfolio.recoverable_try_year),
                "kwh": _fmt_int(portfolio.recoverable_kwh_year),
                "pct": _fmt_1(portfolio.recoverable_pct),
                "donut_svg": donut(
                    portfolio.recoverable_pct, color=priority_color(PRIORITY_ORDER[0])
                ),
                "bar_svg": breakdown_svg,
                "legend": breakdown_legend,
                "count": portfolio.total_count,
            },
            "insights": portfolio.top(6),
            "daily_chart": line_chart(daily, TRT, unit="kWh", time_format="%d.%m"),
            "locations": locations,
            "locations_json": json.dumps(locations),
            "influx_ok": influx is not None,
            "mock_mode": settings.lm_use_mock_vendors,
            "today_label": now.astimezone(TRT).strftime("%d.%m.%Y"),
            "page_title": "Portföy",
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
    locations = _plant_locations(plants)
    return templates.TemplateResponse(
        request,
        "map.html",
        {
            "user": user,
            "section": "map",
            "plant": None,
            "sidebar_plants": sidebar_plants,
            "locations": locations,
            "locations_json": json.dumps(locations),
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
            AnomalyEvent.plant_id == plant_id,
            AnomalyEvent.status.in_(PENDING_STATUSES),
        )
    )
    return int(result.scalar() or 0)


# ------------------------------ Saha verisi ------------------------------
# Ölçümler saha serilerinde durur (`Site.series_key`), tesisin
# `vendor_plant_id`'si altında değil. Bu ayrımı atlayan her sayfa boş açılır ve
# hata da vermez — göçün en sinsi yan etkisi buydu. Aşağıdaki yardımcılar tek
# giriş noktası olsun diye var.


@dataclass(frozen=True)
class SiteEntry:
    """Bir sahanın (veya sahasız tesisin) sayfa tarafındaki kimliği."""

    key: str  # Influx `plant_id` etiketi
    name: str
    site: Site | None
    # Sayaç semantiği sahayla taşınır çünkü genel bakış sayfası birden çok
    # tesisin sahalarını tek listede birleştiriyor; tek bir üretici varsayımı
    # çok üreticili kurulumda yanlış semantiği uygular (bkz. `CounterKind`).
    counter_kind: CounterKind = CounterKind.LIFETIME

    @property
    def code(self) -> str:
        return self.site.code if self.site else ""


@dataclass(frozen=True)
class PlantDay:
    """Bir tesisin bir günlük derlenmiş verisi."""

    plant: Plant
    entries: list[SiteEntry]
    rollup: PlantRollup
    actual: dict[str, dict[datetime, float]]
    expected: dict[str, dict[datetime, float]]
    scores: dict[str, AccuracyScore | None]
    device_counts: dict[str, int]
    unscoped_open: int  # sahası belirlenemeyen (göç öncesi) açık olaylar

    def by_key(self, key: str) -> SiteRollup | None:
        return next((s for s in self.rollup.sites if s.series_key == key), None)


async def _site_entries(plant: Plant) -> list[SiteEntry]:
    """Tesisin izlenen serileri: sahalar varsa onlar, yoksa tesisin kendisi."""
    kind = counter_kind_for(plant.vendor)
    sites = await plant.awaitable_attrs.sites
    if sites:
        return [
            SiteEntry(key=s.series_key, name=s.name, site=s, counter_kind=kind)
            for s in sites
        ]
    return [
        SiteEntry(
            key=plant.vendor_plant_id, name=plant.name, site=None, counter_kind=kind
        )
    ]


async def _day_curves(
    influx: Any, keys: "Sequence[str]", start: datetime, stop: datetime
) -> tuple[dict[str, dict[datetime, float]], dict[str, dict[datetime, float]]]:
    """Her seri için (gerçek, beklenen) 15 dakikalık güç eğrileri."""
    if influx is None:
        return {}, {}
    expected_all = await influx.query_twin_window(start, stop)
    actual: dict[str, dict[datetime, float]] = {}
    for key in keys:
        rows = await influx.query_plant_series(key, "ac_power_kw", start, stop, "15m")
        actual[key] = {ts: float(value) for ts, value in rows}
    return actual, {key: expected_all.get(key, {}) for key in keys}


async def _counters_by_key(
    influx: Any, keys: "Sequence[str]", start: datetime, stop: datetime
) -> dict[str, dict[str, list[tuple[datetime, float]]]]:
    """Saha → cihaz → enerji sayacı okumaları.

    Sayaç bildirmeyen kurulumlar (mock, sayacı olmayan üretici) ve sorguyu
    tanımayan sahte Influx sarmalayıcıları boş sözlük alır; çağıranlar güç
    integraline düşer.
    """
    reader = getattr(influx, "query_energy_counters", None) if influx else None
    if reader is None:
        return {}
    return {key: await reader(key, start, stop) for key in keys}


def _metered_energy(
    counters: dict[str, dict[str, list[tuple[datetime, float]]]],
    key: str,
    window: tuple[datetime, datetime] | None = None,
    kind: CounterKind = CounterKind.LIFETIME,
) -> float:
    """Bir sahanın sayaçtan okunan üretimi (kWh); sayaç yoksa 0.

    Cihazlar önce kendi içinde farklanır, sonra toplanır — sayaçları toplayıp
    farklamak, bir cihaz gün ortasında sıfırlandığında sahte bir düşüş üretirdi.

    `kind` üreticinin sayaç semantiğidir ve **geçilmesi zorunlu gibi
    davranılmalı**: varsayılan `LIFETIME`, günlük sıfırlanan sayaçta pencerenin
    ilk okumasını taban sayıp o kadar enerjiyi düşürür (bkz. `CounterKind`).
    """
    total = 0.0
    for rows in counters.get(key, {}).values():
        scoped = (
            [(ts, v) for ts, v in rows if window[0] <= ts < window[1]]
            if window
            else rows
        )
        total += counter_energy_kwh(scoped, kind)
    return total


def _metered_or_integrated(
    counters: dict[str, dict[str, list[tuple[datetime, float]]]],
    curves: dict[str, dict[datetime, float]],
    lo: datetime,
    hi: datetime,
    kinds: "Mapping[str, CounterKind] | None" = None,
) -> float:
    """Gün toplamı: sayaç varsa ondan, yoksa güç eğrisinin integralinden.

    `kinds` saha anahtarı → sayaç semantiği eşlemesidir. Tek bir değer
    yetmiyor: bu fonksiyon genel bakış sayfasında birden çok tesisin sahalarını
    aynı çağrıda topluyor ve tesisler farklı üreticilerde olabilir.
    """
    lookup = kinds or {}
    metered = sum(
        _metered_energy(counters, key, (lo, hi), lookup.get(key, CounterKind.LIFETIME))
        for key in counters
    )
    if metered > 0:
        return metered
    return sum(v for curve in curves.values() for ts, v in curve.items() if lo <= ts < hi) * 0.25


async def _open_by_site(
    session: AsyncSession, plant_id: uuid.UUID
) -> tuple[dict[uuid.UUID, int], int]:
    """Saha başına bekleyen olay sayısı + sahası boş olanların sayısı.

    Aksiyon planıyla aynı durumları sayar (`open` + `acked`); rozetin sayısı ile
    listedeki kalem sayısı aksi hâlde birbirini tutmazdı.
    """
    rows = (
        await session.execute(
            select(AnomalyEvent.site_id, func.count())
            .where(
                AnomalyEvent.plant_id == plant_id,
                AnomalyEvent.status.in_(PENDING_STATUSES),
            )
            .group_by(AnomalyEvent.site_id)
        )
    ).all()
    per_site: dict[uuid.UUID, int] = {}
    unscoped = 0
    for site_id, count in rows:
        if site_id is None:
            unscoped += int(count)
        else:
            per_site[site_id] = int(count)
    return per_site, unscoped


async def _device_counts_by_site(
    session: AsyncSession, plant_id: uuid.UUID, entries: "Sequence[SiteEntry]"
) -> dict[str, int]:
    """Seri anahtarı → invertör sayısı (cihaz payı üzerinden kayıp tahmini için)."""
    rows = (
        await session.execute(
            select(Inverter.site_id, func.count())
            .where(Inverter.plant_id == plant_id)
            .group_by(Inverter.site_id)
        )
    ).all()
    by_site = {site_id: int(count) for site_id, count in rows}
    total = sum(by_site.values())
    return {
        e.key: (by_site.get(e.site.id, 0) if e.site else total) for e in entries
    }


async def _load_plant_day(
    session: AsyncSession, influx: Any, plant: Plant, day: date
) -> PlantDay:
    """Bir tesisin gün verisini saha seviyesinde derler ve tesise toplar."""
    start, stop = _elapsed_window(day)
    entries = await _site_entries(plant)
    actual, expected = await _day_curves(influx, [e.key for e in entries], start, stop)
    counters = await _counters_by_key(influx, [e.key for e in entries], start, stop)
    per_site, unscoped = await _open_by_site(session, plant.id)
    device_counts = await _device_counts_by_site(session, plant.id, entries)

    site_rollups: list[SiteRollup] = []
    scores: dict[str, AccuracyScore | None] = {}
    for entry in entries:
        curve = actual.get(entry.key, {})
        twin = expected.get(entry.key, {})
        capacity_kwp = (
            entry.site.dc_capacity_kwp if entry.site else None
        ) or plant.dc_capacity_kwp
        capacity_kw = (
            (entry.site.ac_capacity_kw if entry.site else None)
            or plant.ac_capacity_kw
            or capacity_kwp
        )
        scores[entry.key] = (
            score_day(
                entry.key,
                day,
                align_series(curve, twin),
                capacity_kw=float(capacity_kw),
                model_version="ui",
            )
            if capacity_kw
            else None
        )
        ordered = sorted(curve.items())
        site_rollups.append(
            SiteRollup(
                series_key=entry.key,
                name=entry.name,
                capacity_kwp=capacity_kwp,
                # Sayaç varsa o kazanır: güç integrali, telemetri çekilemeyen
                # her pencereyi üretimsiz sayar ve boşluk kadar eksik gösterir.
                actual_kwh=round(
                    _metered_energy(counters, entry.key, kind=entry.counter_kind)
                    or energy_kwh(curve),
                    3,
                ),
                expected_kwh=round(energy_kwh(twin), 3),
                peak_kw=peak_kw(curve),
                last_power_kw=ordered[-1][1] if ordered else 0.0,
                open_anomalies=per_site.get(entry.site.id, 0) if entry.site else unscoped,
            )
        )

    return PlantDay(
        plant=plant,
        entries=entries,
        rollup=roll_up(site_rollups, actual_by_site=actual or None),
        actual=actual,
        expected=expected,
        scores=scores,
        device_counts=device_counts,
        unscoped_open=unscoped if any(e.site for e in entries) else 0,
    )


def _site_contexts(day: PlantDay, settings: Settings) -> list[SiteContext]:
    """Kurtarılabilir gelir hesabının saha bağlamları."""
    plant = day.plant
    contexts: list[SiteContext] = []
    for entry in day.entries:
        rollup = day.by_key(entry.key)
        score = day.scores.get(entry.key)
        shortfall = shortfall_from_score(score)
        if shortfall is None and rollup is not None:
            # Skor için yeterli eşleşme yoksa gün toplamlarına düşülür; ikisi de
            # "beklenen − gerçek" ölçer, tek fark hizalanmış nokta sayısı.
            shortfall = max(0.0, rollup.expected_kwh - rollup.actual_kwh)
        contexts.append(
            SiteContext(
                series_key=entry.key,
                name=entry.name,
                capacity_kwp=rollup.capacity_kwp if rollup else None,
                tariff_try_kwh=(
                    (entry.site.feed_in_tariff_try_kwh if entry.site else None)
                    or tariff_for(plant, settings)
                ),
                device_count=day.device_counts.get(entry.key, 1) or 1,
                measured_shortfall_kwh=shortfall,
                accuracy_nmae_pct=score.nmae_pct if score else None,
            )
        )
    if day.unscoped_open:
        # Göç öncesi olayların sahası bilinmiyor: kapasite verilmez, bu yüzden
        # ₺ olarak 0 fiyatlanır ama listede görünür. Uydurma bir sahaya atamak
        # yanlış fabrikayı suçlamak olurdu.
        contexts.append(
            SiteContext(
                series_key=plant.vendor_plant_id,
                name=f"{plant.name} (saha belirsiz)",
                capacity_kwp=None,
                tariff_try_kwh=tariff_for(plant, settings),
            )
        )
    return contexts


async def _loss_findings(
    session: AsyncSession,
    plants: "Sequence[Plant]",
    statuses: "Sequence[str]" = PENDING_STATUSES,
) -> list[LossFinding]:
    """Anomali satırlarını kurtarılabilir gelir katmanının girdisine çevirir.

    `acked` de dahil: "ilgileniliyor" demek "düzeldi" demek değil, kayıp
    sürüyor. Yalnız `open` saymak, biri işi üstlendiği anda ekrandaki parayı
    sıfırlar ve kayıp görünmez hâle gelirdi. `resolved` olaylar dışarıda kalır.
    """
    if not plants:
        return []
    plant_by_id = {p.id: p for p in plants}
    events = (
        await session.scalars(
            select(AnomalyEvent).where(
                AnomalyEvent.plant_id.in_(list(plant_by_id)),
                AnomalyEvent.status.in_(list(statuses)),
            )
        )
    ).all()
    site_keys = {
        s.id: s.series_key
        for s in (
            await session.scalars(
                select(Site).where(Site.plant_id.in_(list(plant_by_id)))
            )
        ).all()
    }
    findings: list[LossFinding] = []
    for event in events:
        key = site_keys.get(event.site_id) if event.site_id else None
        findings.append(
            finding_from_event(event, key or plant_by_id[event.plant_id].vendor_plant_id)
        )
    return findings


def _plant_locations(plants: "Sequence[Plant]") -> list[dict[str, Any]]:
    """Haritaya konacak tesisler; konumu girilmemiş olanlar dışarıda kalır."""
    return [
        {
            "name": p.name,
            "lat": p.latitude,
            "lon": p.longitude,
            "capacity": p.total_dc_capacity_kwp,
        }
        for p in plants
        if p.latitude is not None and p.longitude is not None
    ]


def _priority_breakdown(portfolio: PortfolioInsights) -> tuple[str, list[dict[str, Any]]]:
    """Öncelik kırılım barı + HTML gösterge satırları."""
    segments = [
        Segment(
            label=PRIORITY_LABELS[priority],
            value=portfolio.try_by_priority.get(priority, 0.0),
            color=priority_color(priority),
        )
        for priority in PRIORITY_ORDER
    ]
    legend = [
        {
            "label": segment.label,
            "color": segment.color,
            "money": fmt_try(segment.value),
            "count": portfolio.count_by_priority.get(priority, 0),
        }
        for priority, segment in zip(PRIORITY_ORDER, segments, strict=True)
    ]
    return stacked_bar(segments), legend


# ---- Isı haritası ----

# Isı haritasının saat aralığı (TRT). Gece hücreleri hep "veri yok" olacağı için
# ızgarayı gereksiz genişletir ve gündüzü okunmaz hâle getirir.
_HEATMAP_HOURS = tuple(range(5, 21))
_HEATMAP_MIN_EXPECTED_KWH = 1.0
_HEATMAP_CAP_PCT = 150.0

# Izgaranın renk skalası göstergesi. Eşikler `advice.performance_chip`'ten gelir;
# burada yalnızca etiketleri yazıyoruz, sayıları ikinci kez tanımlamıyoruz.
_HEATMAP_LEGEND = (
    {"label": f"%{PR_NORMAL_PCT:.0f} ve üzeri", "chip": "ok"},
    {"label": f"%{PR_WEAK_PCT:.0f} – %{PR_NORMAL_PCT:.0f}", "chip": "warn"},
    {"label": f"%{PR_WEAK_PCT:.0f} altı", "chip": "crit"},
    {"label": "veri yok", "chip": "muted"},
)


def _hourly_mean_energy(rows: "Sequence[tuple[datetime, float]]") -> dict[int, float]:
    """Ham cihaz noktalarını TRT saatine göre kWh'e indirger.

    Ortalama güç × 1 saat kullanılır; noktaları toplayıp sabit bir aralıkla
    çarpmak, örnekleme sıklığı değiştiğinde (5 dk → 15 dk) enerjiyi üçe
    katlardı. Ortalama, sıklıktan bağımsızdır.
    """
    buckets: dict[int, list[float]] = {}
    for ts, value in rows:
        buckets.setdefault(ts.astimezone(TRT).hour, []).append(float(value))
    return {hour: sum(v) / len(v) for hour, v in buckets.items() if v}


def _device_energy_kwh(points: "Sequence[tuple[datetime, float]]") -> float:
    """Ham cihaz noktalarından günlük enerji (kWh).

    `query_device_series` ham çözünürlükte döner (5 dakikalık çekim). Noktaları
    toplayıp sabit 0,25 ile çarpmak — tesis serisinde olduğu gibi — enerjiyi
    örnekleme sıklığı oranında şişirirdi. Ortalama güç × kapsanan süre
    sıklıktan bağımsızdır.
    """
    if len(points) < 2:
        return 0.0
    ordered = sorted(points)
    span_h = (ordered[-1][0] - ordered[0][0]).total_seconds() / 3600.0
    if span_h <= 0.0:
        return 0.0
    mean_kw = sum(v for _, v in ordered) / len(ordered)
    return mean_kw * span_h


def _hourly_expected_energy(curve: dict[datetime, float]) -> dict[int, float]:
    """15 dakikalık ikiz eğrisinden TRT saat başına beklenen enerji (kWh)."""
    hourly: dict[int, float] = {}
    for ts, value in curve.items():
        hour = ts.astimezone(TRT).hour
        hourly[hour] = hourly.get(hour, 0.0) + value * 0.25
    return hourly


async def _device_heatmap(
    influx: Any,
    day: PlantDay,
    devices: "Sequence[tuple[str, str, str]]",
    day_date: date,
) -> str:
    """Cihaz × saat performans ızgarası.

    Cihazın beklentisi, sahanın ikiz beklentisinin cihaz sayısına eşit
    bölünmesiyle bulunur — üretici cihaz başına anma gücü vermiyor, eşit pay en
    az varsayım içeren seçenek. Bu yüzden ızgara *mutlak* bir doğruluk değil,
    cihazlar arasındaki farkı görmeye yarar.
    """
    if influx is None or not devices:
        return heatmap((), (), [])
    start, stop = _elapsed_window(day_date)
    columns = tuple(f"{hour:02d}" for hour in _HEATMAP_HOURS)
    labels: list[str] = []
    grid: list[list[float | None]] = []
    for site_key, device_id, label in devices:
        rows = await influx.query_device_series(
            site_key, device_id, "ac_power_kw", start, stop
        )
        actual = _hourly_mean_energy(rows)
        share = max(1, day.device_counts.get(site_key, 1))
        expected = _hourly_expected_energy(day.expected.get(site_key, {}))
        labels.append(label)
        grid.append(
            [
                _heatmap_cell(actual.get(hour), expected.get(hour, 0.0) / share)
                for hour in _HEATMAP_HOURS
            ]
        )
    return heatmap(labels, columns, grid, color_of=performance_color)


def _heatmap_cell(actual_kwh: float | None, expected_kwh: float) -> float | None:
    """Tek hücrenin performans oranı; karşılaştırma anlamsızsa None (veri yok)."""
    if actual_kwh is None or expected_kwh < _HEATMAP_MIN_EXPECTED_KWH:
        return None
    return min(_HEATMAP_CAP_PCT, actual_kwh / expected_kwh * 100.0)


def _heatmap_devices(
    inverters: "Sequence[Any]", entries: "Sequence[SiteEntry]", multi_site: bool
) -> list[tuple[str, str, str]]:
    """(seri anahtarı, cihaz no, etiket) — ısı haritası satırları."""
    key_by_site = {e.site.id: e.key for e in entries if e.site}
    fallback = entries[0].key if entries else ""
    devices: list[tuple[str, str, str]] = []
    for inv in inverters:
        key = key_by_site.get(inv.site_id, fallback) if inv.site_id else fallback
        site_name = next((e.name for e in entries if e.key == key), "")
        label = (
            f"{site_name} · {inv.vendor_device_id}"
            if multi_site and site_name
            else f"{inv.vendor_device_id} nolu"
        )
        devices.append((key, inv.vendor_device_id, label))
    return devices


def _scoped(day: PlantDay, site_code: str | None) -> tuple[list[SiteEntry], Site | None]:
    """Sayfanın kapsamı: belirli bir fabrika ya da tesisin tamamı."""
    if not site_code:
        return day.entries, None
    entry = next((e for e in day.entries if e.code == site_code), None)
    if entry is None:
        raise HTTPException(status_code=404, detail="site not found")
    return [entry], entry.site


@dataclass(frozen=True)
class PageScope:
    """Tesis sayfalarının paylaştığı kapsam: hangi tesis, hangi gün, hangi saha(lar).

    Beş sayfa (özet, aksiyon planı, cihazlar, ısı haritası ve saha görünümü) aynı
    prologa ihtiyaç duyuyor. Ayrı ayrı yazılsa biri gün parametresini, bir diğeri
    `?site=` daraltmasını okumayı unuturdu.
    """

    plant: Plant
    day: PlantDay
    entries: list[SiteEntry]
    site: Site | None
    day_date: date
    sidebar_plants: list[dict[str, Any]]

    @property
    def site_code(self) -> str:
        return self.site.code if self.site else ""

    @property
    def keys(self) -> set[str]:
        """Kapsamdaki seri anahtarları; tesis genelinde sahasız olaylar da dahil."""
        keys = {e.key for e in self.entries}
        if self.site is None:
            keys.add(self.plant.vendor_plant_id)
        return keys

    @property
    def title(self) -> str:
        return f"{self.plant.name} · {self.site.name}" if self.site else self.plant.name

    @property
    def base_url(self) -> str:
        """Kapsamın kök adresi — sekme ve gün bağlantıları buradan türetilir."""
        root = f"/ui/plants/{self.plant.id}"
        return f"{root}/sites/{self.site.code}" if self.site else root

    def context(self, section: str, user: User) -> dict[str, Any]:
        return {
            "user": user,
            "section": section,
            "plant": self.plant,
            "site": self.site,
            "sidebar_plants": self.sidebar_plants,
            "day_str": self.day_date.isoformat(),
            "site_code": self.site_code,
            "site_options": [
                {"code": e.code, "name": e.name} for e in self.day.entries if e.site
            ],
            "base_url": self.base_url,
            "plant_url": f"/ui/plants/{self.plant.id}",
            "page_title": self.title,
        }


async def _page_scope(
    request: Request,
    session: AsyncSession,
    plant_id: uuid.UUID,
    site_code: str | None = None,
) -> PageScope:
    """Sayfa kapsamını çözer. Saha yol parametresinden ya da `?site=`'dan gelebilir."""
    plant = await _load_plant(session, plant_id)
    day_date = _parse_day(request.query_params.get("date"), datetime.now(tz=TRT).date())
    day = await _load_plant_day(session, request.app.state.influx, plant, day_date)
    entries, site = _scoped(day, site_code or request.query_params.get("site") or None)
    sidebar_plants, _ = await sidebar_plants_context(session)
    return PageScope(
        plant=plant,
        day=day,
        entries=entries,
        site=site,
        day_date=day_date,
        sidebar_plants=sidebar_plants,
    )


async def _scope_insights(
    session: AsyncSession, settings: Settings, scope: PageScope
) -> PortfolioInsights:
    """Kapsamdaki bulguları fiyatlanmış aksiyon planına çevirir."""
    contexts = [
        c for c in _site_contexts(scope.day, settings) if c.series_key in scope.keys
    ]
    allowed = {c.series_key for c in contexts}
    findings = [
        f
        for f in await _loss_findings(session, [scope.plant])
        if f.site_key in allowed
    ]
    return portfolio_insights(contexts, findings)


def _recoverable_panel(portfolio: PortfolioInsights) -> dict[str, Any]:
    """`.money-hero` panelinin verisi."""
    bar_svg, legend = _priority_breakdown(portfolio)
    return {
        "money": fmt_try(portfolio.recoverable_try_year),
        "kwh": _fmt_int(portfolio.recoverable_kwh_year),
        "pct": _fmt_1(portfolio.recoverable_pct),
        "donut_svg": donut(
            portfolio.recoverable_pct, color=priority_color(PRIORITY_ORDER[0])
        ),
        "bar_svg": bar_svg,
        "legend": legend,
        "count": portfolio.total_count,
    }


async def _scope_inverters(scope: PageScope) -> list[Any]:
    return [
        inv
        for inv in await scope.plant.awaitable_attrs.inverters
        if scope.site is None or inv.site_id == scope.site.id
    ]


def _data_freshness(inverters: "Sequence[Any]") -> dict[str, Any]:
    """Kapsamdaki en yeni telemetri damgası ve kaç cihazın hâlâ veri gönderdiği.

    Bu göstergenin sayfada bulunması bir konfor değil, **doğruluk koşulu**.
    31.07.2026'da veri depoları ölüp çekim saatlerce durduğunda panel günlük
    üretimi 32 kWh gösterdi; gerçek 2.679 kWh idi. Ekrandaki hiçbir şey o sayının
    saatler öncesine ait olduğunu söylemiyordu — üretim düşük görünüyordu, bayat
    görünmüyordu. Kullanıcı "bugün az üretmişiz" diye okur, "veri gelmiyor" diye
    okumaz. Tazelik damgası bu iki durumu ayırt eder.

    `stale` eşiği invertör sağlık modeliyle aynı (`STALE_AFTER`); iki farklı eşik
    tutmak aynı cihazın kartta taze, tabloda çevrimdışı görünmesine yol açardı.
    """
    from luminmind.analytics.inverter_health import STALE_AFTER

    stamps = [
        inv.last_seen_at.replace(tzinfo=UTC)
        if inv.last_seen_at.tzinfo is None
        else inv.last_seen_at
        for inv in inverters
        if inv.last_seen_at is not None
    ]
    total = len(inverters)
    if not stamps:
        return {
            "value": "—",
            "trend": f"{total} cihazdan hiç veri gelmedi" if total else "cihaz kaydı yok",
            "ok": False,
            "stale": True,
            "online": 0,
            "total": total,
        }

    now = datetime.now(tz=UTC)
    newest = max(stamps)
    online = sum(1 for ts in stamps if now - ts <= STALE_AFTER)
    stale = (now - newest) > STALE_AFTER
    return {
        "value": newest.astimezone(TRT).strftime("%H:%M"),
        "trend": (
            f"{_time_ago_tr(newest)} · {online}/{total} cihaz veri gönderiyor"
            if not stale
            else f"{_time_ago_tr(newest)} — veri akışı durmuş olabilir"
        ),
        "ok": not stale,
        "stale": stale,
        "online": online,
        "total": total,
    }


def _production_chart(day: PlantDay, entries: "Sequence[SiteEntry]") -> str:
    """Kapsamdaki üretim eğrisi: toplam gerçek + beklenen, çok sahalıysa saha kırılımı."""
    keys = [e.key for e in entries]
    actual = sum_series([day.actual.get(k, {}) for k in keys])
    expected = sum_series([day.expected.get(k, {}) for k in keys])
    series: list[Series] = []
    if actual:
        series.append(Series("Ürettiğiniz", CHART_ACTUAL, sorted(actual.items())))
    if expected:
        series.append(Series("Olması gereken", CHART_EXPECTED, sorted(expected.items())))
    if len(entries) > 1:
        for index, entry in enumerate(entries):
            curve = day.actual.get(entry.key, {})
            if curve:
                series.append(
                    Series(entry.name, series_color(index + 2), sorted(curve.items()))
                )
    return line_chart(series, TRT, unit="kW")


def _expected_share_series(scope: PageScope, color_offset: int = 0) -> list[Series]:
    """Cihaz grafiğinin karşılaştırma tabanı: **cihaz başına** beklenen güç.

    Sahanın toplam beklentisini çizmek grafiği okunamaz hâle getiriyordu: 650
    kWp'lik iki fabrikanın toplamı ~500 kW'a çıkarken tek cihaz ~140 kW'da
    kalıyor, eksen beklentiye göre ölçekleniyor ve cihaz eğrileri dibe sıkışıyor
    — yani cihazları karşılaştırmak için açılan sayfada cihazlar görünmüyordu.
    Ayrıca üç kat büyük bir şeyle kıyaslamak, sağlıklı bir cihazı arızalı gibi
    gösteriyordu.

    Pay, sahanın beklentisinin cihaz sayısına eşit bölünmesiyle bulunur (ısı
    haritasıyla aynı varsayım: üretici cihaz başına anma gücü vermiyor). Her
    saha için tek çizgi yeter — o sahadaki cihazların payı aynıdır.
    """
    series: list[Series] = []
    for index, entry in enumerate(scope.entries):
        curve = scope.day.expected.get(entry.key, {})
        count = max(1, scope.day.device_counts.get(entry.key, 1))
        if not curve:
            continue
        label = (
            f"{entry.name} · cihaz payı" if len(scope.entries) > 1 else "Cihaz payı (beklenen)"
        )
        series.append(
            Series(
                label,
                # Renkler cihaz eğrilerinin devamından alınır; iki fabrikanın payı
                # aynı renk olsaydı hangisinin hangisi olduğu ayırt edilemezdi.
                CHART_EXPECTED if len(scope.entries) == 1 else series_color(color_offset + index),
                [(ts, value / count) for ts, value in sorted(curve.items())],
                dashed=True,
            )
        )
    return series


def _scope_kpis(
    day: PlantDay, entries: "Sequence[SiteEntry]", tariff: float
) -> dict[str, Any]:
    rollups = [r for r in day.rollup.sites if r.series_key in {e.key for e in entries}]
    actual_kwh = sum(r.actual_kwh for r in rollups)
    expected = sum(r.expected_kwh for r in rollups)
    combined = sum_series([day.actual.get(e.key, {}) for e in entries])
    top = peak_kw(combined)
    pr = min(200.0, performance_ratio(actual_kwh, expected))
    missed_try = money_of(max(0.0, expected - actual_kwh), tariff)
    return {
        "peak_kw": _fmt_int(top) if top else "—",
        "energy_kwh": _fmt_int(actual_kwh) if actual_kwh else "—",
        "money": fmt_try(money_of(actual_kwh, tariff)) if actual_kwh else "—",
        "expected_kwh": _fmt_int(expected) if expected else "—",
        "pr": _fmt_1(pr) if pr else "—",
        "pr_ok": pr >= PR_NORMAL_PCT,
        "pr_chip": performance_chip(pr) if pr else "muted",
        "pr_verdict": _pr_verdict(pr),
        "missed_try": fmt_try(missed_try) if missed_try >= 1 else None,
        "tariff_label": f"{fmt_number(tariff, 2)} ₺/kWh üzerinden",
        "capacity": _fmt_1(sum(r.capacity_kwp or 0.0 for r in rollups)),
    }


async def _render_plant_overview(
    request: Request,
    session: AsyncSession,
    user: User,
    plant_id: uuid.UUID,
    site_code: str | None,
) -> HTMLResponse:
    settings: Settings = request.app.state.settings
    scope = await _page_scope(request, session, plant_id, site_code)
    day, entries, site = scope.day, scope.entries, scope.site
    tariff = (site.feed_in_tariff_try_kwh if site else None) or tariff_for(
        scope.plant, settings
    )

    portfolio = await _scope_insights(session, settings, scope)
    inverters = await _scope_inverters(scope)
    devices = _heatmap_devices(inverters, entries, multi_site=len(entries) > 1)

    return templates.TemplateResponse(
        request,
        "plant_detail.html",
        scope.context("plant", user)
        | {
            "tab": request.query_params.get("tab") or "durum",
            "plant_open_anomalies": await _open_anomaly_count(session, scope.plant.id),
            "kpis": _scope_kpis(day, entries, tariff),
            "recoverable": _recoverable_panel(portfolio),
            "insights": portfolio.top(5),
            "production_chart": _production_chart(day, entries),
            "heatmap_svg": await _device_heatmap(
                request.app.state.influx, day, devices, scope.day_date
            ),
            "heatmap_legend": _HEATMAP_LEGEND,
            "site_rows": [
                {
                    "name": r.name,
                    "code": next((e.code for e in day.entries if e.key == r.series_key), ""),
                    "capacity": _fmt_1(r.capacity_kwp) if r.capacity_kwp else "—",
                    "energy": _fmt_int(r.actual_kwh),
                    "expected": _fmt_int(r.expected_kwh),
                    "peak": _fmt_int(r.peak_kw),
                    "pr": _fmt_1(r.pr_pct) if r.pr_pct else "—",
                    "pr_chip": performance_chip(r.pr_pct) if r.pr_pct else "muted",
                    "open_anomalies": r.open_anomalies,
                }
                for r in day.rollup.sites
            ],
            "inverter_rows": _build_inverter_rows(
                inverters,
                {e.site.id: e.name for e in day.entries if e.site},
                {e.site.id: e.code for e in day.entries if e.site},
            ),
            "freshness": _data_freshness(inverters),
            "batteries": await scope.plant.awaitable_attrs.batteries,
            "unscoped_open": day.unscoped_open,
            "auto_refresh_s": 60,
        },
    )


@router.get("/plants/{plant_id}", response_class=HTMLResponse)
async def plant_detail(
    request: Request,
    plant_id: uuid.UUID,
    session: Annotated[AsyncSession, Depends(get_session)],
    user: Annotated[User, Depends(get_web_user)],
) -> HTMLResponse:
    return await _render_plant_overview(request, session, user, plant_id, None)


@router.get("/plants/{plant_id}/sites/{site_code}", response_class=HTMLResponse)
async def site_detail(
    request: Request,
    plant_id: uuid.UUID,
    site_code: str,
    session: Annotated[AsyncSession, Depends(get_session)],
    user: Annotated[User, Depends(get_web_user)],
) -> HTMLResponse:
    """Aynı sayfa, tek fabrikaya daraltılmış — çatı altındaki tek tek inceleme."""
    return await _render_plant_overview(request, session, user, plant_id, site_code)


# ------------------------------ Aksiyon planı (ROI) ------------------------------


@router.get("/plants/{plant_id}/insights", response_class=HTMLResponse)
async def plant_insights_page(
    request: Request,
    plant_id: uuid.UUID,
    session: Annotated[AsyncSession, Depends(get_session)],
    user: Annotated[User, Depends(get_web_user)],
) -> HTMLResponse:
    """Kurtarılabilir gelire göre sıralı bulgu tablosu."""
    settings: Settings = request.app.state.settings
    scope = await _page_scope(request, session, plant_id)
    portfolio = await _scope_insights(session, settings, scope)
    return templates.TemplateResponse(
        request,
        "insights.html",
        scope.context("insights", user)
        | {
            "recoverable": _recoverable_panel(portfolio),
            "insights": portfolio.insights,
            "unscoped_open": scope.day.unscoped_open,
        },
    )


# ------------------------------ Cihaz karşılaştırma ------------------------------


@router.get("/plants/{plant_id}/devices", response_class=HTMLResponse)
async def plant_devices_page(
    request: Request,
    plant_id: uuid.UUID,
    session: Annotated[AsyncSession, Depends(get_session)],
    user: Annotated[User, Depends(get_web_user)],
) -> HTMLResponse:
    """Cihazların aynı eksende karşılaştırılması — hangi invertör geride kalıyor?"""
    influx = request.app.state.influx
    scope = await _page_scope(request, session, plant_id)
    start, stop = _trt_day_window(scope.day_date)
    inverters = await _scope_inverters(scope)
    devices = _heatmap_devices(inverters, scope.entries, multi_site=len(scope.entries) > 1)

    power: list[Series] = []
    temperature: list[Series] = []
    if influx is not None:
        for index, (key, device_id, label) in enumerate(devices):
            color = series_color(index)
            rows = await influx.query_device_series(
                key, device_id, "ac_power_kw", start, stop
            )
            if rows:
                power.append(Series(label, color, sorted(rows)))
            temps = await influx.query_device_series(key, device_id, "temp_c", start, stop)
            if temps:
                temperature.append(Series(label, color, sorted(temps)))
    power.extend(_expected_share_series(scope, color_offset=len(devices)))

    return templates.TemplateResponse(
        request,
        "devices.html",
        scope.context("devices", user)
        | {
            "power_chart": line_chart(power, TRT, unit="kW"),
            "temp_chart": line_chart(temperature, TRT, unit="°C"),
            "device_count": len(devices),
            "inverter_rows": _build_inverter_rows(
                inverters,
                {e.site.id: e.name for e in scope.day.entries if e.site},
                {e.site.id: e.code for e in scope.day.entries if e.site},
            ),
        },
    )


# ------------------------------ Isı haritası ------------------------------


@router.get("/plants/{plant_id}/heatmap", response_class=HTMLResponse)
async def plant_heatmap_page(
    request: Request,
    plant_id: uuid.UUID,
    session: Annotated[AsyncSession, Depends(get_session)],
    user: Annotated[User, Depends(get_web_user)],
) -> HTMLResponse:
    """Cihaz × saat performans ızgarası."""
    scope = await _page_scope(request, session, plant_id)
    inverters = await _scope_inverters(scope)
    devices = _heatmap_devices(inverters, scope.entries, multi_site=len(scope.entries) > 1)
    return templates.TemplateResponse(
        request,
        "heatmap.html",
        scope.context("heatmap", user)
        | {
            "heatmap_svg": await _device_heatmap(
                request.app.state.influx, scope.day, devices, scope.day_date
            ),
            "heatmap_legend": _HEATMAP_LEGEND,
            "device_count": len(devices),
            "hours_label": (
                f"{_HEATMAP_HOURS[0]:02d}:00 – {_HEATMAP_HOURS[-1]:02d}:59 (TRT)"
            ),
        },
    )


def _pr_verdict(pr: float) -> str:
    """Performans oranını tek kelimelik yargıya çevirir (kullanıcı % ile konuşmaz)."""
    if pr <= 0:
        return "karşılaştırma için veri bekleniyor"
    if pr >= 95:
        return "havanın elverdiği kadarını üretmişsiniz"
    if pr >= 85:
        return "normal seyrinde"
    if pr >= 70:
        return "olması gerekenin altında"
    return "belirgin biçimde düşük"


def _build_inverter_rows(
    inverters: "Sequence[Any]",
    site_names: dict[uuid.UUID, str] | None = None,
    site_codes: dict[uuid.UUID, str] | None = None,
) -> list[dict[str, Any]]:
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
                temp_color = "var(--red)"
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
                # Cihaz numarası yalnızca fabrika içinde tekil; tesis genelinde
                # listelenirken hangi fabrikanın "1"i olduğu yazılmak zorunda.
                # `site_code` bağlantıyı fabrikaya sabitler, yoksa cihaz sayfası
                # iki kayıt arasında seçim yapamaz.
                "site_name": (site_names or {}).get(inv.site_id, ""),
                "site_code": (site_codes or {}).get(inv.site_id, ""),
                "health_chip": health_chip,
                "health_label": health_label,
                "power": (
                    f"{inv.last_power_kw:.1f} kW" if inv.last_power_kw is not None else "—"
                ),
                "temp": f"{inv.last_temp_c:.1f} °C" if inv.last_temp_c is not None else "—",
                "temp_color": temp_color,
                "error_or_status": error_or_status,
                "last_seen": last_seen_local,
            }
        )
    return rows


# ------------------------------ Inverter detail ------------------------------


async def _resolve_inverter(
    session: AsyncSession, plant: Plant, device_id: str, site_code: str | None
) -> tuple[Inverter, Site | None]:
    """Cihazı çözer. Cihaz numarası yalnızca fabrika içinde tekil olduğu için
    sahasız istek birden çok kayda denk gelebilir; o durumda tahmin etmek yerine
    404 döner — yanlış fabrikanın cihazını göstermek sessiz bir yanlış teşhistir.
    """
    criteria = [Inverter.plant_id == plant.id, Inverter.vendor_device_id == device_id]
    site: Site | None = None
    if site_code:
        sites = await plant.awaitable_attrs.sites
        site = next((s for s in sites if s.code == site_code), None)
        if site is None:
            raise HTTPException(status_code=404, detail="site not found")
        criteria.append(Inverter.site_id == site.id)

    matches = (await session.scalars(select(Inverter).where(*criteria))).all()
    if not matches:
        raise HTTPException(status_code=404, detail="inverter not found")
    if len(matches) > 1:
        raise HTTPException(
            status_code=404,
            detail=f"device {device_id} exists in more than one site; use /sites/{{code}}/",
        )
    inverter = matches[0]
    if site is None and inverter.site_id is not None:
        site = await session.get(Site, inverter.site_id)
    return inverter, site


async def _render_inverter_detail(
    request: Request,
    session: AsyncSession,
    user: User,
    plant_id: uuid.UUID,
    device_id: str,
    site_code: str | None,
) -> HTMLResponse:
    from luminmind.analytics.inverter_health import (
        CRITICAL_OVERHEAT_C,
        KIND_INV_ERROR,
        KIND_INV_OFFLINE,
        KIND_INV_OVERHEAT,
        OVERHEAT_C,
    )

    settings: Settings = request.app.state.settings
    plant = await _load_plant(session, plant_id)
    inverter, site = await _resolve_inverter(session, plant, device_id, site_code)

    day = _parse_day(request.query_params.get("date"), datetime.now(tz=TRT).date())
    start, stop = _elapsed_window(day)
    # Ölçümler sahanın serisinde; tesis anahtarıyla sormak boş grafik döndürür.
    series_key = site.series_key if site else plant.vendor_plant_id

    influx = request.app.state.influx
    power_points: list[tuple[datetime, float]] = []
    temp_points: list[tuple[datetime, float]] = []
    counters: dict[str, list[tuple[datetime, float]]] = {}
    if influx is not None:
        power_points = await influx.query_device_series(
            series_key, device_id, "ac_power_kw", start, stop
        )
        temp_points = await influx.query_device_series(
            series_key, device_id, "temp_c", start, stop
        )
        counters = (await _counters_by_key(influx, [series_key], start, stop)).get(
            series_key, {}
        )

    peak_power = max((v for _, v in power_points), default=0.0)
    # Cihazın kendi sayacı esas alınır — tesis kartıyla aynı kaynak. Sayaç yoksa
    # ortalama güç × geçen süreye düşülür (sabit 0,25 ile çarpmak, 5 dakikalık
    # çekimde enerjiyi üçe katlardı).
    energy_kwh = counter_energy_kwh(
        counters.get(device_id, []), counter_kind_for(plant.vendor)
    ) or _device_energy_kwh(power_points)

    # Sağlık rozeti — plant_detail'daki mantıkla aynı, tek satır için
    rows = _build_inverter_rows([inverter])
    health = {
        "chip": rows[0]["health_chip"] if rows else "muted",
        "label": rows[0]["health_label"] if rows else "Beklemede",
    }

    temp_color = "var(--text)"
    if inverter.last_temp_c is not None:
        if inverter.last_temp_c > CRITICAL_OVERHEAT_C:
            temp_color = "var(--red)"
        elif inverter.last_temp_c > OVERHEAT_C:
            temp_color = "var(--amber)"

    last_seen_local = "veri yok"
    if inverter.last_seen_at is not None:
        _ls = (
            inverter.last_seen_at.replace(tzinfo=UTC)
            if inverter.last_seen_at.tzinfo is None else inverter.last_seen_at
        )
        last_seen_local = _ls.astimezone(TRT).strftime("%d.%m %H:%M")
    money = money_of(energy_kwh, tariff_for(plant, settings))
    kpis = {
        "last_power": (
            f"{inverter.last_power_kw:.1f}" if inverter.last_power_kw is not None else "—"
        ),
        "last_seen": last_seen_local,
        "peak_power": _fmt_int(peak_power) if peak_power else "—",
        "energy_kwh": _fmt_int(energy_kwh) if energy_kwh else "—",
        "money": fmt_try(money) if money >= 1 else "—",
        "last_temp": f"{inverter.last_temp_c:.1f}" if inverter.last_temp_c is not None else "—",
        "temp_color": temp_color,
        "temp_hot": inverter.last_temp_c is not None and inverter.last_temp_c > OVERHEAT_C,
        "error_or_status": rows[0]["error_or_status"] if rows else "—",
    }

    # Cihaza dair son 10 olay (evidence.device_id eşleşen). Saha filtresi zorunlu:
    # iki fabrikada da "1 nolu" cihaz var, sahasız sorgu ikisinin olaylarını
    # birbirine karıştırırdı.
    event_scope = [
        AnomalyEvent.plant_id == plant.id,
        AnomalyEvent.kind.in_({KIND_INV_OFFLINE, KIND_INV_OVERHEAT, KIND_INV_ERROR}),
    ]
    if site is not None:
        event_scope.append(AnomalyEvent.site_id == site.id)
    all_events = (
        await session.scalars(
            select(AnomalyEvent)
            .where(*event_scope)
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
                [Series("Güç", CHART_ACTUAL, power_points)], TRT, unit="kW"
            ),
            "temp_chart": line_chart(
                [Series("Sıcaklık", CHART_TEMPERATURE, temp_points)], TRT, unit="°C"
            ),
            "device_events": device_events,
            "site": site,
            "plant_url": f"/ui/plants/{plant.id}",
            "page_title": (
                f"{site.name} · İnvertör {device_id}"
                if site
                else f"{plant.name} · İnvertör {device_id}"
            ),
            "auto_refresh_s": 60,
        },
    )


@router.get("/plants/{plant_id}/inverters/{device_id}", response_class=HTMLResponse)
async def inverter_detail(
    request: Request,
    plant_id: uuid.UUID,
    device_id: str,
    session: Annotated[AsyncSession, Depends(get_session)],
    user: Annotated[User, Depends(get_web_user)],
) -> HTMLResponse:
    """Sahasız erişim — yalnız cihaz numarası tesiste tekilse çalışır."""
    return await _render_inverter_detail(request, session, user, plant_id, device_id, None)


@router.get(
    "/plants/{plant_id}/sites/{site_code}/inverters/{device_id}", response_class=HTMLResponse
)
async def site_inverter_detail(
    request: Request,
    plant_id: uuid.UUID,
    site_code: str,
    device_id: str,
    session: Annotated[AsyncSession, Depends(get_session)],
    user: Annotated[User, Depends(get_web_user)],
) -> HTMLResponse:
    """Fabrika belirtilmiş erişim — aynı numaralı cihazlar bu yolla ayrışır."""
    return await _render_inverter_detail(
        request, session, user, plant_id, device_id, site_code
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

    settings: Settings = request.app.state.settings
    event = await session.get(AnomalyEvent, anomaly_id)
    if event is None:
        raise HTTPException(status_code=404, detail="anomaly not found")
    plant = await session.get(Plant, event.plant_id)
    if plant is None:
        raise HTTPException(status_code=404, detail="plant not found")

    kind_label = KIND_LABELS.get(event.kind) or DEVICE_KIND_LABELS.get(event.kind, event.kind)
    device_id = event.evidence.get("device_id") if isinstance(event.evidence, dict) else None
    counts = await _inverter_counts(session, [plant.id])
    task = build_task(event, plant, tariff_for(plant, settings), counts.get(plant.id, 0))

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
                plant.vendor_plant_id, "ac_power_kw", w_start, w_stop, "15m"
            )
        chart_html = line_chart([Series("Güç", CHART_ACTUAL, points)], TRT, unit="kW")

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
            "task": task,
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
    settings: Settings = request.app.state.settings
    plant = await _load_plant(session, plant_id)
    tasks = await load_tasks(
        session, settings, [plant], statuses=("open", "acked", "resolved")
    )
    stats = {
        "open": sum(1 for t in tasks if t.status == "open"),
        "acked": sum(1 for t in tasks if t.status == "acked"),
        "resolved": sum(1 for t in tasks if t.status == "resolved"),
    }
    # Açık + ilgilenilen işlerin toplam günlük parasal etkisi
    pending_loss = sum(t.daily_loss_try for t in tasks if t.status != "resolved")
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
            "tasks": tasks,
            "stats": stats,
            "pending_loss": fmt_try(pending_loss) if pending_loss >= 1 else None,
            "back_url": f"/ui/plants/{plant.id}/anomalies",
            "page_title": f"{plant.name} · Yapılacaklar",
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


# ------------------------------ Plant management ------------------------------


def _plant_form_defaults(plant: Plant | None = None) -> dict[str, Any]:
    if plant is None:
        return {
            "name": "", "vendor": "huawei", "vendor_plant_id": "",
            "latitude": "", "longitude": "",
            "dc_capacity_kwp": "", "ac_capacity_kw": "", "timezone": "Europe/Istanbul",
            "feed_in_tariff_try_kwh": "",
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
        "feed_in_tariff_try_kwh": plant.feed_in_tariff_try_kwh or "",
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
    settings: Settings = request.app.state.settings
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
            "default_tariff": fmt_number(settings.lm_default_tariff_try_kwh, 2),
            "post_url": "/ui/tesisler/yeni",
            "error": None, "success": None,
            "page_title": "Yeni santral",
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
    feed_in_tariff_try_kwh: Annotated[str, Form()] = "",
    tilt_deg: Annotated[str, Form()] = "",
    azimuth_deg: Annotated[str, Form()] = "",
    modules_per_string: Annotated[str, Form()] = "",
    strings: Annotated[str, Form()] = "",
) -> Response:
    settings: Settings = request.app.state.settings
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
                "default_tariff": fmt_number(settings.lm_default_tariff_try_kwh, 2),
                "post_url": "/ui/tesisler/yeni",
                "error": "Bu cihaz markası ve tesis kimliği zaten kayıtlı.",
                "success": None,
                "page_title": "Yeni santral",
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
        feed_in_tariff_try_kwh=_opt_float(feed_in_tariff_try_kwh),
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
    settings: Settings = request.app.state.settings
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
            "default_tariff": fmt_number(settings.lm_default_tariff_try_kwh, 2),
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
    feed_in_tariff_try_kwh: Annotated[str, Form()] = "",
) -> Response:
    plant = await _load_plant(session, plant_id)
    plant.name = name
    plant.vendor = vendor
    plant.vendor_plant_id = vendor_plant_id
    plant.latitude = _opt_float(latitude)
    plant.longitude = _opt_float(longitude)
    plant.dc_capacity_kwp = _opt_float(dc_capacity_kwp)
    plant.ac_capacity_kw = _opt_float(ac_capacity_kw)
    plant.feed_in_tariff_try_kwh = _opt_float(feed_in_tariff_try_kwh)
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
    if len(password) < _MIN_PASSWORD_LENGTH:
        return RedirectResponse(
            "/ui/kullanicilar?error="
            + quote(f"Şifre en az {_MIN_PASSWORD_LENGTH} karakter olmalı"),
            status_code=303,
        )
    exists = (await session.scalars(select(User).where(User.email == email))).one_or_none()
    if exists is not None:
        return RedirectResponse(
            "/ui/kullanicilar?error=" + quote("Bu e-posta zaten kayıtlı"), status_code=303
        )
    session.add(User(email=email, hashed_password=hash_password(password), role=role))
    return RedirectResponse(
        "/ui/kullanicilar?success=" + quote("Kullanıcı oluşturuldu"), status_code=303
    )


@router.post("/kullanicilar/{user_id}/sifre")
async def user_password_submit(
    user_id: uuid.UUID,
    session: Annotated[AsyncSession, Depends(get_session)],
    user: Annotated[User, Depends(require_admin)],
    password: Annotated[str, Form()],
) -> Response:
    """Parola değiştirme — paneli dışa açmadan önce zorunlu adım.

    Seed hesabı `admin` parolasıyla gelir; bu hesap değiştirilemezse panel
    internete açıldığı anda herkese açık demektir.
    """
    if len(password) < _MIN_PASSWORD_LENGTH:
        return RedirectResponse(
            "/ui/kullanicilar?error="
            + quote(f"Şifre en az {_MIN_PASSWORD_LENGTH} karakter olmalı"),
            status_code=303,
        )
    target = await session.get(User, user_id)
    if target is None:
        return RedirectResponse("/ui/kullanicilar", status_code=303)
    target.hashed_password = hash_password(password)
    return RedirectResponse(
        "/ui/kullanicilar?success=" + quote(f"{target.email} şifresi güncellendi"),
        status_code=303,
    )


@router.post("/kullanicilar/{user_id}/sil")
async def user_delete_submit(
    user_id: uuid.UUID,
    session: Annotated[AsyncSession, Depends(get_session)],
    user: Annotated[User, Depends(require_admin)],
) -> Response:
    """Kullanıcıyı siler. Kendini silme ve son yöneticiyi silme engellenir."""
    if user_id == user.id:
        return RedirectResponse(
            "/ui/kullanicilar?error=" + quote("Kendi hesabınızı silemezsiniz"), status_code=303
        )
    target = await session.get(User, user_id)
    if target is None:
        return RedirectResponse("/ui/kullanicilar", status_code=303)
    if target.role == "admin":
        admin_count = await session.scalar(
            select(func.count()).select_from(User).where(User.role == "admin")
        )
        if (admin_count or 0) <= 1:
            return RedirectResponse(
                "/ui/kullanicilar?error=" + quote("Son yönetici silinemez"), status_code=303
            )
    owned = await session.scalar(
        select(func.count()).select_from(Plant).where(Plant.owner_id == user_id)
    )
    if owned:
        return RedirectResponse(
            "/ui/kullanicilar?error="
            + quote(f"Bu kullanıcıya bağlı {owned} santral var; önce sahipliği devredin"),
            status_code=303,
        )
    email = target.email
    await session.delete(target)
    return RedirectResponse(
        "/ui/kullanicilar?success=" + quote(f"{email} silindi"), status_code=303
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
