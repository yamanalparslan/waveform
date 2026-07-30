"""Tesis/saha yapısını kurar ve eski düz tesisleri bu yapıya emer.

    python -m luminmind.scripts.bootstrap_sites [--drop-mock] [--dry-run]

Neden ayrı bir betik: şema değişikliği (alembic) ile veri göçü bilinçli olarak
ayrılır. Migration boş bir kuruluma da dolu bir kuruluma da güvenle uygulanır;
veriye dokunan kısım burada, tekrar çalıştırılabilir (idempotent) biçimde durur.

Yaptığı iş:

1. Ana tesisi (`Tescom UPS İzmir`) bulur/oluşturur.
2. `Settings.tescom_factory_sites` eşlemesinden sahaları upsert eder. Saha
   anahtarı (`series_key`) InfluxDB'deki mevcut `plant_id` etiketiyle birebir
   seçilir — böylece geçmiş veri tek nokta taşınmadan kullanılabilir kalır.
3. Aynı anahtarı `vendor_plant_id` olarak taşıyan **eski düz tesisleri emer**:
   invertör, dizi, kalibrasyon ve anomali kayıtları ana tesise + sahaya
   bağlanır, ardından eski tesis satırı silinir.
4. `--drop-mock` ile demo tesisi (Konya GES) ve ona bağlı olayları siler.

Influx'a dokunmaz. Ana tesiste sahaya bağlanamayan invertör kalırsa siler
değil, uyarır — bunlar adaptör düzeltmesinden önce yazılmış çakışmalı
kayıtlardır ve elle incelenmelidir.
"""

import argparse
import asyncio
import logging

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from luminmind.config import Settings, get_settings
from luminmind.core.db import create_engine, session_scope
from luminmind.core.models import (
    AnomalyEvent,
    Inverter,
    Plant,
    PvArray,
    Site,
    TwinCalibration,
    User,
)
from luminmind.core.schemas import Vendor

logger = logging.getLogger(__name__)

MASTER_PLANT_NAME = "Tescom UPS İzmir"
MOCK_PLANT_ID = "mock-plant-1"


async def _ensure_master_plant(session: AsyncSession, settings: Settings) -> Plant:
    plant = (
        await session.scalars(
            select(Plant).where(
                Plant.vendor == Vendor.TESCOM.value,
                Plant.vendor_plant_id == settings.tescom_plant_id,
            )
        )
    ).one_or_none()
    if plant is not None:
        plant.name = MASTER_PLANT_NAME
        return plant

    owner = (await session.scalars(select(User).order_by(User.created_at))).first()
    if owner is None:
        raise RuntimeError("kullanıcı yok — önce `python -m luminmind.scripts.seed` çalıştırın")

    plant = Plant(
        owner_id=owner.id,
        name=MASTER_PLANT_NAME,
        vendor=Vendor.TESCOM.value,
        vendor_plant_id=settings.tescom_plant_id,
        latitude=settings.tescom_latitude,
        longitude=settings.tescom_longitude,
        timezone=settings.tescom_timezone,
    )
    session.add(plant)
    await session.flush()
    logger.info("ana tesis oluşturuldu: %s", MASTER_PLANT_NAME)
    return plant


async def _upsert_sites(session: AsyncSession, plant: Plant, settings: Settings) -> list[Site]:
    sites: list[Site] = []
    for order, (code, (series_key, name, capacity)) in enumerate(
        settings.tescom_factory_sites.items(), start=1
    ):
        site = (
            await session.scalars(select(Site).where(Site.series_key == series_key))
        ).one_or_none()
        if site is None:
            site = Site(plant_id=plant.id, code=code, series_key=series_key)
            session.add(site)
            logger.info("saha oluşturuldu: %s (%s)", name, series_key)
        site.plant_id = plant.id
        site.name = name
        site.code = code
        site.display_order = order
        if capacity:
            site.dc_capacity_kwp = capacity
            # AC anma gücü bilinmiyorsa DC/AC 1,2 oranıyla türetilir
            site.ac_capacity_kw = site.ac_capacity_kw or round(capacity / 1.2, 1)
        sites.append(site)
    await session.flush()
    return sites


async def _absorb_legacy_plant(
    session: AsyncSession, master: Plant, site: Site, legacy: Plant
) -> None:
    """Eski düz tesisin tüm bağlı kayıtlarını ana tesis + sahaya taşır."""
    for model in (Inverter, PvArray):
        await session.execute(
            update(model)
            .where(model.plant_id == legacy.id)
            .values(plant_id=master.id, site_id=site.id)
        )
    await session.execute(
        update(AnomalyEvent)
        .where(AnomalyEvent.plant_id == legacy.id)
        .values(plant_id=master.id, site_id=site.id)
    )
    await session.execute(
        update(TwinCalibration)
        .where(TwinCalibration.plant_id == legacy.id)
        .values(plant_id=master.id, site_id=site.id)
    )
    # Kapasite/koordinat/tarife sahada boşsa eski tesisten devral
    site.dc_capacity_kwp = site.dc_capacity_kwp or legacy.dc_capacity_kwp
    site.ac_capacity_kw = site.ac_capacity_kw or legacy.ac_capacity_kw
    site.latitude = site.latitude or legacy.latitude
    site.longitude = site.longitude or legacy.longitude
    site.feed_in_tariff_try_kwh = site.feed_in_tariff_try_kwh or legacy.feed_in_tariff_try_kwh
    site.grid_export_limit_kw = site.grid_export_limit_kw or legacy.grid_export_limit_kw

    await session.delete(legacy)
    logger.info("eski tesis emildi: %s → saha %s", legacy.vendor_plant_id, site.name)


async def _drop_mock_plant(session: AsyncSession) -> bool:
    """Demo tesisi ve ona bağlı olayları siler (FK'de ondelete yok, elle)."""
    from luminmind.core.models import ArbitragePlan, BatterySystem

    mock = (
        await session.scalars(select(Plant).where(Plant.vendor_plant_id == MOCK_PLANT_ID))
    ).one_or_none()
    if mock is None:
        return False

    events = (
        await session.scalars(select(AnomalyEvent).where(AnomalyEvent.plant_id == mock.id))
    ).all()
    for event in events:
        await session.delete(event)
    calibrations = (
        await session.scalars(select(TwinCalibration).where(TwinCalibration.plant_id == mock.id))
    ).all()
    for calibration in calibrations:
        await session.delete(calibration)
    batteries = (
        await session.scalars(select(BatterySystem).where(BatterySystem.plant_id == mock.id))
    ).all()
    for battery in batteries:
        plans = (
            await session.scalars(
                select(ArbitragePlan).where(ArbitragePlan.battery_id == battery.id)
            )
        ).all()
        for plan in plans:
            await session.delete(plan)
    await session.flush()
    await session.delete(mock)
    logger.info("demo tesis silindi: %s (%d anomali)", MOCK_PLANT_ID, len(events))
    return True


async def _report_orphans(
    session: AsyncSession, master: Plant, prune: bool = False
) -> list[str]:
    """Sahaya bağlanamamış invertörler — çakışmalı eski yazımların kalıntısı.

    Adaptör düzeltmesinden önce tüm cihazlar tek tesise, çakışan numaralarla
    yazılıyordu; o dönemden kalan invertör satırları hiçbir sahaya ait değil.
    Doğru kayıtlar artık sahaların altında olduğu için bunlar kopyadır, ama
    silme kararı çağırana bırakılır (`--prune-orphans`).
    """
    orphans = (
        await session.scalars(
            select(Inverter).where(Inverter.plant_id == master.id, Inverter.site_id.is_(None))
        )
    ).all()
    names = [o.vendor_device_id for o in orphans]
    if not names:
        return []
    if prune:
        for orphan in orphans:
            await session.delete(orphan)
        logger.info("sahasız %d invertör silindi: %s", len(names), ", ".join(names))
    else:
        logger.warning(
            "sahaya bağlanmamış %d invertör: %s — silmek için --prune-orphans",
            len(names),
            ", ".join(names),
        )
    return names


async def bootstrap_sites(
    session: AsyncSession,
    settings: Settings | None = None,
    drop_mock: bool = False,
    prune_orphans: bool = False,
) -> dict[str, object]:
    """Yapıyı kurar; yapılan işin özetini döndürür. Tekrar çalıştırılabilir."""
    settings = settings or get_settings()
    master = await _ensure_master_plant(session, settings)
    sites = await _upsert_sites(session, master, settings)

    absorbed: list[str] = []
    for site in sites:
        legacy = (
            await session.scalars(
                select(Plant).where(
                    Plant.vendor_plant_id == site.series_key, Plant.id != master.id
                )
            )
        ).one_or_none()
        if legacy is not None:
            await _absorb_legacy_plant(session, master, site, legacy)
            absorbed.append(site.series_key)

    dropped_mock = await _drop_mock_plant(session) if drop_mock else False
    await session.flush()

    master.dc_capacity_kwp = sum(s.dc_capacity_kwp or 0.0 for s in sites) or None
    master.ac_capacity_kw = sum(s.ac_capacity_kw or 0.0 for s in sites) or None
    orphans = await _report_orphans(session, master, prune=prune_orphans)

    return {
        "plant": master.name,
        "sites": [s.series_key for s in sites],
        "absorbed": absorbed,
        "dropped_mock": dropped_mock,
        "orphan_inverters": orphans,
    }


async def _run(drop_mock: bool, dry_run: bool, prune_orphans: bool) -> dict[str, object]:
    settings = get_settings()
    engine = create_engine(settings.postgres_dsn)
    try:
        async with session_scope(engine) as session:
            summary = await bootstrap_sites(
                session, settings, drop_mock=drop_mock, prune_orphans=prune_orphans
            )
            if dry_run:
                await session.rollback()
                logger.info("dry-run: değişiklikler geri alındı")
        return summary
    finally:
        await engine.dispose()


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--drop-mock", action="store_true", help="demo tesisi (Konya GES) sil"
    )
    parser.add_argument(
        "--dry-run", action="store_true", help="değişiklikleri yazma, yalnızca raporla"
    )
    parser.add_argument(
        "--prune-orphans",
        action="store_true",
        help="sahaya bağlanamayan (çakışma dönemi kalıntısı) invertörleri sil",
    )
    args = parser.parse_args()
    summary = asyncio.run(_run(args.drop_mock, args.dry_run, args.prune_orphans))
    for key, value in summary.items():
        logger.info("%s: %s", key, value)


if __name__ == "__main__":
    main()


__all__ = ["MASTER_PLANT_NAME", "MOCK_PLANT_ID", "bootstrap_sites"]
