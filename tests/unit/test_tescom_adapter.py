from datetime import UTC, datetime
from zoneinfo import ZoneInfo

import httpx
import respx

from luminmind.adapters import TescomAdapter
from luminmind.adapters.normalize import normalize_tescom_devices
from luminmind.core.schemas import Vendor

BASE = "http://tescom.local:8503"
TRT = ZoneInfo("Europe/Istanbul")
# 2026-07-21 10:39 TRT = 07:39 UTC
SINCE = datetime(2026, 7, 21, 7, 0, tzinfo=UTC)


def make_adapter() -> TescomAdapter:
    return TescomAdapter(
        base_url=BASE, api_key="secret-key", dc_capacity_kwp=250.0, backoff_base_s=0.0
    )


def test_normalize_tescom_maps_fields_and_tz(load_fixture):
    payload = load_fixture("tescom/devices.json")
    # filtresiz → tüm 3 kayıt (mekanik-1, uretim-1, uretim-2)
    points = normalize_tescom_devices("tescom-izmir", payload, TRT)
    assert len(points) == 3
    mekanik = next(p for p in points if p.ac_power_kw == 124.73)
    assert mekanik.vendor == Vendor.TESCOM
    assert mekanik.vendor_plant_id == "tescom-izmir"
    assert mekanik.vendor_device_id == "1"
    assert mekanik.dc_voltage_v == 801.9
    assert mekanik.temp_c == 52.6
    # yerel 10:39:27 TRT → 07:39:27 UTC
    assert mekanik.ts == datetime(2026, 7, 21, 7, 39, 27, 661885, tzinfo=UTC)


def test_normalize_tescom_filters_by_fabrika(load_fixture):
    payload = load_fixture("tescom/devices.json")
    uretim = normalize_tescom_devices(
        "tescom-izmir-uretim", payload, TRT, fabrika_filter="uretim"
    )
    # uretim fabrikasında slave_id 1 ve 2 var
    assert {p.vendor_device_id for p in uretim} == {"1", "2"}
    assert all(p.vendor_plant_id == "tescom-izmir-uretim" for p in uretim)

    mekanik = normalize_tescom_devices(
        "tescom-izmir-mekanik", payload, TRT, fabrika_filter="mekanik"
    )
    assert [p.vendor_device_id for p in mekanik] == ["1"]


def test_normalize_handles_timestamp_without_microseconds():
    payload = [{"slave_id": 3, "zaman": "2026-07-21 11:00:00", "guc": 50.0}]
    points = normalize_tescom_devices("p", payload, TRT)
    assert points[0].ts == datetime(2026, 7, 21, 8, 0, tzinfo=UTC)
    assert points[0].ac_power_kw == 50.0


def test_normalize_skips_bad_timestamp():
    payload = [{"slave_id": 4, "zaman": "not-a-date", "guc": 1.0}]
    assert normalize_tescom_devices("p", payload, TRT) == []


@respx.mock
async def test_fetch_plants_discovers_fabrikalar(load_fixture):
    respx.get(f"{BASE}/api/v1/devices").mock(
        return_value=httpx.Response(200, json=load_fixture("tescom/devices.json"))
    )
    async with make_adapter() as adapter:
        plants = await adapter.fetch_plants()
    # 2 fabrika (mekanik, uretim) → 2 tesis
    ids = {p.vendor_plant_id for p in plants}
    assert ids == {"tescom-izmir-mekanik", "tescom-izmir-uretim"}
    # her tesis aynı koordinatı taşır ama adı fabrikaya göre farklıdır
    names = {p.name for p in plants}
    assert "Tescom İzmir GES · Mekanik" in names
    assert "Tescom İzmir GES · Uretim" in names


@respx.mock
async def test_fetch_plants_no_fabrika_field_returns_single_plant():
    # Eski API biçimi (fabrika_id yok) — tek tesis olarak sunulmalı
    respx.get(f"{BASE}/api/v1/devices").mock(
        return_value=httpx.Response(
            200,
            json=[{"slave_id": 1, "zaman": "2026-07-21 10:00:00", "guc": 5.0}],
        )
    )
    async with make_adapter() as adapter:
        plants = await adapter.fetch_plants()
    assert len(plants) == 1
    assert plants[0].vendor_plant_id == "tescom-izmir"


@respx.mock
async def test_fetch_telemetry_filters_by_fabrika(load_fixture):
    route = respx.get(f"{BASE}/api/v1/devices").mock(
        return_value=httpx.Response(200, json=load_fixture("tescom/devices.json"))
    )
    async with make_adapter() as adapter:
        uretim = await adapter.fetch_telemetry("tescom-izmir-uretim", since=SINCE)
        mekanik = await adapter.fetch_telemetry("tescom-izmir-mekanik", since=SINCE)

    assert route.calls.last.request.headers["X-API-Key"] == "secret-key"
    assert {p.vendor_device_id for p in uretim} == {"1", "2"}
    assert all(p.vendor_plant_id == "tescom-izmir-uretim" for p in uretim)
    assert [p.vendor_device_id for p in mekanik] == ["1"]
    assert all(p.vendor_plant_id == "tescom-izmir-mekanik" for p in mekanik)


@respx.mock
async def test_fetch_devices_scoped_to_fabrika(load_fixture):
    respx.get(f"{BASE}/api/v1/devices").mock(
        return_value=httpx.Response(200, json=load_fixture("tescom/devices.json"))
    )
    async with make_adapter() as adapter:
        uretim = await adapter.fetch_devices("tescom-izmir-uretim")
    assert {d.vendor_device_id for d in uretim} == {"1", "2"}


@respx.mock
async def test_old_data_filtered_by_since(load_fixture):
    # since damgadan sonra → hiçbir nokta kalmaz
    future = datetime(2026, 7, 22, tzinfo=UTC)
    respx.get(f"{BASE}/api/v1/devices").mock(
        return_value=httpx.Response(200, json=load_fixture("tescom/devices.json"))
    )
    async with make_adapter() as adapter:
        assert await adapter.fetch_telemetry("tescom-izmir", since=future) == []


@respx.mock
async def test_non_list_payload_is_safe():
    respx.get(f"{BASE}/api/v1/devices").mock(
        return_value=httpx.Response(200, json={"error": "unauthorized"})
    )
    async with make_adapter() as adapter:
        assert await adapter.fetch_telemetry("tescom-izmir", since=SINCE) == []
