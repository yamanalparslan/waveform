"""Tescom UPS inverter izleme API adaptörü (İzmir fabrikası — gerçek PV verisi).

API (yerel servis, port 8503):
- `GET /api/v1/devices` → tüm inverterlerin anlık verisi (cihaz listesi)
- `GET /api/v1/devices/{slave_id}/latest?limit=N` → tek cihazın son N geçmiş verisi
- Kimlik doğrulama: `X-API-Key` başlığı (oturum/token yenileme yok)

Tescom "tesis" kavramı sunmaz, yalnızca cihazlar (slave_id) döner; bu yüzden
tüm cihazlar tek bir LuminMind tesisi (config'ten koordinat/ad) altında toplanır.
Depolama (BESS) verisi yoktur — yalnızca PV üretimi.
"""

import logging
from datetime import datetime
from typing import Any, ClassVar
from zoneinfo import ZoneInfo

from luminmind.adapters.base import VendorAdapter
from luminmind.adapters.normalize import normalize_tescom_devices
from luminmind.adapters.retry import request_with_retry
from luminmind.core.schemas import DeviceMeta, PlantMeta, TelemetryPoint, Vendor

logger = logging.getLogger(__name__)


class TescomAdapter(VendorAdapter):
    vendor: ClassVar[Vendor] = Vendor.TESCOM

    def __init__(
        self,
        base_url: str,
        api_key: str,
        plant_id: str = "tescom-izmir",
        plant_name: str = "Tescom İzmir GES",
        latitude: float = 38.42,
        longitude: float = 27.14,
        dc_capacity_kwp: float | None = None,
        timezone: str = "Europe/Istanbul",
        timeout_s: float = 30.0,
        backoff_base_s: float = 1.0,
    ) -> None:
        super().__init__(base_url=base_url, timeout_s=timeout_s)
        self._api_key = api_key
        self._plant_id = plant_id
        self._plant_name = plant_name
        self._latitude = latitude
        self._longitude = longitude
        self._dc_capacity_kwp = dc_capacity_kwp
        self._tz = ZoneInfo(timezone)
        self._backoff_base_s = backoff_base_s

    async def authenticate(self) -> None:
        # API key başlıkta gönderilir; ayrı oturum akışı yok.
        return None

    @property
    def _headers(self) -> dict[str, str]:
        return {"X-API-Key": self._api_key}

    async def _get_devices(self) -> list[dict[str, Any]]:
        response = await request_with_retry(
            self._client,
            "GET",
            "/api/v1/devices",
            backoff_base_s=self._backoff_base_s,
            headers=self._headers,
        )
        payload = response.json()
        if not isinstance(payload, list):
            logger.warning("Tescom /devices unexpected payload type: %s", type(payload))
            return []
        return payload

    async def fetch_plants(self) -> list[PlantMeta]:
        return [
            PlantMeta(
                vendor=Vendor.TESCOM,
                vendor_plant_id=self._plant_id,
                name=self._plant_name,
                dc_capacity_kwp=self._dc_capacity_kwp,
                latitude=self._latitude,
                longitude=self._longitude,
            )
        ]

    async def fetch_devices(self, vendor_plant_id: str) -> list[DeviceMeta]:
        return [
            DeviceMeta(
                vendor=Vendor.TESCOM,
                vendor_plant_id=self._plant_id,
                vendor_device_id=str(item["slave_id"]),
                model="Tescom Inverter",
            )
            for item in await self._get_devices()
            if "slave_id" in item
        ]

    async def fetch_telemetry(
        self, vendor_plant_id: str, since: datetime
    ) -> list[TelemetryPoint]:
        payload = await self._get_devices()
        points = normalize_tescom_devices(self._plant_id, payload, self._tz)
        return [p for p in points if p.ts >= since]
