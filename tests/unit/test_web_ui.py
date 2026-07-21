"""Sunucu taraflı arayüz (/ui) testleri: oturum çerezi + sayfa içerikleri."""

from datetime import UTC, date, datetime, timedelta, timezone

import httpx
import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import create_async_engine

from luminmind.api.main import create_app
from luminmind.config import Settings
from luminmind.core.aggregate import RawSample
from luminmind.core.db import session_scope
from luminmind.core.models import AnomalyEvent, Base, Plant
from luminmind.scripts.seed import seed
from luminmind.web.charts import Series, line_chart, price_plan_chart
from luminmind.workers.tasks.arbitrage import run_arbitrage

SETTINGS = Settings(jwt_secret="test-secret", lm_use_mock_prices=True)
TRT = timezone(timedelta(hours=3))
NOW = datetime.now(tz=UTC).replace(minute=0, second=0, microsecond=0)


class FakeTsSource:
    async def query_plant_series(self, vendor_plant_id, metric, start, stop, resolution="15m"):
        return [(NOW - timedelta(minutes=15), 480.0), (NOW, 520.0)]

    async def query_raw_window(self, start, stop):
        return [
            RawSample(ts=NOW, plant_id="mock-plant-1", inverter_id="i1",
                      fields={"ac_power_kw": 520.0})
        ]

    async def query_twin_window(self, start, stop):
        return {"mock-plant-1": {NOW: 560.0}}


@pytest.fixture
async def engine():
    engine = create_async_engine("sqlite+aiosqlite://")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    async with session_scope(engine) as session:
        await seed(session)
    yield engine
    await engine.dispose()


@pytest.fixture
async def client(engine):
    app = create_app(settings=SETTINGS, engine=engine, influx=FakeTsSource())
    async with app.router.lifespan_context(app):
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
            yield c


async def do_login(client) -> None:
    response = await client.post(
        "/ui/login",
        data={"email": "admin@luminmind.local", "password": "admin"},
    )
    assert response.status_code == 303
    assert "lm_session" in response.cookies


async def test_root_redirects_to_ui(client):
    response = await client.get("/")
    assert response.status_code in (302, 307)
    assert response.headers["location"] == "/ui"


async def test_pages_redirect_to_login_without_session(client):
    response = await client.get("/ui")
    assert response.status_code == 303
    assert response.headers["location"] == "/ui/login"


async def test_login_page_renders_turkish(client):
    response = await client.get("/ui/login")
    assert response.status_code == 200
    assert "Giriş yap" in response.text


async def test_login_wrong_password_shows_error(client):
    response = await client.post(
        "/ui/login", data={"email": "admin@luminmind.local", "password": "x"}
    )
    assert response.status_code == 401
    assert "hatalı" in response.text


async def test_overview_shows_plant_card(client):
    await do_login(client)
    response = await client.get("/ui")
    assert response.status_code == 200
    assert "Konya GES" in response.text
    assert "Anlık güç" in response.text
    assert "520" in response.text  # son ölçüm
    assert "Açık anomali" in response.text


async def test_plant_detail_renders_svg_chart(client, engine):
    await do_login(client)
    async with session_scope(engine) as session:
        plant = (await session.scalars(select(Plant))).one()
    response = await client.get(f"/ui/plants/{plant.id}?date={NOW.date().isoformat()}")
    assert response.status_code == 200
    assert "<svg" in response.text
    assert "Gerçek" in response.text and "Beklenen" in response.text
    assert "mock-plant-1-inv-01" in response.text  # invertör tablosu
    assert "8S1P" in response.text  # batarya kartı


async def test_anomalies_page_and_status_action(client, engine):
    await do_login(client)
    async with session_scope(engine) as session:
        plant = (await session.scalars(select(Plant))).one()
        session.add(
            AnomalyEvent(
                plant_id=plant.id,
                kind="shading",
                severity="critical",
                deviation_pct=-18.0,
                started_at=NOW,
                status="open",
                evidence={},
            )
        )
    page = await client.get(f"/ui/plants/{plant.id}/anomalies")
    assert "Gölgelenme" in page.text and "Kritik" in page.text

    async with session_scope(engine) as session:
        event = (await session.scalars(select(AnomalyEvent))).one()
    action = await client.post(
        f"/ui/anomalies/{event.id}/status",
        data={"status": "acked", "back": f"/ui/plants/{plant.id}/anomalies"},
    )
    assert action.status_code == 303
    async with session_scope(engine) as session:
        assert (await session.scalars(select(AnomalyEvent))).one().status == "acked"


async def test_arbitrage_page_shows_plan_and_revenue(client, engine):
    await do_login(client)
    plan_day = date(2026, 7, 21)
    await run_arbitrage(SETTINGS, day=plan_day, engine=engine)
    async with session_scope(engine) as session:
        plant = (await session.scalars(select(Plant))).one()
    response = await client.get(
        f"/ui/plants/{plant.id}/arbitrage?date={plan_day.isoformat()}"
    )
    assert response.status_code == 200
    assert "Beklenen gelir" in response.text
    assert "Şarj" in response.text and "Deşarj" in response.text
    assert "<svg" in response.text


async def test_map_page_embeds_sites_and_leaflet(client):
    await do_login(client)
    response = await client.get("/ui/harita")
    assert response.status_code == 200
    # Leaflet + OSM/CARTO gerçek harita altyapısı
    assert "leaflet" in response.text.lower()
    assert "basemaps.cartocdn.com" in response.text
    # Konya GES koordinatları JS'e gömülü
    assert "37.87" in response.text and "32.48" in response.text
    assert "Konya GES" in response.text
    assert 'id="map"' in response.text


async def test_logout_clears_session(client):
    await do_login(client)
    response = await client.post("/ui/logout")
    assert response.status_code == 303
    after = await client.get("/ui")
    assert after.status_code == 303  # tekrar login'e yönlenir


def test_line_chart_empty_state():
    svg = line_chart([], TRT)
    assert "Veri yok" in svg


def test_line_chart_renders_polyline_per_series():
    points = [(NOW, 100.0), (NOW + timedelta(minutes=15), 200.0)]
    svg = line_chart([Series("Gerçek", "#3fa9f5", points)], TRT, unit="kW")
    assert svg.count("<polyline") == 1
    assert "Gerçek" in svg and "kW" in svg


def test_price_plan_chart_colors_actions():
    prices = [(NOW + timedelta(hours=h), 1500.0 + h) for h in range(3)]
    actions = {prices[0][0]: ("charge", 100.0), prices[2][0]: ("discharge", 100.0)}
    svg = price_plan_chart(prices, actions, TRT)
    assert "#2ea87e" in svg and "#d9634c" in svg  # şarj + deşarj renkleri
    assert "Şarj" in svg and "Deşarj" in svg
