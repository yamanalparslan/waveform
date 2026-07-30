"""Tesis/saha göçü: idempotanlık, eski tesislerin emilmesi, demo temizliği."""

from datetime import UTC, datetime

import pytest
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import create_async_engine

from luminmind.config import Settings
from luminmind.core.db import session_scope
from luminmind.core.models import (
    AnomalyEvent,
    Base,
    Inverter,
    Plant,
    PvArray,
    Site,
    TwinCalibration,
    User,
)
from luminmind.core.schemas import Vendor
from luminmind.scripts.bootstrap_sites import MOCK_PLANT_ID, bootstrap_sites
from luminmind.scripts.seed import seed

URETIM = "tescom-izmir-uretim"
MEKANIK = "tescom-izmir-mekanik"
SETTINGS = Settings(
    tescom_base_url="http://tescom.local:8503",
    tescom_plant_id="tescom-izmir",
    lm_use_mock_vendors=True,
)


@pytest.fixture
async def engine():
    engine = create_async_engine("sqlite+aiosqlite://")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    async with session_scope(engine) as session:
        await seed(session)
    yield engine
    await engine.dispose()


async def add_legacy_plant(session, vendor_plant_id: str, capacity: float) -> Plant:
    """Göçten önceki dünyayı taklit eder: her fabrika ayrı düz bir tesis."""
    owner = (await session.scalars(select(User))).first()
    plant = Plant(
        owner_id=owner.id,
        name=f"Eski {vendor_plant_id}",
        vendor=Vendor.TESCOM.value,
        vendor_plant_id=vendor_plant_id,
        latitude=38.53,
        longitude=27.14,
        dc_capacity_kwp=capacity,
        feed_in_tariff_try_kwh=3.1,
    )
    session.add(plant)
    await session.flush()
    session.add(Inverter(plant_id=plant.id, vendor_device_id="1", ac_capacity_kw=100.0))
    session.add(
        AnomalyEvent(
            plant_id=plant.id,
            kind="soiling",
            severity="warning",
            deviation_pct=-8.0,
            started_at=datetime(2026, 7, 25, tzinfo=UTC),
            status="open",
            evidence={},
        )
    )
    session.add(
        TwinCalibration(
            plant_id=plant.id,
            fitted_at=datetime(2026, 7, 26, tzinfo=UTC),
            scale=0.95,
            hour_bias={},
            sample_count=500,
            quality={},
        )
    )
    await session.flush()
    return plant


# ------------------------------ Temel kurulum ------------------------------


async def test_creates_master_plant_and_two_sites(engine):
    async with session_scope(engine) as session:
        summary = await bootstrap_sites(session, SETTINGS)
    assert set(summary["sites"]) == {URETIM, MEKANIK}

    async with session_scope(engine) as session:
        sites = (await session.scalars(select(Site).order_by(Site.display_order))).all()
        assert [s.code for s in sites] == ["uretim", "mekanik"]
        assert [s.dc_capacity_kwp for s in sites] == [400.0, 250.0]
        # Saha anahtarı Influx'taki geçmiş etiketiyle birebir olmalı
        assert [s.series_key for s in sites] == [URETIM, MEKANIK]
        master = (
            await session.scalars(
                select(Plant).where(Plant.vendor_plant_id == "tescom-izmir")
            )
        ).one()
        assert master.name == "Tescom UPS İzmir"
        assert master.dc_capacity_kwp == 650.0  # sahaların toplamı


async def test_is_idempotent(engine):
    """İki kez çalıştırmak ikinci bir saha/tesis kümesi üretmemeli."""
    async with session_scope(engine) as session:
        await bootstrap_sites(session, SETTINGS)
    async with session_scope(engine) as session:
        await bootstrap_sites(session, SETTINGS)

    async with session_scope(engine) as session:
        assert await session.scalar(select(func.count()).select_from(Site)) == 2
        tescom = await session.scalar(
            select(func.count())
            .select_from(Plant)
            .where(Plant.vendor == Vendor.TESCOM.value)
        )
        assert tescom == 1


async def test_series_key_is_globally_unique(engine):
    """İki saha aynı anahtarı alırsa ölçümleri aynı seriye yazılır — DB engellemeli."""
    from sqlalchemy.exc import IntegrityError

    async with session_scope(engine) as session:
        await bootstrap_sites(session, SETTINGS)

    with pytest.raises(IntegrityError):
        async with session_scope(engine) as session:
            master = (
                await session.scalars(
                    select(Plant).where(Plant.vendor_plant_id == "tescom-izmir")
                )
            ).one()
            session.add(
                Site(plant_id=master.id, name="Kopya", code="kopya", series_key=URETIM)
            )


# ------------------------------ Eski tesislerin emilmesi ------------------------------


async def test_absorbs_legacy_plants_with_their_records(engine):
    async with session_scope(engine) as session:
        await add_legacy_plant(session, URETIM, 400.0)
        await add_legacy_plant(session, MEKANIK, 250.0)

    async with session_scope(engine) as session:
        summary = await bootstrap_sites(session, SETTINGS)
    assert set(summary["absorbed"]) == {URETIM, MEKANIK}

    async with session_scope(engine) as session:
        # Eski düz tesisler kalmamalı
        leftovers = (
            await session.scalars(
                select(Plant).where(Plant.vendor_plant_id.in_([URETIM, MEKANIK]))
            )
        ).all()
        assert leftovers == []

        master = (
            await session.scalars(
                select(Plant).where(Plant.vendor_plant_id == "tescom-izmir")
            )
        ).one()
        sites = {s.series_key: s for s in (await session.scalars(select(Site))).all()}

        # İnvertörler ana tesise + doğru sahaya bağlanmış olmalı
        inverters = (
            await session.scalars(select(Inverter).where(Inverter.plant_id == master.id))
        ).all()
        assert len(inverters) == 2
        assert {i.site_id for i in inverters} == {sites[URETIM].id, sites[MEKANIK].id}

        # Anomaliler ve kalibrasyonlar da taşınmış olmalı
        events = (
            await session.scalars(select(AnomalyEvent).where(AnomalyEvent.plant_id == master.id))
        ).all()
        assert len(events) == 2
        assert all(e.site_id is not None for e in events)
        calibrations = (
            await session.scalars(
                select(TwinCalibration).where(TwinCalibration.plant_id == master.id)
            )
        ).all()
        assert len(calibrations) == 2


async def test_absorb_inherits_missing_site_fields(engine):
    """Sahada boş olan tarife/koordinat eski tesisten devralınmalı."""
    async with session_scope(engine) as session:
        await add_legacy_plant(session, URETIM, 400.0)
    async with session_scope(engine) as session:
        await bootstrap_sites(session, SETTINGS)

    async with session_scope(engine) as session:
        site = (await session.scalars(select(Site).where(Site.series_key == URETIM))).one()
        assert site.feed_in_tariff_try_kwh == 3.1
        assert site.latitude == 38.53


async def test_absorbing_twice_is_safe(engine):
    async with session_scope(engine) as session:
        await add_legacy_plant(session, URETIM, 400.0)
    for _ in range(2):
        async with session_scope(engine) as session:
            await bootstrap_sites(session, SETTINGS)

    async with session_scope(engine) as session:
        assert await session.scalar(select(func.count()).select_from(Site)) == 2
        assert await session.scalar(select(func.count()).select_from(Inverter)) >= 1


# ------------------------------ Demo tesisin kaldırılması ------------------------------


async def test_mock_plant_kept_by_default(engine):
    async with session_scope(engine) as session:
        summary = await bootstrap_sites(session, SETTINGS)
    assert summary["dropped_mock"] is False
    async with session_scope(engine) as session:
        assert (
            await session.scalars(select(Plant).where(Plant.vendor_plant_id == MOCK_PLANT_ID))
        ).one_or_none() is not None


async def test_drop_mock_removes_plant_and_its_events(engine):
    async with session_scope(engine) as session:
        mock = (
            await session.scalars(select(Plant).where(Plant.vendor_plant_id == MOCK_PLANT_ID))
        ).one()
        session.add(
            AnomalyEvent(
                plant_id=mock.id,
                kind="shading",
                severity="critical",
                deviation_pct=-20.0,
                started_at=datetime(2026, 7, 20, tzinfo=UTC),
                status="open",
                evidence={},
            )
        )

    async with session_scope(engine) as session:
        summary = await bootstrap_sites(session, SETTINGS, drop_mock=True)
    assert summary["dropped_mock"] is True

    async with session_scope(engine) as session:
        assert (
            await session.scalars(select(Plant).where(Plant.vendor_plant_id == MOCK_PLANT_ID))
        ).one_or_none() is None
        # Yetim kayıt kalmamalı
        assert await session.scalar(select(func.count()).select_from(AnomalyEvent)) == 0
        assert await session.scalar(select(func.count()).select_from(Inverter)) == 0
        assert await session.scalar(select(func.count()).select_from(PvArray)) == 0
