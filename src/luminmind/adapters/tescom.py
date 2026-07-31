"""Tescom UPS inverter izleme API adaptörü (İzmir fabrikası — gerçek PV verisi).

API (yerel servis, port 8503):
- `GET /api/v1/devices` → tüm inverterlerin anlık verisi (cihaz listesi)
- `GET /api/v1/devices/{slave_id}/latest?limit=N` → tek cihazın son N geçmiş verisi
- Kimlik doğrulama: `X-API-Key` başlığı (oturum/token yenileme yok)

Tescom "tesis" kavramı sunmaz ama her cihaz bir `fabrika_id` taşır (İzmir
yerleşkesinde `uretim` ve `mekanik`). `slave_id` yalnızca fabrika içinde
tekildir — iki fabrikada da 1 numaralı cihaz vardır. Bu yüzden her fabrika
LuminMind tarafında ayrı bir **saha anahtarı** (`vendor_plant_id`) altına
yazılır; sahalar Postgres'te tek bir tesis (Tescom UPS İzmir) altında toplanır.

Depolama (BESS) verisi yoktur — yalnızca PV üretimi.
"""

import logging
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, ClassVar
from zoneinfo import ZoneInfo

from luminmind.adapters.base import VendorAdapter
from luminmind.adapters.normalize import normalize_tescom_devices
from luminmind.adapters.retry import request_with_retry
from luminmind.core.schemas import DeviceMeta, PlantMeta, TelemetryPoint, Vendor

logger = logging.getLogger(__name__)

# Üreticinin geçmiş uç noktasının kabul ettiği en büyük kayıt sayısı.
HISTORY_LIMIT_MAX = 1000
# Bu kadar dakikalık boşluk normal çekim gecikmesi sayılır; üstü geri doldurulur.
# Eşik çekim aralığından bağımsız tutuldu: aralık 5 dakikaya indiğinde bile
# tek bir gecikmiş çevrim için cihaz başına geçmiş sorgusu atmak gereksiz.
BACKFILL_THRESHOLD_MIN = 10.0


@dataclass(frozen=True)
class FactorySite:
    """API'deki bir `fabrika_id`'nin LuminMind saha karşılığı."""

    plant_id: str  # zaman serisi anahtarı (Influx `plant_id` etiketi)
    name: str
    dc_capacity_kwp: float | None = None
    latitude: float | None = None
    longitude: float | None = None


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
        factories: dict[str, FactorySite] | None = None,
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
        self._factories = factories or {}

    @property
    def _plant_id_by_factory(self) -> dict[str, str]:
        return {key: site.plant_id for key, site in self._factories.items()}

    def _factory_of(self, vendor_plant_id: str) -> str | None:
        """Saha anahtarı → üreticinin `fabrika_id`'si (ör. tescom-izmir-uretim → uretim).

        Geçmiş uç noktası fabrikayı sorgu parametresi olarak istiyor; yazma
        yönündeki eşlemenin (`_plant_id_by_factory`) tersi gerekiyor. Fabrika
        yapılandırılmamış tek sahalı kurulumda None döner — o kurulumda geçmiş
        sorgusunun hangi fabrikayı isteyeceği belirsizdir.
        """
        return next(
            (
                key
                for key, site in self._factories.items()
                if site.plant_id == vendor_plant_id
            ),
            None,
        )

    def _sites(self) -> list[FactorySite]:
        """Yapılandırılmış sahalar; hiç fabrika tanımlı değilse tek sahaya düşer."""
        if self._factories:
            return list(self._factories.values())
        return [
            FactorySite(
                plant_id=self._plant_id,
                name=self._plant_name,
                dc_capacity_kwp=self._dc_capacity_kwp,
            )
        ]

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
        """Her fabrika ayrı bir saha olarak döner (tek fabrika yapılandırılmışsa tek)."""
        return [
            PlantMeta(
                vendor=Vendor.TESCOM,
                vendor_plant_id=site.plant_id,
                name=site.name,
                dc_capacity_kwp=site.dc_capacity_kwp,
                latitude=site.latitude if site.latitude is not None else self._latitude,
                longitude=site.longitude if site.longitude is not None else self._longitude,
            )
            for site in self._sites()
        ]

    async def fetch_devices(self, vendor_plant_id: str) -> list[DeviceMeta]:
        """Yalnız istenen sahanın cihazları — `slave_id` sahalar arasında tekrar eder."""
        points = normalize_tescom_devices(
            self._plant_id, await self._get_devices(), self._tz, self._plant_id_by_factory
        )
        seen: set[str] = set()
        devices: list[DeviceMeta] = []
        for point in points:
            if point.vendor_plant_id != vendor_plant_id or point.vendor_device_id is None:
                continue
            if point.vendor_device_id in seen:
                continue
            seen.add(point.vendor_device_id)
            devices.append(
                DeviceMeta(
                    vendor=Vendor.TESCOM,
                    vendor_plant_id=vendor_plant_id,
                    vendor_device_id=point.vendor_device_id,
                    model="Tescom Inverter",
                )
            )
        return devices

    async def _get_device_history(
        self, fabrika: str, device_id: str, limit: int
    ) -> list[dict[str, Any]]:
        """Tek cihazın son `limit` ölçümü (üretici yaklaşık dakikada bir kaydediyor).

        `fabrika` parametresi **zorunlu**: uç nokta yalnız `device_id` aldığında
        sunucu tarafında `mekanik`e düşüyor ve üretim fabrikasının 1 numaralı
        cihazı yerine mekaniğinki dönüyor.
        """
        response = await request_with_retry(
            self._client,
            "GET",
            f"/api/v1/devices/{device_id}/latest",
            backoff_base_s=self._backoff_base_s,
            headers=self._headers,
            params={"limit": limit, "fabrika": fabrika},
        )
        payload = response.json()
        if not isinstance(payload, list):
            logger.warning("Tescom /latest unexpected payload type: %s", type(payload))
            return []
        # Geçmiş kayıtlar cihazı tanımlamıyor (yalnız ölçüm alanları döner);
        # normalize edilebilmesi için kimlik alanları geri yazılır.
        return [
            {**row, "fabrika_id": fabrika, "id": device_id}
            for row in payload
            if isinstance(row, dict)
        ]

    async def _backfill(
        self,
        vendor_plant_id: str,
        fabrika: str,
        device_ids: "Sequence[str]",
        since: datetime,
        gap_minutes: float,
    ) -> list[TelemetryPoint]:
        """`since`'ten bu yana kaçırılmış ölçümleri geçmiş uç noktasından toplar."""
        limit = max(2, min(HISTORY_LIMIT_MAX, int(gap_minutes) + 5))
        collected: list[TelemetryPoint] = []
        for device_id in device_ids:
            try:
                rows = await self._get_device_history(fabrika, device_id, limit)
            except Exception:
                # Bir cihazın geçmişi alınamazsa diğerleri yine doldurulur;
                # anlık nokta zaten elde, tamamen boş dönmenin anlamı yok.
                logger.exception(
                    "Tescom history fetch failed plant=%s device=%s",
                    vendor_plant_id,
                    device_id,
                )
                continue
            collected.extend(
                normalize_tescom_devices(
                    self._plant_id, rows, self._tz, self._plant_id_by_factory
                )
            )
        return [
            p for p in collected if p.vendor_plant_id == vendor_plant_id and p.ts >= since
        ]

    async def fetch_telemetry(
        self, vendor_plant_id: str, since: datetime
    ) -> list[TelemetryPoint]:
        """`since`'ten bu yana tüm ölçümler — kaçırılan aralık geri doldurulur.

        Anlık `/devices` yanıtı cihaz başına **tek** nokta verir. Çekim
        durduğunda (host uykusu, container yeniden başlatma, ağ kesintisi) o
        aradaki dakikalar yalnız geçmiş uç noktasından alınabilir; eskiden hiç
        istenmediği için kalıcı olarak kayboluyordu.

        Anlık yanıt yine de çekilir: günlük enerji sayacını
        (`gunluk_uretim_kwh`) ve cihaz durumunu **sadece o** taşıyor.
        """
        payload = await self._get_devices()
        points = normalize_tescom_devices(
            self._plant_id, payload, self._tz, self._plant_id_by_factory
        )
        scoped = [p for p in points if p.vendor_plant_id == vendor_plant_id]
        live = [p for p in scoped if p.ts >= since]

        fabrika = self._factory_of(vendor_plant_id)
        newest = max((p.ts for p in scoped), default=datetime.now(tz=UTC))
        gap_minutes = (newest - since).total_seconds() / 60.0
        if fabrika is None or gap_minutes <= BACKFILL_THRESHOLD_MIN:
            return live

        device_ids = sorted(
            {p.vendor_device_id for p in scoped if p.vendor_device_id is not None}
        )
        history = await self._backfill(
            vendor_plant_id, fabrika, device_ids, since, gap_minutes
        )
        # Anlık nokta kazanır: sayaç ve durum alanları yalnız onda dolu.
        merged: dict[tuple[str | None, datetime], TelemetryPoint] = {
            (p.vendor_device_id, p.ts): p for p in history
        }
        merged.update({(p.vendor_device_id, p.ts): p for p in live})
        recovered = len(merged) - len(live)
        if recovered > 0:
            logger.info(
                "backfilled vendor=tescom plant=%s points=%d gap=%.0fmin",
                vendor_plant_id,
                recovered,
                gap_minutes,
            )
        return sorted(merged.values(), key=lambda p: (p.vendor_device_id or "", p.ts))
