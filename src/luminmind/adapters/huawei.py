"""Huawei FusionSolar Northbound API adaptörü.

Kimlik doğrulama: `POST /thirdData/login` → yanıtın `XSRF-TOKEN` çerezi sonraki
tüm isteklerde `XSRF-TOKEN` başlığı olarak gönderilir. Oturum düştüğünde API
`failCode: 305` döndürür; adaptör bir kez yeniden login olup isteği tekrarlar.

Dikkat: Northbound login endpoint'i saatlik çağrı limitine sahiptir
(PLAN.md risk #1) — bu yüzden token istek başına değil, düşene kadar saklanır.
"""

import logging
from datetime import datetime
from typing import Any, ClassVar

from luminmind.adapters.base import AdapterAuthError, AdapterError, VendorAdapter
from luminmind.adapters.normalize import normalize_huawei_dev_kpi
from luminmind.adapters.retry import request_with_retry
from luminmind.core.schemas import DeviceMeta, PlantMeta, TelemetryPoint, Vendor

logger = logging.getLogger(__name__)

_FAILCODE_SESSION_EXPIRED = 305
_FAILCODE_RATE_LIMITED = 407


class HuaweiAdapter(VendorAdapter):
    vendor: ClassVar[Vendor] = Vendor.HUAWEI

    def __init__(
        self,
        base_url: str,
        username: str,
        system_code: str,
        timeout_s: float = 30.0,
        backoff_base_s: float = 1.0,
    ) -> None:
        super().__init__(base_url=base_url, timeout_s=timeout_s)
        self._username = username
        self._system_code = system_code
        self._backoff_base_s = backoff_base_s
        self._xsrf_token: str | None = None

    async def authenticate(self) -> None:
        response = await request_with_retry(
            self._client,
            "POST",
            "/thirdData/login",
            backoff_base_s=self._backoff_base_s,
            json={"userName": self._username, "systemCode": self._system_code},
        )
        body: dict[str, Any] = response.json()
        token = response.cookies.get("XSRF-TOKEN") or response.headers.get("XSRF-TOKEN")
        if not body.get("success") or token is None:
            raise AdapterAuthError(f"FusionSolar login failed: failCode={body.get('failCode')}")
        self._xsrf_token = token
        logger.info("FusionSolar session established for %s", self._username)

    async def _post(self, path: str, json: dict[str, Any]) -> dict[str, Any]:
        """Oturumlu POST; süresi dolmuş oturumda bir kez yeniden login olur."""
        if self._xsrf_token is None:
            await self.authenticate()
        for _ in range(2):
            response = await request_with_retry(
                self._client,
                "POST",
                path,
                backoff_base_s=self._backoff_base_s,
                json=json,
                headers={"XSRF-TOKEN": self._xsrf_token or ""},
            )
            body: dict[str, Any] = response.json()
            if body.get("success"):
                return body
            fail_code = body.get("failCode")
            if fail_code == _FAILCODE_SESSION_EXPIRED:
                logger.info("FusionSolar session expired; re-authenticating")
                await self.authenticate()
                continue
            if fail_code == _FAILCODE_RATE_LIMITED:
                raise AdapterError(f"FusionSolar rate limit hit on {path} (failCode 407)")
            raise AdapterError(f"FusionSolar call {path} failed: failCode={fail_code}")
        raise AdapterAuthError(f"FusionSolar session could not be renewed for {path}")

    async def fetch_plants(self) -> list[PlantMeta]:
        body = await self._post("/thirdData/getStationList", json={})
        plants: list[PlantMeta] = []
        for station in body.get("data") or []:
            plants.append(
                PlantMeta(
                    vendor=Vendor.HUAWEI,
                    vendor_plant_id=str(station["stationCode"]),
                    name=str(station.get("stationName", station["stationCode"])),
                    dc_capacity_kwp=(
                        None if station.get("capacity") is None
                        # Northbound `capacity` alanı MW cinsindendir
                        else float(station["capacity"]) * 1000
                    ),
                )
            )
        return plants

    async def fetch_devices(self, vendor_plant_id: str) -> list[DeviceMeta]:
        body = await self._post("/thirdData/getDevList", json={"stationCodes": vendor_plant_id})
        devices: list[DeviceMeta] = []
        for dev in body.get("data") or []:
            # devTypeId 1 = string invertör; diğer cihaz tipleri şimdilik kapsam dışı
            if dev.get("devTypeId") != 1:
                continue
            devices.append(
                DeviceMeta(
                    vendor=Vendor.HUAWEI,
                    vendor_plant_id=vendor_plant_id,
                    vendor_device_id=str(dev["id"]),
                    model=dev.get("invType"),
                )
            )
        return devices

    async def fetch_telemetry(
        self, vendor_plant_id: str, since: datetime
    ) -> list[TelemetryPoint]:
        devices = await self.fetch_devices(vendor_plant_id)
        if not devices:
            return []
        body = await self._post(
            "/thirdData/getDevFiveMinutes",
            json={
                "devIds": ",".join(d.vendor_device_id for d in devices),
                "devTypeId": 1,
                "collectTime": int(since.timestamp() * 1000),
            },
        )
        points = normalize_huawei_dev_kpi(vendor_plant_id, body)
        return [p for p in points if p.ts >= since]
