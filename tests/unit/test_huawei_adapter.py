from datetime import UTC, datetime

import httpx
import pytest
import respx

from luminmind.adapters import AdapterError, HuaweiAdapter

BASE = "https://eu5.fusionsolar.huawei.com"
SINCE = datetime(2026, 7, 20, 6, 45, tzinfo=UTC)


def make_adapter() -> HuaweiAdapter:
    return HuaweiAdapter(
        base_url=BASE, username="lm-api", system_code="secret", backoff_base_s=0.0
    )


def login_ok() -> httpx.Response:
    return httpx.Response(
        200,
        json={"success": True, "failCode": 0},
        headers={"Set-Cookie": "XSRF-TOKEN=token-1; Path=/"},
    )


@respx.mock
async def test_login_captures_xsrf_token_and_fetches_plants(load_fixture):
    respx.post(f"{BASE}/thirdData/login").mock(return_value=login_ok())
    station_route = respx.post(f"{BASE}/thirdData/getStationList").mock(
        return_value=httpx.Response(200, json=load_fixture("huawei/station_list.json"))
    )

    async with make_adapter() as adapter:
        plants = await adapter.fetch_plants()

    assert station_route.calls.last.request.headers["XSRF-TOKEN"] == "token-1"
    assert len(plants) == 1
    assert plants[0].vendor_plant_id == "NE=33554616"
    assert plants[0].name == "Konya GES 1"
    assert plants[0].dc_capacity_kwp == 1000.0  # 1.0 MW → kWp


@respx.mock
async def test_login_failure_raises_auth_error():
    respx.post(f"{BASE}/thirdData/login").mock(
        return_value=httpx.Response(200, json={"success": False, "failCode": 20001})
    )
    async with make_adapter() as adapter:
        with pytest.raises(AdapterError, match="login failed"):
            await adapter.fetch_plants()


@respx.mock
async def test_expired_session_triggers_single_relogin(load_fixture):
    login_route = respx.post(f"{BASE}/thirdData/login").mock(return_value=login_ok())
    respx.post(f"{BASE}/thirdData/getStationList").mock(
        side_effect=[
            httpx.Response(200, json={"success": False, "failCode": 305}),
            httpx.Response(200, json=load_fixture("huawei/station_list.json")),
        ]
    )

    async with make_adapter() as adapter:
        plants = await adapter.fetch_plants()

    assert len(plants) == 1
    assert login_route.call_count == 2  # ilk login + oturum düşünce yeniden login


@respx.mock
async def test_rate_limit_failcode_raises():
    respx.post(f"{BASE}/thirdData/login").mock(return_value=login_ok())
    respx.post(f"{BASE}/thirdData/getStationList").mock(
        return_value=httpx.Response(200, json={"success": False, "failCode": 407})
    )
    async with make_adapter() as adapter:
        with pytest.raises(AdapterError, match="rate limit"):
            await adapter.fetch_plants()


@respx.mock
async def test_fetch_telemetry_filters_inverters_and_normalizes(load_fixture):
    respx.post(f"{BASE}/thirdData/login").mock(return_value=login_ok())
    respx.post(f"{BASE}/thirdData/getDevList").mock(
        return_value=httpx.Response(200, json=load_fixture("huawei/dev_list.json"))
    )
    kpi_route = respx.post(f"{BASE}/thirdData/getDevFiveMinutes").mock(
        return_value=httpx.Response(200, json=load_fixture("huawei/dev_five_minutes.json"))
    )

    async with make_adapter() as adapter:
        points = await adapter.fetch_telemetry("NE=33554616", since=SINCE)

    sent = kpi_route.calls.last.request
    import json

    body = json.loads(sent.content)
    # devTypeId=17 (sayaç) filtrelendi, yalnızca iki invertör istendi
    assert body["devIds"] == "1000000031104426,1000000031104427"
    assert len(points) == 2
    assert all(p.ts >= SINCE for p in points)
