"""Dijital ikiz çıktısının kanonik modeli (Influx `twin_expected` ile eşleşir)."""

from datetime import UTC, datetime

from pydantic import BaseModel, ConfigDict, field_validator

TWIN_MODEL_VERSION = "pvwatts-v1"


class TwinPoint(BaseModel):
    """Bir tesisin bir zaman damgası için beklenen (teorik) üretimi."""

    model_config = ConfigDict(frozen=True)

    plant_id: str
    ts: datetime
    expected_ac_kw: float
    poa_irradiance_wm2: float | None = None
    cell_temp_c: float | None = None
    model_version: str = TWIN_MODEL_VERSION

    @field_validator("ts")
    @classmethod
    def _ts_must_be_aware_utc(cls, value: datetime) -> datetime:
        if value.tzinfo is None:
            raise ValueError("timestamp must be timezone-aware")
        return value.astimezone(UTC)
