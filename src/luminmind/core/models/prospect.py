"""Kurulum öncesi fizibilite: tasarım ve rapor kayıtları.

İki tablo var çünkü iki farklı ömür var. **Tasarım** kullanıcının girdisidir
(poligon, eğim, montaj tipi, ekipman) ve düzenlenebilir. **Rapor** o tasarımın
belirli bir andaki hesap sonucudur ve dondurulmuştur: EPC bir teklif verdiğinde
altındaki sayının değişmemesi gerekir.

Bu yüzden rapor, tasarımı işaret etmekle yetinmez — hesabın *tüm girdilerini*
kendi içinde saklar (`assumptions`: kayıp zinciri, maliyet, tarife, iskonto;
`layout`: panel köşeleri). Tasarım sonradan değiştirilse ya da varsayılan
tarife/modül fiyatı güncellense bile eski rapor aynı sayıyı üretmeye devam
eder. Aksi halde "geçen ay 4,7 M₺ NPV demiştiniz" sorusunun cevabı olmazdı.

`layout` alanı panel dikdörtgenlerini **WGS84** olarak taşır. Yerel metrik
çerçeve (`prospect.geometry.LocalFrame`) poligonun kendi merkezine oturduğu
için yeniden üretilebilir olsa da, çizim için her seferinde yerleşim
algoritmasını koşturmak gereksiz; rapor sayfası saniyeler değil milisaniyeler
içinde açılmalı.
"""

import uuid
from datetime import datetime
from typing import TYPE_CHECKING, Any

from sqlalchemy import (
    JSON,
    DateTime,
    Float,
    ForeignKey,
    Integer,
    String,
    Uuid,
    func,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from luminmind.core.models.base import Base

if TYPE_CHECKING:
    from luminmind.core.models.auth import User


class ProspectStatus:
    """`ProspectDesign.status` değerleri.

    StrEnum yerine sabitler: SQLAlchemy sütunu `String` ve migration'da
    `CHECK` kısıtı yok — durum kümesi ürün geliştikçe büyüyecek ve her
    eklemede migration yazmak istemiyoruz.
    """

    DRAFT = "draft"  # poligon çizildi, hesap yapılmadı
    ANALYSED = "analysed"  # en az bir rapor üretildi
    QUOTED = "quoted"  # EPC teklife dönüştürdü
    WON = "won"  # iş alındı — kurulunca Plant kaydına bağlanır
    LOST = "lost"


class ProspectDesign(Base):
    """Kurulmamış bir santralin tasarım girdisi."""

    __tablename__ = "prospect_designs"

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    owner_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("users.id"), index=True)
    name: Mapped[str] = mapped_column(String(200))
    customer: Mapped[str | None] = mapped_column(String(200))  # EPC'nin müşterisi
    status: Mapped[str] = mapped_column(String(20), default=ProspectStatus.DRAFT, index=True)

    # Konum — poligonun merkezi. TMY ve güneş geometrisi buradan çekilir.
    latitude: Mapped[float] = mapped_column(Float)
    longitude: Mapped[float] = mapped_column(Float)

    # Çatı/arazi sınırı: [[lat, lon], …]. Kapanış noktası saklanmaz.
    polygon: Mapped[list[list[float]]] = mapped_column(JSON)
    # Engeller (baca, çatı penceresi, klima): [[[lat, lon], …], …]
    obstacles: Mapped[list[list[list[float]]]] = mapped_column(JSON, default=list)

    # Montaj — `prospect.layout.MountingSpec` alanlarıyla eşleşir
    mount_type: Mapped[str] = mapped_column(String(30), default="rooftop_tilted")
    tilt_deg: Mapped[float] = mapped_column(Float, default=15.0)
    azimuth_deg: Mapped[float] = mapped_column(Float, default=180.0)
    setback_m: Mapped[float] = mapped_column(Float, default=0.6)
    obstacle_clearance_m: Mapped[float] = mapped_column(Float, default=0.5)
    # Boşsa gölgeleme ölçütünden türetilir (bkz. layout.required_row_pitch)
    row_pitch_m: Mapped[float | None] = mapped_column(Float)

    # Ekipman anlık görüntüsü — `ModuleSpec` / `InverterSpec` alan adlarıyla.
    # Varsayılanlar değişse bile tasarımın kullandığı modül sabit kalır.
    module_spec: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    inverter_spec: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)

    # Kurulduysa gerçek santral kaydı — fizibiliteyi gerçekleşmeyle
    # karşılaştırmanın tek bağlantısı. Silinen santral tasarımı düşürmez.
    plant_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("plants.id", ondelete="SET NULL"), index=True
    )

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )

    owner: Mapped["User"] = relationship()
    reports: Mapped[list["ProspectReport"]] = relationship(
        back_populates="design",
        cascade="all, delete-orphan",
        order_by="ProspectReport.computed_at.desc()",
    )

    @property
    def latest_report(self) -> "ProspectReport | None":
        """En güncel rapor; `reports` zaten tarihe göre azalan sıralı."""
        return self.reports[0] if self.reports else None


class ProspectReport(Base):
    """Bir tasarımın dondurulmuş hesap sonucu.

    Skaler göstergeler ayrı sütunlarda duruyor (JSON içinde değil) çünkü
    portföy listesinde "NPV'ye göre sırala" ya da "geri ödemesi 5 yıldan kısa
    olanlar" sorgusu SQL'de çalışmak zorunda; JSON alanından süzmek her satırı
    Python'a çekmek demekti.
    """

    __tablename__ = "prospect_reports"

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    design_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("prospect_designs.id", ondelete="CASCADE"), index=True
    )
    computed_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), index=True
    )
    # Hangi sürümün ürettiği — hesap değişince eski raporlar ayırt edilebilsin
    engine_version: Mapped[str] = mapped_column(String(30), default="prospect-v1")
    # Veri kaynağı künyesi (`TmyDataset.provenance`)
    data_provenance: Mapped[str] = mapped_column(String(300), default="")

    # --- Yerleşim ---
    module_count: Mapped[int] = mapped_column(Integer)
    dc_capacity_kwp: Mapped[float] = mapped_column(Float)
    ac_capacity_kw: Mapped[float] = mapped_column(Float)
    area_m2: Mapped[float] = mapped_column(Float)
    row_pitch_m: Mapped[float] = mapped_column(Float)
    gcr: Mapped[float] = mapped_column(Float)
    orientation: Mapped[str] = mapped_column(String(20))
    modules_per_string: Mapped[int] = mapped_column(Integer)
    strings: Mapped[int] = mapped_column(Integer)
    inverter_count: Mapped[int] = mapped_column(Integer)

    # --- Üretim ---
    year_one_kwh: Mapped[float] = mapped_column(Float)
    specific_yield_kwh_kwp: Mapped[float] = mapped_column(Float)
    performance_ratio: Mapped[float] = mapped_column(Float)
    poa_kwh_m2: Mapped[float] = mapped_column(Float)
    ghi_kwh_m2: Mapped[float] = mapped_column(Float)
    lifetime_kwh: Mapped[float] = mapped_column(Float)
    p90_year_one_kwh: Mapped[float] = mapped_column(Float)

    # --- Fizibilite (₺, reel) ---
    capex_try: Mapped[float] = mapped_column(Float)
    npv_try: Mapped[float] = mapped_column(Float)
    # IRR tanımsız olabilir (hiçbir oranda başa baş gelmiyor) — 0 yazmak
    # "getirisi yok" gibi okunurdu, bu yüzden nullable.
    irr_real: Mapped[float | None] = mapped_column(Float)
    lcoe_try_kwh: Mapped[float] = mapped_column(Float)
    payback_years: Mapped[float | None] = mapped_column(Float)

    # --- Ayrıntı (grafik ve yeniden çizim için) ---
    # Panel dikdörtgenleri WGS84: [[[lat, lon] ×4], …]
    layout: Mapped[list[list[list[float]]]] = mapped_column(JSON, default=list)
    monthly_kwh: Mapped[list[float]] = mapped_column(JSON, default=list)
    waterfall: Mapped[list[dict[str, Any]]] = mapped_column(JSON, default=list)
    projection: Mapped[list[dict[str, Any]]] = mapped_column(JSON, default=list)
    # Kayıp zinciri, maliyet, tarife, iskonto — raporu yeniden üretilebilir kılan
    assumptions: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    # Tasarımın uyarıları: MPPT eksikliği, kırpılan panel, öz-kesişen poligon…
    warnings: Mapped[list[str]] = mapped_column(JSON, default=list)

    design: Mapped[ProspectDesign] = relationship(back_populates="reports")
