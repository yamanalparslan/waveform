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
from luminmind.core.models import AnomalyEvent, Base, Inverter, Plant, User
from luminmind.scripts.seed import seed
from luminmind.web.charts import Series, line_chart, price_plan_chart
from luminmind.workers.tasks.arbitrage import run_arbitrage

SETTINGS = Settings(jwt_secret="test-secret", lm_use_mock_prices=True)
TRT = timezone(timedelta(hours=3))
NOW = datetime.now(tz=UTC).replace(minute=0, second=0, microsecond=0)


class FakeTsSource:
    async def query_plant_series(self, vendor_plant_id, metric, start, stop, resolution="15m"):
        return [(NOW - timedelta(minutes=15), 480.0), (NOW, 520.0)]

    async def query_device_series(
        self, vendor_plant_id, vendor_device_id, metric, start, stop
    ):
        base = 100.0 if metric == "ac_power_kw" else 45.0
        return [(NOW - timedelta(minutes=15), base), (NOW, base + 20)]

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


async def do_login(client, email: str = "admin@luminmind.local", password: str = "admin") -> None:
    response = await client.post(
        "/ui/login", data={"email": email, "password": password}
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
    assert "Anlık toplam güç" in response.text
    assert "520" in response.text  # son ölçüm
    assert "Açık anomali" in response.text
    assert "Son Olaylar" in response.text  # yeni panel


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
    assert "Beklenen günlük gelir" in response.text
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


async def test_reports_page_renders(client):
    await do_login(client)
    response = await client.get("/ui/raporlar?days=7")
    assert response.status_code == 200
    assert "Portföy" in response.text
    assert "Toplam üretim" in response.text
    assert "Ortalama PR" in response.text
    assert "CSV indir" in response.text


async def test_reports_csv_download(client):
    await do_login(client)
    response = await client.get("/ui/raporlar/indir?days=7")
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/csv")
    assert "attachment" in response.headers["content-disposition"]
    # başlık satırı
    assert "tarih,tesis,enerji_kwh" in response.text


async def test_plant_new_form_admin_only(client, engine):
    from luminmind.core.security import hash_password as _hash

    async with session_scope(engine) as session:
        session.add(User(
            email="viewer@luminmind.local",
            hashed_password=_hash("v"),
            role="viewer",
        ))
    await do_login(client, email="viewer@luminmind.local", password="v")
    forbidden = await client.get("/ui/tesisler/yeni")
    assert forbidden.status_code == 403

    await do_login(client)  # admin olarak yeniden gir
    page = await client.get("/ui/tesisler/yeni")
    assert page.status_code == 200
    assert "Yeni tesis" in page.text
    assert "Haritaya tıklayarak" in page.text


async def test_plant_new_creates_and_redirects(client, engine):
    await do_login(client)
    response = await client.post("/ui/tesisler/yeni", data={
        "name": "İzmir GES 2",
        "vendor": "huawei",
        "vendor_plant_id": "NE=99",
        "latitude": "38.4",
        "longitude": "27.1",
        "dc_capacity_kwp": "500",
        "ac_capacity_kw": "500",
        "timezone": "Europe/Istanbul",
    })
    assert response.status_code == 303
    assert response.headers["location"].startswith("/ui/plants/")

    from sqlalchemy import select as _sel
    async with session_scope(engine) as session:
        plant = (await session.scalars(_sel(Plant).where(Plant.name == "İzmir GES 2"))).one()
        assert plant.dc_capacity_kwp == 500.0


async def test_plant_new_rejects_duplicate(client, engine):
    await do_login(client)
    # Konya GES mock-plant-1 zaten seed'de var, aynı vendor+id çakışır
    response = await client.post("/ui/tesisler/yeni", data={
        "name": "Kopya",
        "vendor": "mock",
        "vendor_plant_id": "mock-plant-1",
    })
    assert response.status_code == 409
    assert "zaten kayıtlı" in response.text


async def test_plant_edit_updates(client, engine):
    await do_login(client)
    from sqlalchemy import select as _sel
    async with session_scope(engine) as session:
        plant = (await session.scalars(_sel(Plant))).one()
        pid = plant.id

    response = await client.post(f"/ui/tesisler/{pid}/duzenle", data={
        "name": "Konya GES (yeni)",
        "vendor": "mock",
        "vendor_plant_id": "mock-plant-1",
        "latitude": "37.87",
        "longitude": "32.48",
        "dc_capacity_kwp": "1000",
        "ac_capacity_kw": "1000",
        "timezone": "Europe/Istanbul",
    })
    assert response.status_code == 303

    async with session_scope(engine) as session:
        plant = await session.get(Plant, pid)
        assert plant.name == "Konya GES (yeni)"


async def test_users_page_admin_can_create(client, engine):
    await do_login(client)
    create = await client.post(
        "/ui/kullanicilar/yeni",
        data={"email": "yeni@luminmind.local", "password": "test123", "role": "viewer"},
    )
    assert create.status_code == 303

    # Success mesajı ?success= query üzerinden geliyor; redirect'i elle takip et
    success_url = create.headers["location"]
    page = await client.get(success_url)
    assert page.status_code == 200
    assert "yeni@luminmind.local" in page.text
    assert "Kullanıcı oluşturuldu" in page.text

    from sqlalchemy import select as _sel
    async with session_scope(engine) as session:
        u = (
            await session.scalars(_sel(User).where(User.email == "yeni@luminmind.local"))
        ).one()
        assert u.role == "viewer"


async def test_sidebar_groups_plants_with_dot_separator(client, engine):
    """Parent kaydı varsa `X · A`, `X · B` onun altında child olarak listelenir."""
    await do_login(client)
    async with session_scope(engine) as session:
        admin_id = (await session.scalars(select(User))).first().id
        # Parent tesis (adı child'ların ön ekiyle birebir eşleşmeli)
        session.add(Plant(
            name="Tescom İzmir GES", vendor="tescom",
            vendor_plant_id="tescom-izmir",
            owner_id=admin_id,
        ))
        session.add(Plant(
            name="Tescom İzmir GES · Mekanik", vendor="tescom",
            vendor_plant_id="tescom-izmir-mekanik",
            owner_id=admin_id,
        ))
        session.add(Plant(
            name="Tescom İzmir GES · Uretim", vendor="tescom",
            vendor_plant_id="tescom-izmir-uretim",
            owner_id=admin_id,
        ))

    response = await client.get("/ui")
    body = response.text
    # Parent link + child linkleri hep birlikte görünmeli
    assert "Tescom İzmir GES" in body
    assert "plant-child" in body
    assert ">Mekanik<" in body
    assert ">Uretim<" in body


async def test_map_uses_new_amber_marker(client):
    await do_login(client)
    response = await client.get("/ui/harita")
    assert response.status_code == 200
    assert "leaflet" in response.text.lower()
    assert "#f2b544" in response.text  # yeni amber palet


async def test_inverter_detail_page(client, engine):
    await do_login(client)
    async with session_scope(engine) as session:
        plant = (await session.scalars(select(Plant))).one()
        session.add(Inverter(
            plant_id=plant.id, vendor_device_id="99",
            model="Test Inv", ac_capacity_kw=250.0,
            last_seen_at=datetime.now(tz=UTC), last_power_kw=142.3, last_temp_c=48.0,
            last_error_code="0", last_status="AKTIF",
        ))
    async with session_scope(engine) as session:
        plant = (await session.scalars(select(Plant))).one()

    response = await client.get(f"/ui/plants/{plant.id}/inverters/99")
    assert response.status_code == 200
    assert "İnvertör 99" in response.text
    assert "Üretiyor" in response.text
    assert "142" in response.text
    assert "48" in response.text
    # iki grafik + KPI şeridi
    assert response.text.count("<svg") >= 2


async def test_inverter_detail_404_for_unknown_device(client, engine):
    await do_login(client)
    async with session_scope(engine) as session:
        plant = (await session.scalars(select(Plant))).one()
    response = await client.get(f"/ui/plants/{plant.id}/inverters/yok")
    assert response.status_code == 404


async def test_inverter_detail_scopes_to_plant_not_siblings(client, engine):
    """Aynı `slave_id` iki child'da varsa detay her zaman URL'deki tesise ait olmalı."""
    await do_login(client)
    async with session_scope(engine) as session:
        admin = (await session.scalars(select(User))).first()
        p1 = Plant(
            owner_id=admin.id, name="X GES · Mekanik",
            vendor="tescom", vendor_plant_id="x-mekanik",
        )
        p2 = Plant(
            owner_id=admin.id, name="X GES · Uretim",
            vendor="tescom", vendor_plant_id="x-uretim",
        )
        session.add_all([p1, p2])
        await session.flush()
        session.add_all([
            Inverter(
                plant_id=p1.id, vendor_device_id="1",
                model="Mekanik-1", ac_capacity_kw=100.0,
                last_seen_at=datetime.now(tz=UTC), last_power_kw=42.0,
                last_status="AKTIF", last_error_code="0",
            ),
            Inverter(
                plant_id=p2.id, vendor_device_id="1",
                model="Uretim-1", ac_capacity_kw=100.0,
                last_seen_at=datetime.now(tz=UTC), last_power_kw=88.0,
                last_status="AKTIF", last_error_code="0",
            ),
        ])
    async with session_scope(engine) as session:
        p1 = (await session.scalars(select(Plant).where(Plant.name == "X GES · Mekanik"))).one()
        p2 = (await session.scalars(select(Plant).where(Plant.name == "X GES · Uretim"))).one()

    r1 = await client.get(f"/ui/plants/{p1.id}/inverters/1")
    r2 = await client.get(f"/ui/plants/{p2.id}/inverters/1")
    assert "Mekanik-1" in r1.text and "Uretim-1" not in r1.text
    assert "Uretim-1" in r2.text and "Mekanik-1" not in r2.text


async def test_anomaly_detail_page_and_evidence(client, engine):
    await do_login(client)
    async with session_scope(engine) as session:
        plant = (await session.scalars(select(Plant))).one()
        session.add(AnomalyEvent(
            plant_id=plant.id, kind="shading", severity="critical",
            deviation_pct=-18.4, started_at=NOW - timedelta(hours=2),
            status="open",
            evidence={"band_hours_utc": "[7, 8, 9]", "band_median_pct": "-18.4"},
        ))
    async with session_scope(engine) as session:
        event = (await session.scalars(select(AnomalyEvent))).one()

    response = await client.get(f"/ui/anomalies/{event.id}")
    assert response.status_code == 200
    assert "Gölgelenme" in response.text
    assert "Kritik" in response.text
    assert "-18.4" in response.text
    # Evidence tablosu okunabilir başlıkla geldi
    assert "Bant saatleri (UTC)" in response.text
    assert "Bant medyan" in response.text


async def test_anomaly_detail_action_resolves_open_event(client, engine):
    await do_login(client)
    async with session_scope(engine) as session:
        plant = (await session.scalars(select(Plant))).one()
        session.add(AnomalyEvent(
            plant_id=plant.id, kind="soiling", severity="warning",
            deviation_pct=-6.5, started_at=NOW - timedelta(hours=1),
            status="open", evidence={},
        ))
    async with session_scope(engine) as session:
        event = (await session.scalars(select(AnomalyEvent))).one()

    action = await client.post(
        f"/ui/anomalies/{event.id}/status",
        data={"status": "resolved", "back": f"/ui/anomalies/{event.id}"},
    )
    assert action.status_code == 303

    async with session_scope(engine) as session:
        updated = (await session.scalars(select(AnomalyEvent))).one()
        assert updated.status == "resolved"


async def test_anomaly_detail_404(client):
    await do_login(client)
    import uuid as _uuid
    response = await client.get(f"/ui/anomalies/{_uuid.uuid4()}")
    assert response.status_code == 404


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
