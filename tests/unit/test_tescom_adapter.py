from datetime import UTC, datetime
from zoneinfo import ZoneInfo

import httpx
import respx

from luminmind.adapters import TescomAdapter
from luminmind.adapters.normalize import normalize_tescom_devices
from luminmind.adapters.tescom import FactorySite
from luminmind.config import Settings
from luminmind.core.schemas import Vendor

BASE = "http://tescom.local:8503"
TRT = ZoneInfo("Europe/Istanbul")
# 2026-07-21 10:39 TRT = 07:39 UTC
SINCE = datetime(2026, 7, 21, 7, 0, tzinfo=UTC)

URETIM = "tescom-izmir-uretim"
MEKANIK = "tescom-izmir-mekanik"
FACTORIES = {
    "uretim": FactorySite(plant_id=URETIM, name="Üretim Fabrikası", dc_capacity_kwp=400.0),
    "mekanik": FactorySite(plant_id=MEKANIK, name="Mekanik Fabrika", dc_capacity_kwp=250.0),
}
PLANT_IDS = {"uretim": URETIM, "mekanik": MEKANIK}


def make_adapter(factories: dict[str, FactorySite] | None = FACTORIES) -> TescomAdapter:
    return TescomAdapter(
        base_url=BASE,
        api_key="secret-key",
        dc_capacity_kwp=250.0,
        backoff_base_s=0.0,
        factories=factories,
    )


# ------------------------------ Normalizasyon ------------------------------


def test_normalize_tescom_maps_fields_and_tz(load_fixture):
    payload = load_fixture("tescom/devices.json")
    points = normalize_tescom_devices("tescom-izmir", payload, TRT, PLANT_IDS)

    assert len(points) == 3
    first = points[0]
    assert first.vendor == Vendor.TESCOM
    assert first.vendor_plant_id == MEKANIK
    assert first.vendor_device_id == "1"
    assert first.ac_power_kw == 124.73
    assert first.dc_voltage_v == 801.9
    assert first.dc_current_a == 90.33
    assert first.temp_c == 52.6
    # yerel 10:39:27 TRT → 07:39:27 UTC
    assert first.ts == datetime(2026, 7, 21, 7, 39, 27, 661885, tzinfo=UTC)


def test_same_slave_id_in_two_factories_does_not_collide(load_fixture):
    """Faz 0'ın kilit testi.

    API'de `slave_id` yalnızca fabrika içinde tekil: hem `mekanik` hem `uretim`
    fabrikasında 1 numaralı cihaz var. Kimlik yalnız `slave_id`'den kurulursa
    ikisi aynı Influx serisine yazılır ve biri diğerinin üzerine yazarak
    mekanik fabrikanın verisini yok eder.
    """
    points = normalize_tescom_devices(
        "tescom-izmir", load_fixture("tescom/devices.json"), TRT, PLANT_IDS
    )
    # Aynı cihaz numarası, farklı saha → çakışmayan iki seri
    ones = [p for p in points if p.vendor_device_id == "1"]
    assert len(ones) == 2
    assert {p.vendor_plant_id for p in ones} == {URETIM, MEKANIK}

    # Zaman serisi kimliği (saha, cihaz) çifti olarak tekil olmalı
    keys = {(p.vendor_plant_id, p.vendor_device_id) for p in points}
    assert len(keys) == len(points) == 3


def test_daily_energy_is_mapped(load_fixture):
    """`gunluk_uretim_kwh` eşlenmezse saatlik/günlük enerji agregatları boş kalır."""
    points = normalize_tescom_devices(
        "tescom-izmir", load_fixture("tescom/devices.json"), TRT, PLANT_IDS
    )
    energies = {(p.vendor_plant_id, p.vendor_device_id): p.energy_total_kwh for p in points}
    assert energies[(MEKANIK, "1")] == 15.0
    assert energies[(URETIM, "1")] == 22.0
    assert energies[(URETIM, "2")] == 12.0
    # Influx'a yazılacak alanlar arasında olmalı
    assert "energy_total_kwh" in points[0].measured_fields()


def test_missing_factory_id_falls_back_to_default():
    """Eski/kısmi yanıtlar (fabrika_id yok) varsayılan saha altında toplanır."""
    payload = [{"slave_id": 3, "zaman": "2026-07-21 11:00:00", "guc": 50.0}]
    points = normalize_tescom_devices("tescom-izmir", payload, TRT, PLANT_IDS)
    assert points[0].vendor_plant_id == "tescom-izmir"


def test_unknown_factory_id_falls_back_to_default():
    payload = [{"fabrika_id": "depo", "slave_id": 9, "zaman": "2026-07-21 11:00:00", "guc": 5.0}]
    points = normalize_tescom_devices("tescom-izmir", payload, TRT, PLANT_IDS)
    assert points[0].vendor_plant_id == "tescom-izmir"


def test_normalize_handles_timestamp_without_microseconds():
    payload = [{"slave_id": 3, "zaman": "2026-07-21 11:00:00", "guc": 50.0}]
    points = normalize_tescom_devices("p", payload, TRT)
    assert points[0].ts == datetime(2026, 7, 21, 8, 0, tzinfo=UTC)
    assert points[0].ac_power_kw == 50.0


def test_normalize_skips_bad_timestamp():
    payload = [{"slave_id": 4, "zaman": "not-a-date", "guc": 1.0}]
    assert normalize_tescom_devices("p", payload, TRT) == []


# ------------------------------ Adaptör ------------------------------


@respx.mock
async def test_fetch_plants_returns_one_site_per_factory():
    async with make_adapter() as adapter:
        plants = await adapter.fetch_plants()
    by_key = {p.vendor_plant_id: p for p in plants}
    assert set(by_key) == {URETIM, MEKANIK}
    assert by_key[URETIM].dc_capacity_kwp == 400.0
    assert by_key[MEKANIK].name == "Mekanik Fabrika"
    # Fabrika koordinatı verilmediğinde tesis koordinatına düşer
    assert by_key[URETIM].latitude == 38.42


@respx.mock
async def test_fetch_plants_without_factories_keeps_single_site():
    async with make_adapter(factories=None) as adapter:
        plants = await adapter.fetch_plants()
    assert len(plants) == 1
    assert plants[0].vendor_plant_id == "tescom-izmir"
    assert plants[0].dc_capacity_kwp == 250.0


@respx.mock
async def test_fetch_telemetry_returns_only_requested_site(load_fixture):
    route = respx.get(f"{BASE}/api/v1/devices").mock(
        return_value=httpx.Response(200, json=load_fixture("tescom/devices.json"))
    )
    async with make_adapter() as adapter:
        uretim = await adapter.fetch_telemetry(URETIM, since=SINCE)
        mekanik = await adapter.fetch_telemetry(MEKANIK, since=SINCE)

    assert route.calls.last.request.headers["X-API-Key"] == "secret-key"
    assert {p.vendor_device_id for p in uretim} == {"1", "2"}
    assert {p.vendor_device_id for p in mekanik} == {"1"}
    assert all(p.vendor_plant_id == URETIM for p in uretim)
    assert all(p.ts >= SINCE for p in uretim + mekanik)


@respx.mock
async def test_fetch_devices_lists_only_that_sites_devices(load_fixture):
    respx.get(f"{BASE}/api/v1/devices").mock(
        return_value=httpx.Response(200, json=load_fixture("tescom/devices.json"))
    )
    async with make_adapter() as adapter:
        uretim = await adapter.fetch_devices(URETIM)
        mekanik = await adapter.fetch_devices(MEKANIK)
    assert [d.vendor_device_id for d in uretim] == ["1", "2"]
    assert [d.vendor_device_id for d in mekanik] == ["1"]
    assert all(d.vendor_plant_id == MEKANIK for d in mekanik)


@respx.mock
async def test_old_data_filtered_by_since(load_fixture):
    # since damgadan sonra → hiçbir nokta kalmaz
    future = datetime(2026, 7, 22, tzinfo=UTC)
    respx.get(f"{BASE}/api/v1/devices").mock(
        return_value=httpx.Response(200, json=load_fixture("tescom/devices.json"))
    )
    async with make_adapter() as adapter:
        assert await adapter.fetch_telemetry(URETIM, since=future) == []


@respx.mock
async def test_non_list_payload_is_safe():
    respx.get(f"{BASE}/api/v1/devices").mock(
        return_value=httpx.Response(200, json={"error": "unauthorized"})
    )
    async with make_adapter() as adapter:
        assert await adapter.fetch_telemetry(URETIM, since=SINCE) == []


# ------------------------------ Ayar ayrıştırma ------------------------------


def test_settings_parse_factory_map():
    sites = Settings().tescom_factory_sites
    assert sites["uretim"] == (URETIM, "Üretim Fabrikası", 400.0)
    assert sites["mekanik"] == (MEKANIK, "Mekanik Fabrika", 250.0)


def test_settings_factory_map_skips_malformed_entries():
    settings = Settings(tescom_factories="uretim:key-a:Ad:400,bozuk,:::,mekanik:key-b:Ad2")
    sites = settings.tescom_factory_sites
    assert set(sites) == {"uretim", "mekanik"}
    assert sites["mekanik"] == ("key-b", "Ad2", None)  # kapasite verilmemiş


def test_settings_factory_map_ignores_bad_capacity():
    settings = Settings(tescom_factories="uretim:key-a:Ad:cok")
    assert settings.tescom_factory_sites["uretim"] == ("key-a", "Ad", None)
