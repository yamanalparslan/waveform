from datetime import UTC, datetime

import httpx
import respx

from luminmind.adapters import SmaAdapter

BASE = "https://api.sma.example"
SINCE = datetime(2026, 7, 20, 3, 45, tzinfo=UTC)


def make_adapter() -> SmaAdapter:
    return SmaAdapter(
        base_url=BASE, client_id="lm", client_secret="secret", backoff_base_s=0.0
    )


def token_response(expires_in: int = 3600) -> httpx.Response:
    return httpx.Response(
        200, json={"access_token": "tok-1", "token_type": "Bearer", "expires_in": expires_in}
    )


@respx.mock
async def test_token_fetched_once_and_reused():
    token_route = respx.post(f"{BASE}/oauth2/token").mock(return_value=token_response())
    plants_route = respx.get(f"{BASE}/v1/plants").mock(
        return_value=httpx.Response(
            200, json={"plants": [{"plantId": "sma-plant-1", "name": "Test", "peakPowerKwp": 500}]}
        )
    )

    async with make_adapter() as adapter:
        await adapter.fetch_plants()
        await adapter.fetch_plants()

    assert token_route.call_count == 1  # ikinci çağrıda token önbellekten
    assert plants_route.calls.last.request.headers["Authorization"] == "Bearer tok-1"


@respx.mock
async def test_expired_token_refreshed():
    token_route = respx.post(f"{BASE}/oauth2/token").mock(return_value=token_response(expires_in=0))
    respx.get(f"{BASE}/v1/plants").mock(return_value=httpx.Response(200, json={"plants": []}))

    async with make_adapter() as adapter:
        await adapter.fetch_plants()
        await adapter.fetch_plants()

    assert token_route.call_count == 2  # süre dolduğu için her seferinde yenilendi


@respx.mock
async def test_fetch_telemetry_normalizes_and_filters(load_fixture):
    respx.post(f"{BASE}/oauth2/token").mock(return_value=token_response())
    respx.get(f"{BASE}/v1/plants/sma-plant-1/measurements").mock(
        return_value=httpx.Response(200, json=load_fixture("sma/measurements.json"))
    )

    async with make_adapter() as adapter:
        points = await adapter.fetch_telemetry("sma-plant-1", since=SINCE)

    # inv-02'nin +03:00 damgası UTC 04:00 → since (03:45) sonrası, ikisi de kalır
    assert len(points) == 2
    assert {p.vendor_device_id for p in points} == {"inv-01", "inv-02"}
    assert points[0].ac_power_kw == 182.4
