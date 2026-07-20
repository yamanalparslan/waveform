from datetime import UTC, date, datetime

import httpx
import respx

from luminmind.twin.weather import OpenMeteoClient, parse_minutely_15


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
    # null ışınım 0.0'a düşer (gece / eksik veri)
    assert samples[2].ghi_wm2 == 0.0


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
    assert len(samples) == 4
