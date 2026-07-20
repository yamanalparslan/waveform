"""SMA ennexOS API adaptörü (OAuth2 client-credentials).

NOT: SMA'nın herkese açık resmi API dokümantasyonu yok (PLAN.md açık soru #2).
Endpoint yolları ve yanıt şemaları mock fixture'larla uyumlu geçici bir
sözleşmedir; gerçek API erişimi netleştiğinde yalnızca bu modül ve
`normalize_sma_measurements` güncellenir — arayüz sabit kalır.
"""

import logging
import time
from datetime import datetime
from typing import Any, ClassVar

from luminmind.adapters.base import AdapterAuthError, VendorAdapter
from luminmind.adapters.normalize import normalize_sma_measurements
from luminmind.adapters.retry import request_with_retry
from luminmind.core.schemas import DeviceMeta, PlantMeta, TelemetryPoint, Vendor

logger = logging.getLogger(__name__)

_TOKEN_REFRESH_MARGIN_S = 60.0


class SmaAdapter(VendorAdapter):
    vendor: ClassVar[Vendor] = Vendor.SMA

    def __init__(
        self,
        base_url: str,
        client_id: str,
        client_secret: str,
        timeout_s: float = 30.0,
        backoff_base_s: float = 1.0,
    ) -> None:
        super().__init__(base_url=base_url, timeout_s=timeout_s)
        self._client_id = client_id
        self._client_secret = client_secret
        self._backoff_base_s = backoff_base_s
        self._access_token: str | None = None
        self._token_expires_at: float = 0.0

    async def authenticate(self) -> None:
        """Access token süresi dolmuşsa (marjla) yeniler; değilse dokunmaz."""
        if self._access_token is not None and (
            time.monotonic() < self._token_expires_at - _TOKEN_REFRESH_MARGIN_S
        ):
            return
        response = await request_with_retry(
            self._client,
            "POST",
            "/oauth2/token",
            backoff_base_s=self._backoff_base_s,
            data={
                "grant_type": "client_credentials",
                "client_id": self._client_id,
                "client_secret": self._client_secret,
            },
        )
        body: dict[str, Any] = response.json()
        token = body.get("access_token")
        if not isinstance(token, str):
            raise AdapterAuthError("SMA token response missing access_token")
        self._access_token = token
        self._token_expires_at = time.monotonic() + float(body.get("expires_in", 3600))
        logger.info("SMA access token refreshed (expires_in=%ss)", body.get("expires_in"))

    async def _get(self, path: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
        await self.authenticate()
        response = await request_with_retry(
            self._client,
            "GET",
            path,
            backoff_base_s=self._backoff_base_s,
            params=params,
            headers={"Authorization": f"Bearer {self._access_token}"},
        )
        result: dict[str, Any] = response.json()
        return result

    async def fetch_plants(self) -> list[PlantMeta]:
        body = await self._get("/v1/plants")
        return [
            PlantMeta(
                vendor=Vendor.SMA,
                vendor_plant_id=str(plant["plantId"]),
                name=str(plant.get("name", plant["plantId"])),
                dc_capacity_kwp=(
                    None if plant.get("peakPowerKwp") is None else float(plant["peakPowerKwp"])
                ),
            )
            for plant in body.get("plants") or []
        ]

    async def fetch_devices(self, vendor_plant_id: str) -> list[DeviceMeta]:
        body = await self._get(f"/v1/plants/{vendor_plant_id}/devices")
        return [
            DeviceMeta(
                vendor=Vendor.SMA,
                vendor_plant_id=vendor_plant_id,
                vendor_device_id=str(dev["deviceId"]),
                model=dev.get("productName"),
            )
            for dev in body.get("devices") or []
            if dev.get("type") == "inverter"
        ]

    async def fetch_telemetry(
        self, vendor_plant_id: str, since: datetime
    ) -> list[TelemetryPoint]:
        body = await self._get(
            f"/v1/plants/{vendor_plant_id}/measurements",
            params={"from": since.isoformat()},
        )
        points = normalize_sma_measurements(vendor_plant_id, body)
        return [p for p in points if p.ts >= since]
