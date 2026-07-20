"""FastAPI uçtan uca API testleri (sqlite + fake zaman serisi kaynağı)."""

from datetime import UTC, date, datetime, timedelta

import httpx
import pytest
from sqlalchemy.ext.asyncio import create_async_engine

from luminmind.api.main import create_app
from luminmind.config import Settings
from luminmind.core.aggregate import RawSample
from luminmind.core.db import session_scope
from luminmind.core.models import AnomalyEvent, Base, Plant, User
from luminmind.core.security import hash_password
from luminmind.scripts.seed import seed
from luminmind.workers.tasks.arbitrage import run_arbitrage

DAY = date(2026, 7, 19)
T0 = datetime(2026, 7, 19, 10, 0, tzinfo=UTC)
SETTINGS = Settings(jwt_secret="test-secret", lm_use_mock_prices=True)


class FakeTsSource:
    async def query_plant_series(self, vendor_plant_id, metric, start, stop, resolution="15m"):
        return [(T0 + timedelta(minutes=15 * i), 100.0 * (i + 1)) for i in range(3)]

    async def query_raw_window(self, start, stop):
        return [
            RawSample(ts=T0, plant_id="mock-plant-1", inverter_id="i1",
                      fields={"ac_power_kw": 700.0}),
            RawSample(ts=T0 + timedelta(minutes=15), plant_id="mock-plant-1",
                      inverter_id="i1", fields={"ac_power_kw": 5.0}),
        ]

    async def query_twin_window(self, start, stop):
        return {
            "mock-plant-1": {
                T0: 800.0,
                T0 + timedelta(minutes=15): 5.0,  # düşük ışınım → deviation null
            }
        }


@pytest.fixture
async def engine():
    engine = create_async_engine("sqlite+aiosqlite://")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    async with session_scope(engine) as session:
        await seed(session)
        session.add(
            User(
                email="viewer@luminmind.local",
                hashed_password=hash_password("view"),
                role="viewer",
            )
        )
    yield engine
    await engine.dispose()


@pytest.fixture
async def client(engine):
    app = create_app(settings=SETTINGS, engine=engine, influx=FakeTsSource())
    async with app.router.lifespan_context(app):
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test/api/v1") as c:
            yield c


async def login(client, email="admin@luminmind.local", password="admin") -> dict:
    response = await client.post("/auth/login", json={"email": email, "password": password})
    assert response.status_code == 200, response.text
    tokens = response.json()
    return {"Authorization": f"Bearer {tokens['access_token']}"}, tokens


async def test_health_is_public(client):
    response = await client.get("/health")
    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "ok" and body["postgres"] is True
    assert body["influx_configured"] is True


async def test_endpoints_require_auth(client):
    assert (await client.get("/plants")).status_code == 401


async def test_login_rejects_bad_password(client):
    response = await client.post(
        "/auth/login", json={"email": "admin@luminmind.local", "password": "wrong"}
    )
    assert response.status_code == 401


async def test_login_me_and_refresh_flow(client):
    headers, tokens = await login(client)
    me = await client.get("/auth/me", headers=headers)
    assert me.status_code == 200
    assert me.json()["email"] == "admin@luminmind.local"
    assert me.json()["role"] == "admin"

    refreshed = await client.post("/auth/refresh", json={"refresh_token": tokens["refresh_token"]})
    assert refreshed.status_code == 200
    new_headers = {"Authorization": f"Bearer {refreshed.json()['access_token']}"}
    assert (await client.get("/auth/me", headers=new_headers)).status_code == 200


async def test_access_token_not_valid_as_refresh(client):
    _, tokens = await login(client)
    response = await client.post("/auth/refresh", json={"refresh_token": tokens["access_token"]})
    assert response.status_code == 401


async def test_plant_list_and_detail(client):
    headers, _ = await login(client)
    plants = (await client.get("/plants", headers=headers)).json()
    assert len(plants) == 1
    assert plants[0]["vendor_plant_id"] == "mock-plant-1"

    detail = (await client.get(f"/plants/{plants[0]['id']}", headers=headers)).json()
    assert len(detail["inverters"]) == 4
    assert len(detail["batteries"]) == 1
    assert detail["latitude"] == 37.87


async def test_create_plant_requires_admin(client):
    viewer_headers, _ = await login(client, email="viewer@luminmind.local", password="view")
    payload = {"name": "Yeni GES", "vendor": "huawei", "vendor_plant_id": "NE=1"}
    assert (await client.post("/plants", json=payload, headers=viewer_headers)).status_code == 403

    admin_headers, _ = await login(client)
    created = await client.post("/plants", json=payload, headers=admin_headers)
    assert created.status_code == 201
    duplicate = await client.post("/plants", json=payload, headers=admin_headers)
    assert duplicate.status_code == 409


async def test_timeseries_with_tz_conversion(client):
    headers, _ = await login(client)
    plants = (await client.get("/plants", headers=headers)).json()
    params = {
        "metric": "ac_power_kw",
        "start": "2026-07-19T00:00:00Z",
        "end": "2026-07-20T00:00:00Z",
        "resolution": "15m",
        "tz": "Europe/Istanbul",
    }
    response = await client.get(
        f"/plants/{plants[0]['id']}/timeseries", params=params, headers=headers
    )
    assert response.status_code == 200
    points = response.json()
    assert len(points) == 3
    assert points[0]["ts"].endswith("+03:00")  # TRT'ye çevrildi
    assert points[0]["value"] == 100.0


async def test_timeseries_rejects_invalid_metric_for_resolution(client):
    headers, _ = await login(client)
    plants = (await client.get("/plants", headers=headers)).json()
    params = {
        "metric": "ac_power_kw",  # 1d çözünürlükte yok
        "start": "2026-07-19T00:00:00Z",
        "end": "2026-07-20T00:00:00Z",
        "resolution": "1d",
    }
    response = await client.get(
        f"/plants/{plants[0]['id']}/timeseries", params=params, headers=headers
    )
    assert response.status_code == 422


async def test_comparison_marks_low_irradiance_as_null(client):
    headers, _ = await login(client)
    plants = (await client.get("/plants", headers=headers)).json()
    params = {"start": "2026-07-19T00:00:00Z", "end": "2026-07-20T00:00:00Z"}
    points = (
        await client.get(f"/plants/{plants[0]['id']}/comparison", params=params, headers=headers)
    ).json()
    assert len(points) == 2
    assert points[0]["deviation_pct"] == -12.5  # 700 vs 800
    assert points[1]["deviation_pct"] is None  # beklenen 5 kW < eşik


async def test_anomaly_list_and_patch(client, engine):
    headers, _ = await login(client)
    plants = (await client.get("/plants", headers=headers)).json()
    async with session_scope(engine) as session:
        from sqlalchemy import select

        plant_row = (await session.scalars(select(Plant))).first()
        session.add(
            AnomalyEvent(
                plant_id=plant_row.id,
                kind="soiling",
                severity="warning",
                deviation_pct=-9.0,
                started_at=T0,
                status="open",
                evidence={"median_pct": -9.0},
            )
        )

    events = (
        await client.get(f"/plants/{plants[0]['id']}/anomalies", headers=headers)
    ).json()
    assert len(events) == 1 and events[0]["kind"] == "soiling"

    patched = await client.patch(
        f"/anomalies/{events[0]['id']}", json={"status": "acked"}, headers=headers
    )
    assert patched.status_code == 200
    assert patched.json()["status"] == "acked"


async def test_prices_endpoint_returns_mock_curve(client):
    headers, _ = await login(client)
    response = await client.get(
        "/prices", params={"date": DAY.isoformat()}, headers=headers
    )
    assert response.status_code == 200
    assert len(response.json()) == 24


async def test_arbitrage_plan_endpoint(client, engine):
    headers, _ = await login(client)
    plants = (await client.get("/plants", headers=headers)).json()
    url = f"/plants/{plants[0]['id']}/arbitrage/plan"

    missing = await client.get(url, params={"date": DAY.isoformat()}, headers=headers)
    assert missing.status_code == 404

    await run_arbitrage(SETTINGS, day=DAY, engine=engine)
    response = await client.get(url, params={"date": DAY.isoformat()}, headers=headers)
    assert response.status_code == 200
    [plan] = response.json()
    assert plan["market"] == "DAM"
    assert len(plan["slots"]) == 24
    assert plan["expected_revenue_try"] > 0


async def test_timeseries_503_when_influx_missing(engine):
    app = create_app(settings=SETTINGS, engine=engine, influx=None)
    async with app.router.lifespan_context(app):
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test/api/v1") as c:
            headers, _ = await login(c)
            plants = (await c.get("/plants", headers=headers)).json()
            params = {"start": "2026-07-19T00:00:00Z", "end": "2026-07-20T00:00:00Z"}
            response = await c.get(
                f"/plants/{plants[0]['id']}/timeseries", params=params, headers=headers
            )
            assert response.status_code == 503
