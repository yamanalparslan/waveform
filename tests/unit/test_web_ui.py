"""Sunucu taraflı arayüz (/ui) testleri: oturum çerezi + sayfa içerikleri."""

import re
from datetime import UTC, date, datetime, timedelta, timezone

import httpx
import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import create_async_engine

from luminmind.api.main import create_app
from luminmind.config import Settings
from luminmind.core.aggregate import RawSample
from luminmind.core.db import session_scope
from luminmind.core.models import AnomalyEvent, Base, Inverter, Plant, Site, User
from luminmind.core.security import hash_password
from luminmind.scripts.seed import seed
from luminmind.web.charts import Series, line_chart, price_plan_chart
from luminmind.web.theme import CHART_CHARGE, CHART_DISCHARGE, PALETTE
from luminmind.workers.tasks.arbitrage import run_arbitrage

SETTINGS = Settings(jwt_secret="test-secret", lm_use_mock_prices=True)
TRT = timezone(timedelta(hours=3))
# Dakikaya yuvarlanır, saate değil: saat başına yuvarlamak NOW'ı gerçek zamandan
# 59 dakikaya kadar geriye atıyordu ve invertör sağlık eşiğini (STALE_AFTER =
# 30 dk) aşınca test saat başının ikinci yarısında rastgele düşüyordu.
NOW = datetime.now(tz=UTC).replace(second=0, microsecond=0)


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


async def test_login_page_does_not_advertise_credentials(client):
    """Panel internete açılabiliyor; demo şifresini ekranda tutmak davetiye olurdu."""
    response = await client.get("/ui/login")
    assert "Demo giriş" not in response.text


async def test_palette_block_is_not_html_escaped(client):
    """`<style>` bir raw text öğesi: tarayıcı içindeki `&#34;` kaçışını çözmez.

    Jinja autoescape paleti kaçırırsa `--sans:"SF Pro Text"` bloğa
    `--sans:&#34;SF Pro Text&#34;` olarak girer; CSS ayrıştırıcısı `&#34;`
    içindeki `;`yi bildirim sonu sayar ve font yığını orada kesilir. Sayfa hata
    vermez, sessizce tarayıcının varsayılan yüzüyle (Windows'ta Times New Roman)
    açılır — bu yüzden test ediliyor.
    """
    response = await client.get("/ui/login")
    style = response.text.split("<style>", 1)[1].split("</style>", 1)[0]
    assert "&#34;" not in style and "&quot;" not in style and "&amp;" not in style
    assert '"SF Pro Text"' in style  # tırnaklar olduğu gibi geçmiş
    # Yığın kesilmemiş: son öğe jenerik aile olarak yerinde.
    assert "sans-serif;" in style
    assert "admin@luminmind.local" not in response.text


async def test_stylesheet_is_served_from_static_mount(client):
    response = await client.get("/static/app.css")
    assert response.status_code == 200
    assert "text/css" in response.headers["content-type"]
    assert ".money-hero" in response.text


async def test_pages_link_the_stylesheet_and_inline_the_palette(client):
    """Palet satır içi, yerleşim statik dosyada: ikisi birden gelmezse sayfa çıplak."""
    await do_login(client)
    response = await client.get("/ui")
    assert '<link rel="stylesheet" href="/static/app.css">' in response.text
    assert ":root{" in response.text
    assert f"--blue:{PALETTE['blue']};" in response.text


async def test_login_wrong_password_shows_error(client):
    response = await client.post(
        "/ui/login", data={"email": "admin@luminmind.local", "password": "x"}
    )
    assert response.status_code == 401
    assert "hatalı" in response.text


async def test_portfolio_shows_plant_card(client):
    await do_login(client)
    response = await client.get("/ui")
    assert response.status_code == 200
    assert "Konya GES" in response.text
    assert "Anlık güç" in response.text
    assert "520" in response.text  # son ölçüm
    assert "Bekleyen iş" in response.text
    assert "Aksiyon planı" in response.text


async def test_portfolio_shows_money_and_headline(client):
    """Kullanıcı odaklı panel: kWh yerine önce ₺, tepede tek cümlelik durum."""
    await do_login(client)
    response = await client.get("/ui")
    assert "Bugünkü kazanç" in response.text
    assert "₺" in response.text
    # FakeTsSource iki nokta döner → 0,25 sa × (480+520) = 250 kWh × 2,9 ₺ ≈ 725 ₺
    assert "725 ₺" in response.text
    # Beklenen (twin) 560 kW → 140 kWh; gerçek 250 kWh, oran > %95
    assert "Her şey yolunda" in response.text


async def test_portfolio_leads_with_recoverable_income(client):
    """DeepSolar mantığının merkezi: ekranın en üst sayısı kurtarılabilir gelir."""
    await do_login(client)
    response = await client.get("/ui")
    assert "Potansiyel kurtarılabilir yıllık gelir" in response.text
    assert "stacked-bar" in response.text  # öncelik kırılım barı
    assert "Günlük üretim" in response.text


async def test_plant_detail_renders_svg_chart(client, engine):
    await do_login(client)
    async with session_scope(engine) as session:
        plant = (await session.scalars(select(Plant))).one()
    response = await client.get(f"/ui/plants/{plant.id}?date={NOW.date().isoformat()}")
    assert response.status_code == 200
    assert "<svg" in response.text
    assert "Ürettiğiniz" in response.text and "Olması gereken" in response.text
    assert "Performans oranı" in response.text
    assert "Potansiyel kurtarılabilir yıllık gelir" in response.text
    assert "Cihaz × saat performansı" in response.text  # ısı haritası


async def test_plant_tabs_split_status_from_devices(client, engine):
    """Sekmeler: DURUM ve İNVERTÖRLER. Boş sekme (dize/ışınım) konmaz."""
    await do_login(client)
    async with session_scope(engine) as session:
        plant = (await session.scalars(select(Plant))).one()

    status = await client.get(f"/ui/plants/{plant.id}")
    assert "Durum" in status.text and "İnvertörler" in status.text
    assert "Dizeler" not in status.text and "Işınım" not in status.text

    devices = await client.get(f"/ui/plants/{plant.id}?tab=inverterler")
    assert devices.status_code == 200
    assert "mock-plant-1-inv-01" in devices.text
    assert "Hata / durum" in devices.text
    # Durum sekmesinin içeriği ikinci sekmede tekrar etmez
    assert "Potansiyel kurtarılabilir yıllık gelir" not in devices.text


# ------------------------------ Saha hiyerarşisi ------------------------------
# Göçten sonra ölçümler `Site.series_key` altında duruyor; tesisin
# `vendor_plant_id`'si altında hiç veri yok. Sayfalar yanlış anahtarı sorarsa
# hata vermez, sadece boş açılırlar — aşağıdaki testlerin varlık sebebi bu.

URETIM = "tescom-izmir-uretim"
MEKANIK = "tescom-izmir-mekanik"


class SiteAwareTsSource:
    """Yalnızca saha anahtarlarına yanıt verir; tesis anahtarı boş döner."""

    POWER = {URETIM: (300.0, 320.0), MEKANIK: (150.0, 170.0)}
    EXPECTED = {URETIM: 340.0, MEKANIK: 170.0}

    async def query_plant_series(self, vendor_plant_id, metric, start, stop, resolution="15m"):
        values = self.POWER.get(vendor_plant_id)
        if values is None:
            return []
        return [(NOW - timedelta(minutes=15), values[0]), (NOW, values[1])]

    async def query_device_series(self, vendor_plant_id, vendor_device_id, metric, start, stop):
        if vendor_plant_id not in self.POWER:
            return []
        base = 60.0 if metric == "ac_power_kw" else 44.0
        return [(NOW - timedelta(minutes=15), base), (NOW, base + 10)]

    async def query_raw_window(self, start, stop):
        return []

    async def query_twin_window(self, start, stop):
        return {key: {NOW: value} for key, value in self.EXPECTED.items()}


@pytest.fixture
async def site_engine():
    """Tek tesis, iki fabrika — Tescom UPS İzmir yapısının aynısı."""
    engine = create_async_engine("sqlite+aiosqlite://")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    async with session_scope(engine) as session:
        user = User(
            email="admin@luminmind.local",
            hashed_password=hash_password("admin"),
            role="admin",
        )
        session.add(user)
        await session.flush()
        plant = Plant(
            owner_id=user.id,
            name="Tescom UPS İzmir",
            vendor="tescom",
            vendor_plant_id="tescom-izmir",
            latitude=38.53,
            longitude=27.14,
            dc_capacity_kwp=650.0,
            ac_capacity_kw=540.0,
            feed_in_tariff_try_kwh=2.9,
        )
        session.add(plant)
        await session.flush()
        sites = [
            Site(
                plant_id=plant.id, name="Üretim Fabrikası", code="uretim",
                series_key=URETIM, dc_capacity_kwp=400.0, ac_capacity_kw=333.0,
                display_order=1,
            ),
            Site(
                plant_id=plant.id, name="Mekanik Fabrika", code="mekanik",
                series_key=MEKANIK, dc_capacity_kwp=250.0, display_order=2,
            ),
        ]
        session.add_all(sites)
        await session.flush()
        for site, count in ((sites[0], 2), (sites[1], 1)):
            for index in range(1, count + 1):
                session.add(
                    Inverter(
                        plant_id=plant.id,
                        site_id=site.id,
                        vendor_device_id=str(index),
                        model="Tescom",
                        last_seen_at=NOW,
                        last_power_kw=60.0,
                        last_temp_c=44.0,
                        last_status="AKTIF",
                        last_error_code="0",
                    )
                )
    yield engine
    await engine.dispose()


@pytest.fixture
async def site_client(site_engine):
    app = create_app(settings=SETTINGS, engine=site_engine, influx=SiteAwareTsSource())
    async with app.router.lifespan_context(app):
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
            yield c


async def _tescom_plant(engine):
    async with session_scope(engine) as session:
        return (await session.scalars(select(Plant))).one()


async def test_plant_page_reads_from_site_series_not_the_plant_key(site_client, site_engine):
    """Kilit test: tesis toplamı sahaların toplamı olmalı, 0 değil.

    Sayfa `vendor_plant_id` ile sorarsa `SiteAwareTsSource` boş döner ve tüm
    rakamlar sıfırlanır — göçün sessiz kırılması tam olarak buydu.
    """
    await do_login(site_client)
    plant = await _tescom_plant(site_engine)
    response = await site_client.get(f"/ui/plants/{plant.id}")
    assert response.status_code == 200
    # Üretim 0,25×(300+320)=155 kWh, Mekanik 0,25×(150+170)=80 kWh → 235 kWh
    assert "235" in response.text
    assert "Üretim Fabrikası" in response.text and "Mekanik Fabrika" in response.text


async def test_portfolio_rolls_the_two_factories_into_one_plant(site_client):
    await do_login(site_client)
    response = await site_client.get("/ui")
    assert response.status_code == 200
    assert "Tescom UPS İzmir" in response.text
    assert "235" in response.text  # tesis toplamı
    assert "155" in response.text and "80" in response.text  # saha kırılımı
    # Kurulu güç sahaların toplamından gelir
    assert "650" in response.text


async def test_sidebar_lists_factories_under_the_plant(site_client, site_engine):
    await do_login(site_client)
    plant = await _tescom_plant(site_engine)
    response = await site_client.get(f"/ui/plants/{plant.id}")
    assert f"/ui/plants/{plant.id}/sites/uretim" in response.text
    assert f"/ui/plants/{plant.id}/sites/mekanik" in response.text


async def test_site_page_narrows_to_one_factory(site_client, site_engine):
    await do_login(site_client)
    plant = await _tescom_plant(site_engine)
    response = await site_client.get(f"/ui/plants/{plant.id}/sites/uretim")
    assert response.status_code == 200
    assert "Üretim Fabrikası" in response.text
    assert "155" in response.text  # yalnız bu fabrikanın enerjisi
    # Kapsam daraldığı için fabrika karşılaştırma tablosu gösterilmez
    assert "Mekanik Fabrika" not in response.text.split("</aside>")[1]


def test_today_window_stops_at_the_last_completed_grid_slot():
    """İkiz günün tamamını tahmin eder, gerçek üretim yalnızca şu ana kadar vardır.

    İki seri aynı pencereden okunmazsa öğlen saatlerinde performans oranı yarıya
    düşer ve ana sayfa sebepsiz alarm verir. Tam `now`'a kırpmak da yetmez:
    devam eden 15 dakikalık pencerede ikizin noktası var, ölçüm yok.
    """
    from luminmind.web.routes import _elapsed_window, _trt_day_window

    now = datetime(2026, 7, 29, 8, 47, tzinfo=UTC)  # TRT 11:47
    today = now.astimezone(TRT).date()
    _, stop = _elapsed_window(today, now)
    assert stop == datetime(2026, 7, 29, 8, 45, tzinfo=UTC)  # son tamamlanmış slot
    assert stop < now
    assert stop < _trt_day_window(today)[1]


def test_past_days_keep_the_whole_window():
    from luminmind.web.routes import _elapsed_window, _trt_day_window

    now = datetime(2026, 7, 29, 8, 47, tzinfo=UTC)
    past = now.astimezone(TRT).date() - timedelta(days=3)
    assert _elapsed_window(past, now) == _trt_day_window(past)


def test_window_is_empty_before_the_first_completed_slot():
    """Gün yeni başladıysa karşılaştırılacak tamamlanmış pencere yoktur."""
    from luminmind.web.routes import _elapsed_window

    just_after_midnight = datetime(2026, 7, 28, 21, 7, tzinfo=UTC)  # TRT 00:07
    day = just_after_midnight.astimezone(TRT).date()
    start, stop = _elapsed_window(day, just_after_midnight)
    assert start == stop


async def test_unknown_site_code_is_404(site_client, site_engine):
    await do_login(site_client)
    plant = await _tescom_plant(site_engine)
    response = await site_client.get(f"/ui/plants/{plant.id}/sites/yok")
    assert response.status_code == 404


async def test_insights_page_lists_findings_by_recoverable_income(site_client, site_engine):
    await do_login(site_client)
    plant = await _tescom_plant(site_engine)
    async with session_scope(site_engine) as session:
        site = (await session.scalars(select(Site).where(Site.code == "uretim"))).one()
        session.add(
            AnomalyEvent(
                plant_id=plant.id,
                site_id=site.id,
                kind="soiling",
                severity="warning",
                deviation_pct=-9.0,
                started_at=NOW - timedelta(hours=3),
                status="open",
                evidence={},
            )
        )
    response = await site_client.get(f"/ui/plants/{plant.id}/insights")
    assert response.status_code == 200
    assert "Paneller kirlenmiş görünüyor" in response.text
    assert "Uzun vadeli" in response.text
    assert "Kurtarılabilir" in response.text
    assert "Üretim Fabrikası" in response.text


async def test_insights_page_can_be_narrowed_to_a_site(site_client, site_engine):
    await do_login(site_client)
    plant = await _tescom_plant(site_engine)
    response = await site_client.get(f"/ui/plants/{plant.id}/insights?site=mekanik")
    assert response.status_code == 200
    assert "Mekanik Fabrika" in response.text


async def test_devices_page_charts_every_inverter(site_client, site_engine):
    await do_login(site_client)
    plant = await _tescom_plant(site_engine)
    response = await site_client.get(f"/ui/plants/{plant.id}/devices")
    assert response.status_code == 200
    assert "Cihaz karşılaştırma" in response.text
    assert "3 cihaz" in response.text  # Üretim 2 + Mekanik 1
    # Aynı numaralı iki cihaz varsa etiketler fabrika adıyla ayrışmalı
    assert "Üretim Fabrikası · 1" in response.text
    assert "Mekanik Fabrika · 1" in response.text


async def test_device_chart_baseline_is_per_device_not_the_whole_plant(
    site_client, site_engine
):
    """Saha toplamını taban almak grafiği okunamaz yapıyordu.

    650 kWp'lik iki fabrikanın toplam beklentisi ~500 kW'a çıkarken tek cihaz
    ~140 kW'da kalıyor; eksen beklentiye göre ölçekleniyor, cihaz eğrileri dibe
    sıkışıyor ve sağlıklı bir cihaz üç kat büyük bir çizginin yanında arızalı
    görünüyordu.
    """
    await do_login(site_client)
    plant = await _tescom_plant(site_engine)
    response = await site_client.get(f"/ui/plants/{plant.id}/devices")
    assert "cihaz payı" in response.text
    assert "Olması gereken (saha)" not in response.text
    assert "stroke-dasharray" in response.text  # referans kesik çizgi

    # Eksen artık cihaz ölçeğinde: iki fabrikanın toplam beklentisi (510 kW)
    # değil, en büyük cihaz payı (Üretim 340/2 = 170 kW) sınırı belirliyor.
    ticks = [float(t) for t in re.findall(r'class="tick">([\d.]+)</text>', response.text)]
    plant_wide = sum(SiteAwareTsSource.EXPECTED.values())
    assert ticks, "y ekseni etiketi yok"
    assert max(ticks) < plant_wide / 2


async def test_single_site_device_chart_labels_the_share_plainly(site_client, site_engine):
    await do_login(site_client)
    plant = await _tescom_plant(site_engine)
    response = await site_client.get(f"/ui/plants/{plant.id}/devices?site=mekanik")
    # Tek fabrika kapsamında fabrika adını tekrar etmeye gerek yok
    assert "Cihaz payı (beklenen)" in response.text


async def test_device_page_needs_the_site_when_the_number_repeats(site_client, site_engine):
    """Her iki fabrikada "1 nolu" cihaz var; sahasız istek tahmin etmemeli.

    Eskiden `one_or_none()` çağrısı `MultipleResultsFound` fırlatıp 500 veriyordu;
    tahmin etmek daha da kötüsü olurdu — yanlış fabrikanın cihazı gösterilir.
    """
    await do_login(site_client)
    plant = await _tescom_plant(site_engine)

    ambiguous = await site_client.get(f"/ui/plants/{plant.id}/inverters/1")
    assert ambiguous.status_code == 404

    for code, name in (("uretim", "Üretim Fabrikası"), ("mekanik", "Mekanik Fabrika")):
        scoped = await site_client.get(f"/ui/plants/{plant.id}/sites/{code}/inverters/1")
        assert scoped.status_code == 200, code
        assert name in scoped.text
        assert "1 nolu invertör" in scoped.text


async def test_unique_device_number_still_resolves_without_a_site(site_client, site_engine):
    """Mekanik'te yalnız 2 numara yok; Üretim'de var → tesis genelinde tekil."""
    await do_login(site_client)
    plant = await _tescom_plant(site_engine)
    response = await site_client.get(f"/ui/plants/{plant.id}/inverters/2")
    assert response.status_code == 200
    assert "Üretim Fabrikası" in response.text


async def test_device_page_reads_the_site_series(site_client, site_engine):
    """Tesis anahtarıyla sormak boş grafik döndürürdü."""
    await do_login(site_client)
    plant = await _tescom_plant(site_engine)
    response = await site_client.get(f"/ui/plants/{plant.id}/sites/uretim/inverters/1")
    assert "Güç" in response.text
    assert "chart-empty" not in response.text  # "Veri yok" yerine gerçek eğri


async def test_inverter_links_carry_the_site_code(site_client, site_engine):
    await do_login(site_client)
    plant = await _tescom_plant(site_engine)
    response = await site_client.get(f"/ui/plants/{plant.id}?tab=inverterler")
    assert f"/ui/plants/{plant.id}/sites/uretim/inverters/1" in response.text
    assert f"/ui/plants/{plant.id}/sites/mekanik/inverters/1" in response.text


async def test_heatmap_page_renders_a_grid(site_client, site_engine):
    await do_login(site_client)
    plant = await _tescom_plant(site_engine)
    response = await site_client.get(f"/ui/plants/{plant.id}/heatmap")
    assert response.status_code == 200
    assert "chart heat" in response.text
    assert "veri yok" in response.text  # gece hücreleri nötr
    assert "3 cihaz" in response.text


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
    # Teknik etiket değil, kullanıcı diliyle ne oldu / ne yapmalı
    assert "Paneller gölgede kalıyor" in page.text
    assert "Acil" in page.text
    assert "kontrol ettirin" in page.text
    assert "Günde yaklaşık" in page.text  # parasal etki
    assert "İlgileniyorum" in page.text and "Halloldu" in page.text

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
    assert "Geçmiş" in response.text
    assert "Dönem kazancınız" in response.text
    assert "Kaçırdığınız kazanç" in response.text
    assert "CSV indir" in response.text


async def test_reports_csv_download(client):
    await do_login(client)
    response = await client.get("/ui/raporlar/indir?days=7")
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/csv")
    assert "attachment" in response.headers["content-disposition"]
    # başlık satırı — kazanç sütunu dahil
    assert "tarih,tesis,kazanc_try,enerji_kwh" in response.text


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
    assert "Yeni santral ekle" in page.text
    assert "haritada tıklayarak" in page.text
    assert "Şebekeye satış fiyatı" in page.text  # tarife alanı


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
        "feed_in_tariff_try_kwh": "3.15",
    })
    assert response.status_code == 303
    assert response.headers["location"].startswith("/ui/plants/")

    from sqlalchemy import select as _sel
    async with session_scope(engine) as session:
        plant = (await session.scalars(_sel(Plant).where(Plant.name == "İzmir GES 2"))).one()
        assert plant.dc_capacity_kwp == 500.0
        assert plant.feed_in_tariff_try_kwh == 3.15


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
        # Panel dışa açılabildiği için asgari şifre uzunluğu 8 karakter (NIST tabanı)
        data={"email": "yeni@luminmind.local", "password": "GucluSifre123", "role": "viewer"},
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


async def test_map_marker_colour_comes_from_the_palette(client):
    await do_login(client)
    response = await client.get("/ui/harita")
    assert response.status_code == 200
    assert "leaflet" in response.text.lower()
    # Renk şablonda sabit yazılmaz; palet değişince harita da değişmeli
    assert PALETTE["blue"] in response.text


async def test_inverter_detail_page(client, engine):
    await do_login(client)
    async with session_scope(engine) as session:
        plant = (await session.scalars(select(Plant))).one()
        session.add(Inverter(
            plant_id=plant.id, vendor_device_id="99",
            model="Test Inv", ac_capacity_kw=250.0,
            last_seen_at=NOW, last_power_kw=142.3, last_temp_c=48.0,
            last_error_code="0", last_status="AKTIF",
        ))
    async with session_scope(engine) as session:
        plant = (await session.scalars(select(Plant))).one()

    response = await client.get(f"/ui/plants/{plant.id}/inverters/99")
    assert response.status_code == 200
    assert "99 nolu invertör" in response.text
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
    assert "Paneller gölgede kalıyor" in response.text
    assert "Acil" in response.text
    assert "Ne yapmalısınız" in response.text
    assert "Size maliyeti" in response.text
    # Teknik kanıt artık katlanabilir kutunun içinde, varsayılan görünümde değil
    assert "Bunu neden tespit ettik?" in response.text
    assert "<details" in response.text
    assert "Bant saatleri (UTC)" in response.text


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


def test_line_chart_renders_one_smooth_path_per_series():
    points = [(NOW, 100.0), (NOW + timedelta(minutes=15), 200.0)]
    svg = line_chart([Series("Gerçek", "#3fa9f5", points)], TRT, unit="kW")
    assert svg.count("<path") == 1
    assert " C" in svg  # kübik Bézier: köşeler değil eğri
    assert "Gerçek" in svg and "kW" in svg


def test_price_plan_chart_colors_actions():
    prices = [(NOW + timedelta(hours=h), 1500.0 + h) for h in range(3)]
    actions = {prices[0][0]: ("charge", 100.0), prices[2][0]: ("discharge", 100.0)}
    svg = price_plan_chart(prices, actions, TRT)
    assert CHART_CHARGE in svg and CHART_DISCHARGE in svg  # şarj + deşarj renkleri
    assert "Şarj" in svg and "Deşarj" in svg


# ------------------------------ Dışa açık mod ------------------------------


async def test_security_headers_are_set(client):
    response = await client.get("/ui/login")
    headers = response.headers
    assert headers["x-content-type-options"] == "nosniff"
    assert headers["x-frame-options"] == "DENY"
    assert "frame-ancestors 'none'" in headers["content-security-policy"]
    assert "script-src 'self'" in headers["content-security-policy"]
    # Yerel HTTP modunda HSTS gönderilmez (tarayıcıyı https'e kilitlerdi)
    assert "strict-transport-security" not in headers


async def test_public_mode_sends_hsts_and_secure_cookie(engine):
    """LM_PUBLIC_URL dolduğunda sertleştirme kendiliğinden devreye girmeli."""
    public = Settings(
        lm_use_mock_vendors=True,
        lm_public_url="https://ges.example.com",
        lm_allowed_hosts="test",
    )
    app = create_app(settings=public, engine=engine, influx=FakeTsSource())
    async with app.router.lifespan_context(app):
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
            response = await c.get("/ui/login")
            assert "max-age=31536000" in response.headers["strict-transport-security"]

            login = await c.post(
                "/ui/login", data={"email": "admin@luminmind.local", "password": "admin"}
            )
            cookie = login.headers["set-cookie"]
            assert "Secure" in cookie
            assert "HttpOnly" in cookie
            assert "SameSite=lax" in cookie


async def test_unknown_host_is_rejected_when_allowlist_set(engine):
    public = Settings(
        lm_use_mock_vendors=True,
        lm_public_url="https://ges.example.com",
        lm_allowed_hosts="test",
    )
    app = create_app(settings=public, engine=engine, influx=FakeTsSource())
    async with app.router.lifespan_context(app):
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://saldirgan") as c:
            assert (await c.get("/ui/login")).status_code == 400


async def test_login_is_rate_limited(client):
    for _ in range(8):
        failed = await client.post(
            "/ui/login", data={"email": "admin@luminmind.local", "password": "yanlis"}
        )
        assert failed.status_code == 401

    blocked = await client.post(
        "/ui/login", data={"email": "admin@luminmind.local", "password": "yanlis"}
    )
    assert blocked.status_code == 429
    assert "Çok fazla hatalı deneme" in blocked.text

    # Sınıra takılıyken doğru parola da kabul edilmez
    correct = await client.post(
        "/ui/login", data={"email": "admin@luminmind.local", "password": "admin"}
    )
    assert correct.status_code == 429


async def test_successful_login_clears_rate_limit(client):
    for _ in range(3):
        await client.post(
            "/ui/login", data={"email": "admin@luminmind.local", "password": "yanlis"}
        )
    await do_login(client)
    for _ in range(6):
        response = await client.post(
            "/ui/login", data={"email": "admin@luminmind.local", "password": "yanlis"}
        )
        assert response.status_code == 401  # sayaç sıfırlandı, hâlâ sınırın altında


async def test_logout_clears_cookie(client):
    await do_login(client)
    response = await client.post("/ui/logout")
    assert response.status_code == 303
    assert 'lm_session=""' in response.headers["set-cookie"]


async def test_admin_can_change_a_password(client, engine):
    """Seed hesabının 'admin' parolası değiştirilebilmeli — dışa açmanın ön koşulu."""
    await do_login(client)
    async with session_scope(engine) as session:
        target = (await session.scalars(select(User))).one()

    response = await client.post(
        f"/ui/kullanicilar/{target.id}/sifre", data={"password": "YeniGucluSifre1"}
    )
    assert response.status_code == 303

    # Eski parola artık geçmemeli, yenisi geçmeli
    await client.post("/ui/logout")
    old = await client.post(
        "/ui/login", data={"email": "admin@luminmind.local", "password": "admin"}
    )
    assert old.status_code == 401
    await do_login(client, password="YeniGucluSifre1")


async def test_short_passwords_are_rejected(client, engine):
    await do_login(client)
    async with session_scope(engine) as session:
        target = (await session.scalars(select(User))).one()

    response = await client.post(
        f"/ui/kullanicilar/{target.id}/sifre", data={"password": "kisa"}
    )
    assert response.status_code == 303
    assert "en+az" in response.headers["location"] or "az" in response.headers["location"]
    # Parola değişmemiş olmalı
    await client.post("/ui/logout")
    await do_login(client, password="admin")


async def test_new_user_requires_strong_password(client):
    await do_login(client)
    response = await client.post(
        "/ui/kullanicilar/yeni",
        data={"email": "zayif@luminmind.local", "password": "1234", "role": "viewer"},
    )
    assert response.status_code == 303
    assert "error=" in response.headers["location"]


async def test_last_admin_cannot_be_deleted(client, engine):
    await do_login(client)
    async with session_scope(engine) as session:
        admin = (await session.scalars(select(User))).one()

    # Kendini silme engeli
    response = await client.post(f"/ui/kullanicilar/{admin.id}/sil")
    assert "error=" in response.headers["location"]
    async with session_scope(engine) as session:
        assert len((await session.scalars(select(User))).all()) == 1


async def test_user_with_plants_cannot_be_deleted(client, engine):
    """Santral sahibi silinirse tesisler yetim kalır; önce sahiplik devredilmeli."""
    await do_login(client)
    await client.post(
        "/ui/kullanicilar/yeni",
        data={"email": "ikinci@luminmind.local", "password": "GucluSifre123", "role": "admin"},
    )
    async with session_scope(engine) as session:
        owner = (
            await session.scalars(select(User).where(User.email == "admin@luminmind.local"))
        ).one()
        second = (
            await session.scalars(select(User).where(User.email == "ikinci@luminmind.local"))
        ).one()
        owner_id, second_id = owner.id, second.id

    # İkinci hesapla girip santral sahibi olan ilk hesabı silmeye çalış
    await client.post("/ui/logout")
    await do_login(client, email="ikinci@luminmind.local", password="GucluSifre123")
    response = await client.post(f"/ui/kullanicilar/{owner_id}/sil")
    assert "santral" in response.headers["location"] or "error=" in response.headers["location"]

    async with session_scope(engine) as session:
        assert await session.get(User, owner_id) is not None
        assert await session.get(User, second_id) is not None


async def test_user_without_plants_can_be_deleted(client, engine):
    await do_login(client)
    await client.post(
        "/ui/kullanicilar/yeni",
        data={"email": "gecici@luminmind.local", "password": "GucluSifre123", "role": "viewer"},
    )
    async with session_scope(engine) as session:
        temp = (
            await session.scalars(select(User).where(User.email == "gecici@luminmind.local"))
        ).one()
        temp_id = temp.id

    response = await client.post(f"/ui/kullanicilar/{temp_id}/sil")
    assert response.status_code == 303
    async with session_scope(engine) as session:
        assert await session.get(User, temp_id) is None
