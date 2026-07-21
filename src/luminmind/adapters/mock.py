"""Mock üretici adaptörü.

Gerçek üretici kimlik bilgileri sağlanana kadar (PLAN.md: mock-first kararı)
uçtan uca dilimi çalıştırmak için deterministik sentetik üretim verisi üretir.
HTTP çağrısı yapmaz; `VendorAdapter` arayüzünü birebir uygular, dolayısıyla
ingestion görevi mock ile gerçek adaptör arasında hiçbir fark görmez.

Üretim eğrisi: 06:00–20:00 (UTC+3 varsayımıyla 03:00–17:00 UTC) arasında
sinüs yarım dalgası; gece sıfır. Gürültü yok — testler deterministik kalır.
"""

import math
from datetime import UTC, datetime, timedelta
from typing import ClassVar

from luminmind.adapters.base import VendorAdapter
from luminmind.core.schemas import DeviceMeta, PlantMeta, TelemetryPoint, Vendor

_SUNRISE_UTC_H = 3.0
_SUNSET_UTC_H = 17.0
_INTERVAL = timedelta(minutes=15)

_PLANTS = [
    PlantMeta(
        vendor=Vendor.MOCK,
        vendor_plant_id="mock-plant-1",
        name="Konya GES",
        dc_capacity_kwp=1000.0,
        latitude=37.87,
        longitude=32.48,
    ),
]
_DEVICES_PER_PLANT = 4


def _clear_sky_factor(ts: datetime) -> float:
    """0..1 arası üretim çarpanı (gündüz sinüs yarım dalgası, gece 0)."""
    hour = ts.astimezone(UTC).hour + ts.minute / 60
    if not (_SUNRISE_UTC_H <= hour <= _SUNSET_UTC_H):
        return 0.0
    return math.sin(math.pi * (hour - _SUNRISE_UTC_H) / (_SUNSET_UTC_H - _SUNRISE_UTC_H))


class MockAdapter(VendorAdapter):
    vendor: ClassVar[Vendor] = Vendor.MOCK

    def __init__(self, now: datetime | None = None) -> None:
        super().__init__(base_url="http://mock.invalid")
        self._now = now

    async def authenticate(self) -> None:
        return None

    async def fetch_plants(self) -> list[PlantMeta]:
        return list(_PLANTS)

    async def fetch_devices(self, vendor_plant_id: str) -> list[DeviceMeta]:
        return [
            DeviceMeta(
                vendor=Vendor.MOCK,
                vendor_plant_id=vendor_plant_id,
                vendor_device_id=f"{vendor_plant_id}-inv-{i:02d}",
                model="Mock String Inverter 250kW",
                ac_capacity_kw=250.0,
            )
            for i in range(1, _DEVICES_PER_PLANT + 1)
        ]

    async def fetch_telemetry(
        self, vendor_plant_id: str, since: datetime
    ) -> list[TelemetryPoint]:
        plant = next(p for p in _PLANTS if p.vendor_plant_id == vendor_plant_id)
        capacity_kwp = plant.dc_capacity_kwp or 0.0
        devices = await self.fetch_devices(vendor_plant_id)
        now = self._now or datetime.now(tz=UTC)

        # since sonrası ilk 15 dk hizalı zaman damgasından başla
        first_slot_s = math.ceil(since.timestamp() / _INTERVAL.seconds) * _INTERVAL.seconds
        ts = datetime.fromtimestamp(first_slot_s, tz=UTC)

        points: list[TelemetryPoint] = []
        while ts <= now:
            factor = _clear_sky_factor(ts)
            per_device_kw = capacity_kwp * factor / len(devices)
            for device in devices:
                points.append(
                    TelemetryPoint(
                        vendor=Vendor.MOCK,
                        vendor_plant_id=vendor_plant_id,
                        vendor_device_id=device.vendor_device_id,
                        ts=ts,
                        ac_power_kw=round(per_device_kw, 3),
                        dc_power_kw=round(per_device_kw * 1.03, 3),
                        dc_voltage_v=0.0 if factor == 0 else 620.0,
                        dc_current_a=0.0 if factor == 0 else round(per_device_kw * 1.03 / 0.62, 3),
                        temp_c=25.0 + 15.0 * factor,
                    )
                )
            ts += _INTERVAL
        return points
