import math
from datetime import UTC, date, datetime

import httpx
import respx

from luminmind.twin.weather import (
    OpenMeteoClient,
    parse_ensemble,
    parse_minutely_15,
    sample_interval,
)


def test_parse_minutely_15(load_fixture):
    samples = parse_minutely_15(load_fixture("openmeteo/minutely15.json"))
    assert len(samples) == 4
    first = samples[0]
    assert first.ts == datetime(2026, 7, 20, 9, 0, tzinfo=UTC)
    assert first.ghi_wm2 == 820.5
    assert first.dni_wm2 == 880.0
    assert first.dhi_wm2 == 110.0
    assert first.temp_c == 31.2
    assert first.wind_ms == 3.1
    # Eksik ışınım NaN'dır, 0.0 değil: 0.0 "gece" demektir ve dijital ikiz
    # eksik veriyi gece sanarsa gerçek üretim sınırsız pozitif sapma üretir.
    assert math.isnan(samples[2].ghi_wm2)
    assert not samples[2].has_irradiance
    assert first.has_irradiance


def test_parse_empty_payload():
    assert parse_minutely_15({}) == []


@respx.mock
async def test_client_requests_utc_15m_day(load_fixture):
    route = respx.get("https://api.open-meteo.com/v1/forecast").mock(
        return_value=httpx.Response(200, json=load_fixture("openmeteo/minutely15.json"))
    )
    client = OpenMeteoClient()
    try:
        samples = await client.fetch_day_15m(37.87, 32.48, date(2026, 7, 20))
    finally:
        await client.aclose()

    params = dict(route.calls.last.request.url.params)
    assert params["timezone"] == "UTC"
    assert params["start_date"] == "2026-07-20"
    assert params["wind_speed_unit"] == "ms"
    assert "shortwave_radiation" in params["minutely_15"]
    # Kirlilik ve spektral düzeltme için gereken ek alanlar da istenmeli
    assert "relative_humidity_2m" in params["minutely_15"]
    assert "precipitation" in params["minutely_15"]
    assert "models" not in params  # deterministik istekte ensemble yok
    assert len(samples) == 4


def test_parse_ensemble_splits_members_and_drops_empty(load_fixture):
    payload = load_fixture("openmeteo/ensemble15.json")
    members = parse_ensemble(payload, ["icon_seamless", "gfs_seamless", "ecmwf_ifs025"])
    # Tamamı null olan üye elenir — sahte bir "sıfır üretim" üyesi bandı bozardı
    assert set(members) == {"icon_seamless", "gfs_seamless"}
    assert members["icon_seamless"][0].ghi_wm2 == 820.5
    assert members["gfs_seamless"][0].ghi_wm2 == 790.0
    assert members["icon_seamless"][0].relative_humidity_pct == 34.0
    assert members["icon_seamless"][0].pressure_hpa == 908.1


@respx.mock
async def test_fetch_range_requests_models_and_returns_members(load_fixture):
    route = respx.get("https://api.open-meteo.com/v1/forecast").mock(
        return_value=httpx.Response(200, json=load_fixture("openmeteo/ensemble15.json"))
    )
    client = OpenMeteoClient()
    try:
        members = await client.fetch_range_15m(
            37.87, 32.48, date(2026, 7, 20), date(2026, 7, 22), ["icon_seamless", "gfs_seamless"]
        )
    finally:
        await client.aclose()

    params = dict(route.calls.last.request.url.params)
    assert params["models"] == "icon_seamless,gfs_seamless"
    assert params["end_date"] == "2026-07-22"
    assert set(members) == {"icon_seamless", "gfs_seamless"}


@respx.mock
async def test_fetch_range_without_models_returns_single_member(load_fixture):
    respx.get("https://api.open-meteo.com/v1/forecast").mock(
        return_value=httpx.Response(200, json=load_fixture("openmeteo/minutely15.json"))
    )
    client = OpenMeteoClient()
    try:
        members = await client.fetch_range_15m(37.87, 32.48, date(2026, 7, 20), date(2026, 7, 20))
    finally:
        await client.aclose()
    assert list(members) == [""]
    assert len(members[""]) == 4


def test_sample_interval_from_timestamps(load_fixture):
    from datetime import timedelta

    samples = parse_minutely_15(load_fixture("openmeteo/minutely15.json"))
    assert sample_interval(samples) == timedelta(minutes=15)
    assert sample_interval(samples[:1]) == timedelta(minutes=15)
