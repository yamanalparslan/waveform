import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import create_async_engine

from luminmind.core.db import session_scope
from luminmind.core.models import Base, BatterySystem, Inverter, Plant, User


@pytest.fixture
async def engine():
    engine = create_async_engine("sqlite+aiosqlite://")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    yield engine
    await engine.dispose()


async def test_plant_relationships_roundtrip(engine):
    async with session_scope(engine) as session:
        user = User(email="a@b.c", hashed_password="x", role="admin")
        plant = Plant(
            owner=user,
            name="Test GES",
            vendor="mock",
            vendor_plant_id="p1",
            dc_capacity_kwp=1000.0,
        )
        plant.inverters = [Inverter(vendor_device_id="inv-01", ac_capacity_kw=250.0)]
        plant.batteries = [
            BatterySystem(
                cells_series=8, cells_parallel=1, rated_energy_kwh=0.4, rated_power_kw=0.5
            )
        ]
        session.add(plant)

    async with session_scope(engine) as session:
        loaded = (await session.scalars(select(Plant))).one()
        assert loaded.name == "Test GES"
        assert (await loaded.awaitable_attrs.owner).email == "a@b.c"
        inverters = await loaded.awaitable_attrs.inverters
        assert [i.vendor_device_id for i in inverters] == ["inv-01"]
        batteries = await loaded.awaitable_attrs.batteries
        assert batteries[0].cells_series == 8


async def test_session_scope_rolls_back_on_error(engine):
    with pytest.raises(RuntimeError):
        async with session_scope(engine) as session:
            session.add(User(email="x@y.z", hashed_password="x"))
            raise RuntimeError("boom")

    async with session_scope(engine) as session:
        assert (await session.scalars(select(User))).all() == []
