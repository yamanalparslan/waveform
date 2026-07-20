"""HTTP retry/backoff stratejisi.

Politika:
- 429 → `Retry-After` başlığına (yoksa üstel backoff'a) göre bekle, yeniden dene.
- 5xx ve ağ/timeout hataları → üstel backoff (base * 2^attempt) ile yeniden dene.
- 4xx (429 hariç) → kalıcı hata, hemen `AdapterError`.
- Denemeler tükenince son hatayla `AdapterError`.
"""

import asyncio
import logging
from typing import Any

import httpx

from luminmind.adapters.base import AdapterError

logger = logging.getLogger(__name__)

_RETRYABLE_STATUS = frozenset({429, 500, 502, 503, 504})


def _retry_delay_s(response: httpx.Response | None, attempt: int, backoff_base_s: float) -> float:
    if response is not None and response.status_code == 429:
        retry_after = response.headers.get("Retry-After")
        if retry_after is not None:
            try:
                return max(float(retry_after), 0.0)
            except ValueError:
                pass  # HTTP tarih formatı vb. → üstel backoff'a düş
    return backoff_base_s * float(2**attempt)


async def request_with_retry(
    client: httpx.AsyncClient,
    method: str,
    url: str,
    *,
    retries: int = 3,
    backoff_base_s: float = 1.0,
    **request_kwargs: Any,
) -> httpx.Response:
    """`retries` yeniden deneme hakkıyla istek atar (toplam en çok retries+1 deneme)."""
    last_error: Exception | None = None
    for attempt in range(retries + 1):
        response: httpx.Response | None = None
        try:
            response = await client.request(method, url, **request_kwargs)
        except httpx.TransportError as exc:
            last_error = exc
            logger.warning("%s %s transport error (attempt %d): %s", method, url, attempt, exc)
        else:
            if response.status_code < 400:
                return response
            if response.status_code not in _RETRYABLE_STATUS:
                raise AdapterError(
                    f"{method} {url} failed with status {response.status_code}: "
                    f"{response.text[:500]}"
                )
            last_error = httpx.HTTPStatusError(
                f"status {response.status_code}", request=response.request, response=response
            )
            logger.warning(
                "%s %s got %d (attempt %d)", method, url, response.status_code, attempt
            )
        if attempt < retries:
            await asyncio.sleep(_retry_delay_s(response, attempt, backoff_base_s))
    raise AdapterError(f"{method} {url} failed after {retries + 1} attempts") from last_error
