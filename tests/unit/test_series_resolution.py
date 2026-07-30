"""Zaman serisi anahtarı → tesis/saha çözümlemesi.

Bu katman göç sırasında sessiz bir kırılmanın kaynağıydı: telemetrideki anahtar
sahanın olduğu halde arama tesis üzerinden yapılıyordu, hiçbir kayıt bulunamıyor
ve cihaz sağlığı senkronu hiç çalışmıyordu. Testler o hatanın geri dönmesini
engelliyor.
"""

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import create_async_engine

from luminmind.core.db import session_scope
from luminmind.core.models import Base, Plant, Site, User
from luminmind.core.schemas import Vendor
from luminmind.core.security import hash_password
from luminmind.core.series import (
    all_series_targets,
    resolve_series_key,
    series_capacities,
)

URETIM = "tescom-izmir-uretim"
MEKANIK = "tescom-izmir-mekanik"


@pytest.fixture
async def engine():
    engine = create_async_engine("sqlite+aiosqlite://")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    yield engine
    await engine.dispose()


async def build(session, *, with_sites: bool = True) -> Plant:
    user = User(email="a@b.c", hashed_password=hash_password("x" * 10), role="admin")
    session.add(user)
    await session.flush()
    plant = Plant(
        owner_id=user.id,
        name="Tescom UPS İzmir",
        vendor=Vendor.TESCOM.value,
        vendor_plant_id="tescom-izmir",
        latitude=38.53,
        longitude=27.14,
        dc_capacity_kwp=650.0,
        ac_capacity_kw=540.0,
    )
    session.add(plant)
    await session.flush()
    if with_sites:
        session.add_all(
            [
                Site(
                    plant_id=plant.id,
                    name="Üretim Fabrikası",
                    code="uretim",
                    series_key=URETIM,
                    dc_capacity_kwp=400.0,
                    ac_capacity_kw=333.0,
                    display_order=1,
                ),
                Site(
                    plant_id=plant.id,
                    name="Mekanik Fabrika",
                    code="mekanik",
                    series_key=MEKANIK,
                    dc_capacity_kwp=250.0,
                    display_order=2,
                ),
            ]
        )
        await session.flush()
    return plant


async def test_resolves_site_key_to_plant_and_site(engine):
    async with session_scope(engine) as session:
        await build(session)
    async with session_scope(engine) as session:
        target = await resolve_series_key(session, URETIM)
        assert target is not None
        assert target.site is not None
        assert target.site.code == "uretim"
        assert target.plant.vendor_plant_id == "tescom-izmir"
        assert target.series_key == URETIM
        assert "Üretim" in target.display_name


async def test_falls_back_to_plant_for_siteless_installs(engine):
    """Mock/Huawei/SMA kurulumlarında saha yok — eski davranış korunmalı."""
    async with session_scope(engine) as session:
        await build(session, with_sites=False)
    async with session_scope(engine) as session:
        target = await resolve_series_key(session, "tescom-izmir")
        assert target is not None
        assert target.site is None
        assert target.series_key == "tescom-izmir"


async def test_unknown_key_resolves_to_none(engine):
    async with session_scope(engine) as session:
        await build(session)
    async with session_scope(engine) as session:
        assert await resolve_series_key(session, "yok-boyle-bir-seri") is None


async def test_master_plant_key_still_resolves_after_sites_exist(engine):
    """Sahalar varken tesis anahtarı da çözümlenmeli (eski seriler için)."""
    async with session_scope(engine) as session:
        await build(session)
    async with session_scope(engine) as session:
        target = await resolve_series_key(session, "tescom-izmir")
        assert target is not None and target.site is None


async def test_all_targets_returns_one_per_site(engine):
    async with session_scope(engine) as session:
        await build(session)
    async with session_scope(engine) as session:
        targets = await all_series_targets(session)
        assert [t.series_key for t in targets] == [URETIM, MEKANIK]


async def test_all_targets_returns_plant_when_no_sites(engine):
    async with session_scope(engine) as session:
        await build(session, with_sites=False)
    async with session_scope(engine) as session:
        [target] = await all_series_targets(session)
        assert target.series_key == "tescom-izmir"


async def test_capacities_prefer_ac_nameplate(engine):
    """nMAE ölçülen AC gücü normalize eder; DC'ye bölmek hatayı küçük gösterir."""
    async with session_scope(engine) as session:
        await build(session)
    async with session_scope(engine) as session:
        capacities = await series_capacities(session)
        assert capacities[URETIM] == 333.0  # sahanın AC değeri
        # Mekanik sahasında AC yok → tesisin AC değerine düşer
        assert capacities[MEKANIK] == 540.0


async def test_dc_capacity_is_used_by_twin_not_scoring(engine):
    async with session_scope(engine) as session:
        await build(session)
    async with session_scope(engine) as session:
        target = await resolve_series_key(session, URETIM)
        assert target is not None
        assert target.capacity_kwp == 400.0  # ikizin dizi türetmesi
        assert target.capacity_kw == 333.0  # skorlamanın normalizasyonu


async def test_capacityless_series_is_excluded(engine):
    async with session_scope(engine) as session:
        plant = await build(session, with_sites=False)
        plant.dc_capacity_kwp = None
        plant.ac_capacity_kw = None
    async with session_scope(engine) as session:
        assert await series_capacities(session) == {}


async def test_site_capacity_overrides_plant(engine):
    async with session_scope(engine) as session:
        await build(session)
    async with session_scope(engine) as session:
        sites = {s.series_key: s for s in (await session.scalars(select(Site))).all()}
        assert sites[URETIM].dc_capacity_kwp == 400.0
        assert sites[MEKANIK].dc_capacity_kwp == 250.0
