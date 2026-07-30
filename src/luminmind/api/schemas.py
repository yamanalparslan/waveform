"""API istek/yanıt şemaları (PLAN.md §4 sözleşmesi)."""

import uuid
from datetime import date, datetime
from typing import Any, Literal

from pydantic import BaseModel, Field


class LoginRequest(BaseModel):
    # EmailStr yerine str: email-validator bağımlılığı eklememek için (format
    # doğrulaması kayıt akışı geldiğinde ele alınır)
    email: str
    password: str


class TokenPair(BaseModel):
    access_token: str
    refresh_token: str
    token_type: Literal["bearer"] = "bearer"


class AccessToken(BaseModel):
    access_token: str
    token_type: Literal["bearer"] = "bearer"


class RefreshRequest(BaseModel):
    refresh_token: str


class UserOut(BaseModel):
    id: uuid.UUID
    email: str
    role: str

class InverterOut(BaseModel):
    id: uuid.UUID
    vendor_device_id: str
    model: str | None
    ac_capacity_kw: float | None


class BatteryOut(BaseModel):
    id: uuid.UUID
    chemistry: str
    cells_series: int
    cells_parallel: int
    pack_count: int
    rated_energy_kwh: float
    rated_power_kw: float
    model_params: dict[str, Any]


class PlantSummary(BaseModel):
    id: uuid.UUID
    name: str
    vendor: str
    vendor_plant_id: str
    dc_capacity_kwp: float | None
    ac_capacity_kw: float | None
    timezone: str


class PlantDetail(PlantSummary):
    latitude: float | None
    longitude: float | None
    inverters: list[InverterOut]
    batteries: list[BatteryOut]


class PlantCreate(BaseModel):
    name: str
    vendor: str
    vendor_plant_id: str
    latitude: float | None = None
    longitude: float | None = None
    dc_capacity_kwp: float | None = None
    ac_capacity_kw: float | None = None
    timezone: str = "Europe/Istanbul"


class SeriesPoint(BaseModel):
    ts: datetime
    value: float


class ComparisonPoint(BaseModel):
    ts: datetime
    actual_kw: float | None
    expected_kw: float
    deviation_pct: float | None


class AnomalyOut(BaseModel):
    id: uuid.UUID
    kind: str
    severity: str
    deviation_pct: float
    started_at: datetime
    ended_at: datetime | None
    status: str
    evidence: dict[str, Any]


class AnomalyPatch(BaseModel):
    status: Literal["open", "acked", "resolved"]


class PriceOut(BaseModel):
    ts: datetime
    price_try_mwh: float


class ArbitrageSlotOut(BaseModel):
    slot_start: datetime
    action: str
    power_kw: float
    price_try_mwh: float


class ArbitragePlanOut(BaseModel):
    id: uuid.UUID
    battery_id: uuid.UUID
    plan_date: date
    market: str
    expected_revenue_try: float
    slots: list[ArbitrageSlotOut]


class HealthOut(BaseModel):
    status: str
    postgres: bool
    influx_configured: bool
    version: str = Field(default="0.1.0")
