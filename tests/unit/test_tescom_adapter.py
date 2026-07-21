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
    points = normalize_tescom_devices("tescom-izmir", payload, TRT)

    assert len(points) == 2
    first = points[0]
    assert first.vendor == Vendor.TESCOM
    assert first.vendor_plant_id == "tescom-izmir"
    assert first.vendor_device_id == "1"
    assert first.ac_power_kw == 124.73
    assert first.dc_voltage_v == 801.9
    assert first.dc_current_a == 90.33
    assert first.temp_c == 52.6
    # yerel 10:39:27 TRT → 07:39:27 UTC
    assert first.ts == datetime(2026, 7, 21, 7, 39, 27, 661885, tzinfo=UTC)


def test_normalize_handles_timestamp_without_microseconds():
    payload = [{"slave_id": 3, "zaman": "2026-07-21 11:00:00", "guc": 50.0}]
    points = normalize_tescom_devices("p", payload, TRT)
    assert points[0].ts == datetime(2026, 7, 21, 8, 0, tzinfo=UTC)
    assert points[0].ac_power_kw == 50.0


def test_normalize_skips_bad_timestamp():
    payload = [{"slave_id": 4, "zaman": "not-a-date", "guc": 1.0}]
    assert normalize_tescom_devices("p", payload, TRT) == []


@respx.mock
async def test_fetch_plants_uses_config():
    async with make_adapter() as adapter:
        plants = await adapter.fetch_plants()
    assert len(plants) == 1
    assert plants[0].vendor_plant_id == "tescom-izmir"
    assert plants[0].latitude == 38.42
    assert plants[0].dc_capacity_kwp == 250.0


@respx.mock
async def test_fetch_telemetry_sends_api_key_header(load_fixture):
    route = respx.get(f"{BASE}/api/v1/devices").mock(
        return_value=httpx.Response(200, json=load_fixture("tescom/devices.json"))
    )
    async with make_adapter() as adapter:
        points = await adapter.fetch_telemetry("tescom-izmir", since=SINCE)

    assert route.calls.last.request.headers["X-API-Key"] == "secret-key"
    assert len(points) == 2
    assert {p.vendor_device_id for p in points} == {"1", "2"}
    assert all(p.ts >= SINCE for p in points)


@respx.mock
async def test_fetch_devices_lists_slave_ids(load_fixture):
    respx.get(f"{BASE}/api/v1/devices").mock(
        return_value=httpx.Response(200, json=load_fixture("tescom/devices.json"))
    )
    async with make_adapter() as adapter:
        devices = await adapter.fetch_devices("tescom-izmir")
    assert [d.vendor_device_id for d in devices] == ["1", "2"]


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
