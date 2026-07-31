"""Tesis, cihaz ve kimlik bilgisi modelleri (PLAN.md §3.1 ER şeması)."""

import uuid
from datetime import date, datetime
from typing import TYPE_CHECKING, Any

from sqlalchemy import (
    JSON,
    Date,
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
    # Yaşam döngüsü: `operational` (kurulu, veri akıyor) | `prospect` (fizibilite
    # aşamasında) | `construction` (kurulumda). İzleme sorgularının kurulmamış
    # santralleri süzebilmesi için var. Varsayılan `operational`, çünkü bu alan
    # eklenmeden önce kaydedilmiş her santral kuruludur — tersi varsayım mevcut
    # tesisleri panelden düşürürdü.
    status: Mapped[str] = mapped_column(String(20), default="operational", index=True)
    vendor: Mapped[str] = mapped_column(String(20))  # Vendor enum değeri
    vendor_plant_id: Mapped[str] = mapped_column(String(100))
    latitude: Mapped[float | None] = mapped_column(Float)
    longitude: Mapped[float | None] = mapped_column(Float)
    dc_capacity_kwp: Mapped[float | None] = mapped_column(Float)
    ac_capacity_kw: Mapped[float | None] = mapped_column(Float)
    # Şebekeye satış tarifesi (₺/kWh) — arayüzdeki parasal karşılıklar bundan hesaplanır.
    # Boşsa Settings.default_tariff_try_kwh kullanılır (web/advice.py).
    feed_in_tariff_try_kwh: Mapped[float | None] = mapped_column(Float)
    # Rakım (m) — hava yoğunluğu üzerinden mutlak hava kütlesini ve spektral
    # düzeltmeyi etkiler; boşsa deniz seviyesi varsayılır.
    altitude_m: Mapped[float | None] = mapped_column(Float)
    # Devreye alma tarihi — yaşa bağlı bozunum (≈%0,5/yıl) buradan türetilir.
    commissioned_on: Mapped[date | None] = mapped_column(Date)
    # Bağlantı anlaşmasındaki azami şebekeye verme gücü (kW). Arbitraj LP'si
    # bunu kısıt olarak kullanır; boşsa AC kapasite sınır kabul edilir.
    grid_export_limit_kw: Mapped[float | None] = mapped_column(Float)
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
    calibrations: Mapped[list["TwinCalibration"]] = relationship(
        back_populates="plant",
        cascade="all, delete-orphan",
        order_by="TwinCalibration.fitted_at.desc()",
    )
    sites: Mapped[list["Site"]] = relationship(
        back_populates="plant",
        cascade="all, delete-orphan",
        order_by="Site.display_order",
    )

    @property
    def total_dc_capacity_kwp(self) -> float | None:
        """Sahaların toplam kurulu gücü; saha yoksa tesisin kendi değeri."""
        site_total = sum(s.dc_capacity_kwp or 0.0 for s in self.sites)
        if site_total > 0:
            return site_total
        return self.dc_capacity_kwp


class Inverter(Base):
    __tablename__ = "inverters"
    # Tekillik saha bazında: `vendor_device_id` (Tescom `slave_id`) yalnızca
    # fabrika içinde tekildir — hem Üretim hem Mekanik fabrikasında 1 numaralı
    # cihaz var. Tesis bazlı kısıt bu ikisini aynı kayda zorlardı.
    __table_args__ = (UniqueConstraint("site_id", "vendor_device_id"),)

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    plant_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("plants.id"), index=True)
    site_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("sites.id"), index=True)
    vendor_device_id: Mapped[str] = mapped_column(String(100))
    model: Mapped[str | None] = mapped_column(String(100))  # ör. Schneider CL-60E
    ac_capacity_kw: Mapped[float | None] = mapped_column(Float)
    efficiency_curve: Mapped[dict[str, Any] | None] = mapped_column(JSON)

    # Sağlık durumu cache'i — Influx zaman serisinden özetlenip yazılır (inverter_sync
    # görevi tarafından güncellenir). Kaynak yine InfluxDB, bu alanlar hızlı okuma için.
    last_seen_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_power_kw: Mapped[float | None] = mapped_column(Float)
    last_temp_c: Mapped[float | None] = mapped_column(Float)
    last_error_code: Mapped[str | None] = mapped_column(String(30))
    last_status: Mapped[str | None] = mapped_column(String(30))  # üreticiden gelen ham durum

    plant: Mapped[Plant] = relationship(back_populates="inverters")
    site: Mapped["Site | None"] = relationship(back_populates="inverters")
    pv_arrays: Mapped[list["PvArray"]] = relationship(back_populates="inverter")


class PvArray(Base):
    __tablename__ = "pv_arrays"

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    plant_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("plants.id"), index=True)
    site_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("sites.id"), index=True)
    inverter_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("inverters.id"))
    modules_per_string: Mapped[int] = mapped_column(Integer)
    strings: Mapped[int] = mapped_column(Integer)
    tilt_deg: Mapped[float] = mapped_column(Float)
    azimuth_deg: Mapped[float] = mapped_column(Float)
    # Panel datasheet parametreleri: pdc0, gamma_pdc, NOCT… (pvlib girdisi, Faz 3)
    module_params: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)

    # Montaj geometrisi — sıra-arası gölgelenme ve termal model bunlara bağlı
    mount_type: Mapped[str] = mapped_column(String(30), default="fixed_ground")
    gcr: Mapped[float | None] = mapped_column(Float)  # kollektör genişliği / sıra aralığı
    albedo: Mapped[float | None] = mapped_column(Float)
    bifaciality: Mapped[float | None] = mapped_column(Float)
    module_type: Mapped[str | None] = mapped_column(String(20))  # monosi | polysi | cdte…

    plant: Mapped[Plant] = relationship(back_populates="pv_arrays")
    site: Mapped["Site | None"] = relationship(back_populates="pv_arrays")
    # Dizinin bağlı olduğu invertör — kırpma seviyesi buranın AC anma gücünden gelir
    inverter: Mapped["Inverter | None"] = relationship(back_populates="pv_arrays")


class Site(Base):
    """Bir tesisin alt sahası (Tescom'da fabrika).

    **`series_key` bu modelin varlık sebebi.** InfluxDB'de `plant_id` etiketi
    bir Postgres UUID değil, serbest bir dizedir; geçmiş veri bugüne kadar
    `tescom-izmir-uretim` / `tescom-izmir-mekanik` dizeleriyle yazıldı. Zaman
    serisi anahtarını sahada saklayınca tek bir nokta taşımadan hiyerarşi
    kurulabiliyor: Postgres'te "tek tesis, iki saha", Influx'ta ise seriler
    olduğu yerde kalıyor.

    Bu yüzden `series_key` **global tekil**: iki saha aynı anahtarı alırsa
    ölçümleri aynı seriye yazılır ve biri diğerini ezer — düzeltmekte olduğumuz
    hatanın aynısı. Kısıt bunu veritabanı seviyesinde imkânsız kılar.

    `code` üreticideki kimliktir (`fabrika_id`) ve adaptör eşlemesini sağlar;
    `series_key`'den ayrı tutulur ki sahanın adı/kodu değişse bile geçmiş seri
    kopmasın.
    """

    __tablename__ = "sites"
    __table_args__ = (
        UniqueConstraint("series_key", name="uq_sites_series_key"),
        UniqueConstraint("plant_id", "code", name="uq_sites_plant_id_code"),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    plant_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("plants.id"), index=True)
    name: Mapped[str] = mapped_column(String(120))
    code: Mapped[str] = mapped_column(String(60))  # üreticideki fabrika_id
    series_key: Mapped[str] = mapped_column(String(120))  # Influx `plant_id` etiketi

    dc_capacity_kwp: Mapped[float | None] = mapped_column(Float)
    ac_capacity_kw: Mapped[float | None] = mapped_column(Float)
    latitude: Mapped[float | None] = mapped_column(Float)  # boşsa tesisten miras
    longitude: Mapped[float | None] = mapped_column(Float)
    altitude_m: Mapped[float | None] = mapped_column(Float)
    commissioned_on: Mapped[date | None] = mapped_column(Date)
    feed_in_tariff_try_kwh: Mapped[float | None] = mapped_column(Float)
    grid_export_limit_kw: Mapped[float | None] = mapped_column(Float)
    display_order: Mapped[int] = mapped_column(Integer, default=0)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )

    plant: Mapped[Plant] = relationship(back_populates="sites")
    inverters: Mapped[list["Inverter"]] = relationship(back_populates="site")
    pv_arrays: Mapped[list["PvArray"]] = relationship(back_populates="site")


class TwinCalibration(Base):
    """Dijital ikizin tesis bazlı kalibrasyon durumu (geçmişiyle birlikte).

    Her fit yeni bir satır yazar; model her zaman en güncel satırı okur.
    Geçmiş korunur çünkü "model ne zaman neyi varsaydı" sorusu bir arıza
    tartışmasında kritik hale gelir.
    """

    __tablename__ = "twin_calibrations"
    # Kalibrasyon saha bazlı öğrenilir (her fabrikanın kendi kapasitesi, yönelimi
    # ve kirlilik geçmişi var), bu yüzden tekillik sahaya bağlıdır. Tesise
    # bağlamak, iki sahanın aynı anda kalibre olmasını imkânsız kılardı.
    __table_args__ = (UniqueConstraint("site_id", "fitted_at"),)

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    plant_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("plants.id"), index=True)
    site_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("sites.id"), index=True)
    fitted_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    scale: Mapped[float] = mapped_column(Float, default=1.0)
    soiling_base_ratio: Mapped[float] = mapped_column(Float, default=1.0)
    hour_bias: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    sample_count: Mapped[int] = mapped_column(Integer, default=0)
    quality: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    version: Mapped[str] = mapped_column(String(30), default="calib-v1")
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )

    plant: Mapped[Plant] = relationship(back_populates="calibrations")


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
