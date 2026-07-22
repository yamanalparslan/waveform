"""Tescom UPS inverter izleme API adaptörü (İzmir fabrikası — gerçek PV verisi).

API (yerel servis, port 8503):
- `GET /api/v1/devices` → tüm invertörlerin anlık verisi (cihaz listesi)
- `GET /api/v1/devices/{slave_id}/latest?limit=N` → tek cihazın son N geçmiş verisi
- Kimlik doğrulama: `X-API-Key` başlığı (oturum/token yenileme yok)

Cihaz kimliği `(fabrika_id, slave_id)` çiftidir — aynı slave_id farklı
fabrikalarda (ör. "mekanik", "uretim") olabilir. LuminMind bu bilgiyi
**her fabrikayı ayrı bir tesis** olarak modelleyerek yönetir:
`vendor_plant_id = f"{plant_id_prefix}-{fabrika_id}"` (ör. tescom-izmir-mekanik).
Fabrika listesi API cevabından dinamik olarak keşfedilir.

Depolama (BESS) verisi yoktur — yalnızca PV üretimi.
"""

import logging
from datetime import datetime
from typing import Any, ClassVar
from zoneinfo import ZoneInfo

from luminmind.adapters.base import VendorAdapter
from luminmind.adapters.normalize import normalize_tescom_devices, tescom_fabrika_ids
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
        self._plant_id_prefix = plant_id
        self._plant_name_base = plant_name
        self._latitude = latitude
        self._longitude = longitude
        self._dc_capacity_kwp = dc_capacity_kwp
        self._tz = ZoneInfo(timezone)
        self._backoff_base_s = backoff_base_s

    async def authenticate(self) -> None:
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

    def _fabrika_from_plant_id(self, vendor_plant_id: str) -> str | None:
        """`tescom-izmir-mekanik` → `mekanik`. Tarihi tek-fabrika kayıtlarını
        (prefix ile birebir eşleşen) None olarak döndürür."""
        prefix = f"{self._plant_id_prefix}-"
        if vendor_plant_id == self._plant_id_prefix:
            return None
        if vendor_plant_id.startswith(prefix):
            return vendor_plant_id[len(prefix):]
        return None

    async def fetch_plants(self) -> list[PlantMeta]:
        """API'den fabrikaları keşfeder; her fabrika ayrı bir tesis olur."""
        payload = await self._get_devices()
        fabrikalar = tescom_fabrika_ids(payload)
        if not fabrikalar:
            # eski API biçimi (fabrika_id yok) — tek tesis olarak sun
            return [
                PlantMeta(
                    vendor=Vendor.TESCOM,
                    vendor_plant_id=self._plant_id_prefix,
                    name=self._plant_name_base,
                    dc_capacity_kwp=self._dc_capacity_kwp,
                    latitude=self._latitude,
                    longitude=self._longitude,
                )
            ]
        return [
            PlantMeta(
                vendor=Vendor.TESCOM,
                vendor_plant_id=f"{self._plant_id_prefix}-{fab}",
                name=f"{self._plant_name_base} · {fab.title()}",
                dc_capacity_kwp=self._dc_capacity_kwp,
                latitude=self._latitude,
                longitude=self._longitude,
            )
            for fab in fabrikalar
        ]

    async def fetch_devices(self, vendor_plant_id: str) -> list[DeviceMeta]:
        fabrika = self._fabrika_from_plant_id(vendor_plant_id)
        payload = await self._get_devices()
        devices: list[DeviceMeta] = []
        for item in payload:
            if fabrika is not None and item.get("fabrika_id") != fabrika:
                continue
            if "slave_id" not in item:
                continue
            devices.append(
                DeviceMeta(
                    vendor=Vendor.TESCOM,
                    vendor_plant_id=vendor_plant_id,
                    vendor_device_id=str(item["slave_id"]),
                    model="Tescom Inverter",
                )
            )
        return devices

    async def fetch_telemetry(
        self, vendor_plant_id: str, since: datetime
    ) -> list[TelemetryPoint]:
        fabrika = self._fabrika_from_plant_id(vendor_plant_id)
        payload = await self._get_devices()
        points = normalize_tescom_devices(
            vendor_plant_id, payload, self._tz, fabrika_filter=fabrika
        )
        return [p for p in points if p.ts >= since]
