"""Zaman serisi anahtarı → Postgres kayıtları çözümlemesi.

InfluxDB'de `plant_id` etiketi bir UUID değil serbest bir dizedir. Saha
hiyerarşisine geçtikten sonra bu dize **sahanın** anahtarıdır (`Site.series_key`),
tesisin değil. Ölçüm yazan/okuyan her katman (ingestion, cihaz sağlığı, anomali,
doğruluk, kalibrasyon) aynı çözümlemeyi yapmak zorunda; kopyalanırsa biri
güncellenmeyi unutulur ve o katman sessizce çalışmayı bırakır — göç sırasında
tam olarak bu oldu.

Geriye uyum: saha bulunamazsa eski davranışa düşülür ve anahtar `Plant`'ın
`vendor_plant_id`'si olarak aranır. Böylece sahası olmayan (mock, Huawei, SMA)
kurulumlar etkilenmez.
"""

import logging
from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from luminmind.core.models import Plant, Site

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class SeriesTarget:
    """Bir zaman serisi anahtarının sahibi: tesis ve (varsa) saha."""

    plant: Plant
    site: Site | None

    @property
    def series_key(self) -> str:
        return self.site.series_key if self.site else self.plant.vendor_plant_id

    @property
    def display_name(self) -> str:
        return f"{self.plant.name} · {self.site.name}" if self.site else self.plant.name

    @property
    def capacity_kwp(self) -> float | None:
        """DC kurulu güç (kWp) — dijital ikizin dizi türetmesi bunu kullanır."""
        if self.site is not None:
            return self.site.dc_capacity_kwp or self.plant.dc_capacity_kwp
        return self.plant.dc_capacity_kwp

    @property
    def capacity_kw(self) -> float | None:
        """Normalizasyon kapasitesi: AC anma gücü, yoksa DC.

        nMAE/nRMSE ölçülen **AC** gücü normalize eder ve AC güç invertör anma
        gücünü aşamaz; DC kurulu güce bölmek hatayı DC/AC oranı kadar (tipik
        %20–25) olduğundan küçük gösterir.
        """
        if self.site is not None:
            ac = self.site.ac_capacity_kw or self.plant.ac_capacity_kw
        else:
            ac = self.plant.ac_capacity_kw
        return ac or self.capacity_kwp


async def resolve_series_key(session: AsyncSession, series_key: str) -> SeriesTarget | None:
    """Influx `plant_id` etiketinden tesis + saha çözümler; bulunamazsa None."""
    site = (
        await session.scalars(select(Site).where(Site.series_key == series_key))
    ).one_or_none()
    if site is not None:
        plant = await session.get(Plant, site.plant_id)
        if plant is not None:
            return SeriesTarget(plant=plant, site=site)

    plant = (
        await session.scalars(select(Plant).where(Plant.vendor_plant_id == series_key))
    ).one_or_none()
    if plant is not None:
        return SeriesTarget(plant=plant, site=None)
    return None


async def all_series_targets(session: AsyncSession) -> list[SeriesTarget]:
    """İzlenen tüm seriler: her sahaya bir hedef, sahasız tesislere kendileri."""
    targets: list[SeriesTarget] = []
    plants = (await session.scalars(select(Plant).order_by(Plant.name))).all()
    for plant in plants:
        sites = await plant.awaitable_attrs.sites
        if sites:
            targets.extend(SeriesTarget(plant=plant, site=site) for site in sites)
        else:
            targets.append(SeriesTarget(plant=plant, site=None))
    return targets


async def series_capacities(session: AsyncSession) -> dict[str, float]:
    """Seri anahtarı → normalizasyon kapasitesi (kW). Kapasitesizler dışarıda kalır."""
    capacities: dict[str, float] = {}
    for target in await all_series_targets(session):
        capacity = target.capacity_kw
        if capacity:
            capacities[target.series_key] = float(capacity)
    return capacities
