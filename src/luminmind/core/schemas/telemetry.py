"""Kanonik LuminMind veri modeli.

Tüm üretici API yanıtları normalizasyon katmanında bu şemalara dönüştürülür;
platformun geri kalanı (Influx yazımı, dijital ikiz, karşılaştırma) yalnızca
bu modelleri tanır. Zaman damgaları her zaman timezone-aware olup UTC'de saklanır.
"""

from datetime import UTC, datetime
from enum import StrEnum

from pydantic import BaseModel, ConfigDict, field_validator


class Vendor(StrEnum):
    HUAWEI = "huawei"
    SMA = "sma"
    TESCOM = "tescom"
    MOCK = "mock"


class PlantMeta(BaseModel):
    """Üreticiden gelen tesis meta verisi (PostgreSQL PLANTS ile eşleşir)."""

    model_config = ConfigDict(frozen=True)

    vendor: Vendor
    vendor_plant_id: str
    name: str
    dc_capacity_kwp: float | None = None
    latitude: float | None = None
    longitude: float | None = None
    timezone: str = "Europe/Istanbul"


class DeviceMeta(BaseModel):
    """Tesis altındaki cihaz (invertör vb.) meta verisi."""

    model_config = ConfigDict(frozen=True)

    vendor: Vendor
    vendor_plant_id: str
    vendor_device_id: str
    model: str | None = None
    ac_capacity_kw: float | None = None


class TelemetryPoint(BaseModel):
    """Tek bir cihazın tek bir zaman damgasındaki ölçümleri.

    Alan adları InfluxDB `pv_telemetry` measurement field'larıyla birebir aynıdır.
    Üreticinin sağlamadığı alanlar None kalır ve Influx'a yazılmaz.
    """

    model_config = ConfigDict(frozen=True)

    vendor: Vendor
    vendor_plant_id: str
    vendor_device_id: str | None = None
    ts: datetime

    ac_power_kw: float | None = None
    dc_power_kw: float | None = None
    dc_voltage_v: float | None = None
    dc_current_a: float | None = None
    energy_total_kwh: float | None = None
    energy_daily_kwh: float | None = None
    temp_c: float | None = None
    # Cihaz sağlığı — üretici raporladıysa (Tescom `hata_kodu` / `durum`);
    # Influx'a yazılmaz, Postgres inverters.last_* alanlarına akar.
    error_code: str | None = None
    status: str | None = None

    @field_validator("ts")
    @classmethod
    def _ts_must_be_aware_utc(cls, value: datetime) -> datetime:
        if value.tzinfo is None:
            raise ValueError("timestamp must be timezone-aware")
        return value.astimezone(UTC)

    def measured_fields(self) -> dict[str, float]:
        """None olmayan ölçüm alanlarını döndürür (Influx field seti)."""
        fields = {
            "ac_power_kw": self.ac_power_kw,
            "dc_power_kw": self.dc_power_kw,
            "dc_voltage_v": self.dc_voltage_v,
            "dc_current_a": self.dc_current_a,
            "energy_total_kwh": self.energy_total_kwh,
            "energy_daily_kwh": self.energy_daily_kwh,
            "temp_c": self.temp_c,
        }
        return {name: value for name, value in fields.items() if value is not None}
