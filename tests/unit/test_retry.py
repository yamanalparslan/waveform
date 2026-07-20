import httpx
import pytest
import respx

from luminmind.adapters.base import AdapterError
from luminmind.adapters.retry import request_with_retry

BASE = "https://api.example.com"


@pytest.fixture
def client():
    return httpx.AsyncClient(base_url=BASE)


@respx.mock
async def test_succeeds_after_transient_5xx(client):
    route = respx.get(f"{BASE}/data").mock(
        side_effect=[httpx.Response(500), httpx.Response(502), httpx.Response(200, json={})]
    )
    response = await request_with_retry(client, "GET", "/data", backoff_base_s=0.0)
    assert response.status_code == 200
    assert route.call_count == 3


@respx.mock
async def test_respects_retry_after_header(client):
    respx.get(f"{BASE}/data").mock(
        side_effect=[
            httpx.Response(429, headers={"Retry-After": "0"}),
            httpx.Response(200, json={}),
        ]
    )
    response = await request_with_retry(client, "GET", "/data", backoff_base_s=0.0)
    assert response.status_code == 200


@respx.mock
async def test_non_retryable_4xx_raises_immediately(client):
    route = respx.get(f"{BASE}/data").mock(return_value=httpx.Response(403, text="forbidden"))
    with pytest.raises(AdapterError, match="403"):
        await request_with_retry(client, "GET", "/data", backoff_base_s=0.0)
    assert route.call_count == 1


@respx.mock
async def test_exhausted_retries_raise(client):
    route = respx.get(f"{BASE}/data").mock(return_value=httpx.Response(503))
    with pytest.raises(AdapterError, match="after 3 attempts"):
        await request_with_retry(client, "GET", "/data", retries=2, backoff_base_s=0.0)
    assert route.call_count == 3


@respx.mock
async def test_transport_errors_retried(client):
    route = respx.get(f"{BASE}/data").mock(
        side_effect=[httpx.ConnectError("boom"), httpx.Response(200, json={})]
    )
    response = await request_with_retry(client, "GET", "/data", backoff_base_s=0.0)
    assert response.status_code == 200
    assert route.call_count == 2
