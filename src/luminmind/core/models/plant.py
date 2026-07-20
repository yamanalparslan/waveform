"""Tesis, cihaz ve kimlik bilgisi modelleri (PLAN.md §3.1 ER şeması)."""

import uuid
from datetime import datetime
from typing import TYPE_CHECKING, Any

from sqlalchemy import (
    JSON,
    DateTime,
    Float,
    ForeignKey,
    Integer,
    LargeBinary,
    String,
    UniqueConstraint,
    Uuid,
    func,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from luminmind.core.models.base import Base

if TYPE_CHECKING:
    from luminmind.core.models.auth import User
    from luminmind.core.models.events import ArbitragePlan


class Plant(Base):
    __tablename__ = "plants"
    __table_args__ = (UniqueConstraint("vendor", "vendor_plant_id"),)

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    owner_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("users.id"), index=True)
    name: Mapped[str] = mapped_column(String(200))
    vendor: Mapped[str] = mapped_column(String(20))  # Vendor enum değeri
    vendor_plant_id: Mapped[str] = mapped_column(String(100))
    latitude: Mapped[float | None] = mapped_column(Float)
    longitude: Mapped[float | None] = mapped_column(Float)
    dc_capacity_kwp: Mapped[float | None] = mapped_column(Float)
    ac_capacity_kw: Mapped[float | None] = mapped_column(Float)
    timezone: Mapped[str] = mapped_column(String(50), default="Europe/Istanbul")
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )

    owner: Mapped["User"] = relationship(back_populates="plants")
    inverters: Mapped[list["Inverter"]] = relationship(
        back_populates="plant", cascade="all, delete-orphan"
    )
    pv_arrays: Mapped[list["PvArray"]] = relationship(
        back_populates="plant", cascade="all, delete-orphan"
    )
    batteries: Mapped[list["BatterySystem"]] = relationship(
        back_populates="plant", cascade="all, delete-orphan"
    )
    credential: Mapped["VendorCredential | None"] = relationship(
        back_populates="plant", cascade="all, delete-orphan"
    )


class Inverter(Base):
    __tablename__ = "inverters"
    __table_args__ = (UniqueConstraint("plant_id", "vendor_device_id"),)

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    plant_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("plants.id"), index=True)
    vendor_device_id: Mapped[str] = mapped_column(String(100))
    model: Mapped[str | None] = mapped_column(String(100))  # ör. Schneider CL-60E
    ac_capacity_kw: Mapped[float | None] = mapped_column(Float)
    efficiency_curve: Mapped[dict[str, Any] | None] = mapped_column(JSON)

    plant: Mapped[Plant] = relationship(back_populates="inverters")


class PvArray(Base):
    __tablename__ = "pv_arrays"

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    plant_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("plants.id"), index=True)
    inverter_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("inverters.id"))
    modules_per_string: Mapped[int] = mapped_column(Integer)
    strings: Mapped[int] = mapped_column(Integer)
    tilt_deg: Mapped[float] = mapped_column(Float)
    azimuth_deg: Mapped[float] = mapped_column(Float)
    # Panel datasheet parametreleri: pdc0, gamma_pdc, NOCT… (pvlib girdisi, Faz 3)
    module_params: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)

    plant: Mapped[Plant] = relationship(back_populates="pv_arrays")


class VendorCredential(Base):
    __tablename__ = "vendor_credentials"

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    plant_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("plants.id"), unique=True)
    vendor: Mapped[str] = mapped_column(String(20))
    auth_type: Mapped[str] = mapped_column(String(20))  # oauth2 | session
    # Kimlik bilgisi JSON'u Fernet ile şifrelenir (core/security.py) — düz metin asla
    encrypted_payload: Mapped[bytes] = mapped_column(LargeBinary)
    token_expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    plant: Mapped[Plant] = relationship(back_populates="credential")


class BatterySystem(Base):
    __tablename__ = "battery_systems"

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    plant_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("plants.id"), index=True)
    chemistry: Mapped[str] = mapped_column(String(50), default="NMC-21700")
    cells_series: Mapped[int] = mapped_column(Integer)
    cells_parallel: Mapped[int] = mapped_column(Integer)
    pack_count: Mapped[int] = mapped_column(Integer, default=1)
    rated_energy_kwh: Mapped[float] = mapped_column(Float)
    rated_power_kw: Mapped[float] = mapped_column(Float)
    # EKF/eşdeğer devre parametreleri: R0, R1, C1, OCV-SoC eğrisi (Faz 4 kalibrasyonu yazar)
    model_params: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)

    plant: Mapped[Plant] = relationship(back_populates="batteries")
    arbitrage_plans: Mapped[list["ArbitragePlan"]] = relationship(
        back_populates="battery", cascade="all, delete-orphan"
    )
