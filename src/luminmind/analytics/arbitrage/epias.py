"""EPİAŞ Şeffaflık Platformu fiyat istemcisi arayüzü.

NOT: Şeffaflık 2.0 servis hesabı henüz yok (PLAN.md kararı + risk #4). Endpoint
yolu ve yanıt şeması geçici bir sözleşmedir; kayıt tamamlanınca TGT tabanlı
kimlik doğrulama ve gerçek şema yalnızca bu modülde güncellenir. O zamana kadar
üretimde `MockPriceProvider` (mock_prices.py) kullanılır.
"""

from dataclasses import dataclass
from datetime import UTC, date, datetime
from typing import Any, Protocol

import httpx

from luminmind.adapters.retry import request_with_retry

MARKET_DAM = "DAM"  # Gün Öncesi Piyasası (GÖP)
MARKET_IDM = "IDM"  # Gün İçi Piyasası (GİP)


@dataclass(frozen=True)
class PriceSlot:
    start: datetime
    price_try_mwh: float


class PriceProvider(Protocol):
    async def fetch_day_ahead_prices(self, day: date) -> list[PriceSlot]: ...


class EpiasClient:
    """Şeffaflık 2.0 GÖP PTF (MCP) istemcisi — geçici sözleşme, respx ile test edilir."""

    def __init__(self, base_url: str, timeout_s: float = 30.0) -> None:
        self._client = httpx.AsyncClient(base_url=base_url, timeout=timeout_s)

    async def aclose(self) -> None:
        await self._client.aclose()

    async def fetch_day_ahead_prices(self, day: date) -> list[PriceSlot]:
        response = await request_with_retry(
            self._client,
            "GET",
            "/v1/markets/dam/mcp",
            params={"date": day.isoformat()},
        )
        payload: dict[str, Any] = response.json()
        slots: list[PriceSlot] = []
        for item in payload.get("items") or []:
            slots.append(
                PriceSlot(
                    start=datetime.fromisoformat(item["hour_start"]).astimezone(UTC),
                    price_try_mwh=float(item["price_try_mwh"]),
                )
            )
        return slots
