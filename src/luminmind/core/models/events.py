"""Anomali olayları ve arbitraj planları (Faz 5'in yazacağı tablolar)."""

import uuid
from datetime import date, datetime
from typing import Any

from sqlalchemy import (
    JSON,
    Date,
    DateTime,
    Float,
    ForeignKey,
    Index,
    String,
    UniqueConstraint,
    Uuid,
    func,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from luminmind.core.models.base import Base
from luminmind.core.models.plant import BatterySystem


class AnomalyEvent(Base):
    __tablename__ = "anomaly_events"
    __table_args__ = (Index("ix_anomaly_events_plant_started", "plant_id", "started_at"),)

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    plant_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("plants.id"))
    kind: Mapped[str] = mapped_column(String(30))  # microcrack | shading | soiling
    severity: Mapped[str] = mapped_column(String(20), default="warning")
    deviation_pct: Mapped[float] = mapped_column(Float)
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    ended_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    status: Mapped[str] = mapped_column(String(20), default="open")  # open | acked | resolved
    evidence: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )


class ArbitragePlan(Base):
    __tablename__ = "arbitrage_plans"
    __table_args__ = (UniqueConstraint("battery_id", "plan_date", "market"),)

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    battery_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("battery_systems.id"))
    plan_date: Mapped[date] = mapped_column(Date)
    market: Mapped[str] = mapped_column(String(10))  # DAM | IDM
    expected_revenue_try: Mapped[float] = mapped_column(Float, default=0.0)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )

    battery: Mapped[BatterySystem] = relationship(back_populates="arbitrage_plans")
    slots: Mapped[list["ArbitrageSlot"]] = relationship(
        back_populates="plan", cascade="all, delete-orphan", order_by="ArbitrageSlot.slot_start"
    )


class ArbitrageSlot(Base):
    __tablename__ = "arbitrage_slots"

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    plan_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("arbitrage_plans.id"), index=True)
    slot_start: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    action: Mapped[str] = mapped_column(String(10))  # charge | discharge | idle
    power_kw: Mapped[float] = mapped_column(Float, default=0.0)
    price_try_mwh: Mapped[float] = mapped_column(Float)

    plan: Mapped[ArbitragePlan] = relationship(back_populates="slots")
